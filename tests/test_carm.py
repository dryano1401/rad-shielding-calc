"""C-arm secondary barriers worked from KAP."""

from __future__ import annotations

import math

import pytest

from radshield.engine.evaluate import evaluate_point, reference_dose
from radshield.model.project import PointOfInterest, SourcePoint
from radshield.physics.limits import ncrp147_goal
from radshield.physics.ncrp147 import carm

from .test_geometry_and_engine import build_project


def carm_source(**params) -> SourcePoint:
    defaults = {
        "kvp": 100,
        "kap_week_mGy_cm2": 9.648e5,
        "field_area_cm2": 900.0,
        "field_distance_m": 1.0,
    }
    defaults.update(params)
    return SourcePoint(
        id="src1", floor_id="fl1", x=0.0, y=0.0, label="C-arm",
        method="carm", height_above_floor_m=1.0, params=defaults,
    )


def base_inputs(**kwargs) -> carm.CArmInputs:
    defaults = dict(
        kvp=100.0, kap_week_mGy_cm2=9.648e5, scatter_distance_m=3.0,
        leakage_distance_m=3.0, occupancy=1.0, field_area_cm2=900.0,
    )
    defaults.update(kwargs)
    return carm.CArmInputs(**defaults)


# --- the two fits read off the report's figures -------------------------


@pytest.mark.parametrize("kvp,angle,expected", [
    # a1 = 1.6e-2 (kVp-125) + 8.43 - 1.11e-1 t + 9.83e-4 t^2 - 1.74e-6 t^3
    (100.0, 90.0, 4.73384e-6),
    (150.0, 90.0, 5.53384e-6),
    (100.0, 135.0, 6.67912e-6),
])
def test_scatter_fraction_matches_the_figure_c1_polynomial(kvp, angle, expected):
    assert carm.scatter_fraction(kvp, angle) == pytest.approx(expected, rel=1e-4)


def test_scatter_fraction_bottoms_out_near_seventy_degrees():
    """Figure C.1's curves are U-shaped with a minimum around 69 degrees, so
    90 degrees is near the least conservative angle available."""
    at_min = min(carm.scatter_fraction(100.0, t) for t in range(20, 141))
    assert carm.scatter_fraction(100.0, 69.0) == pytest.approx(at_min, rel=1e-3)
    assert carm.scatter_fraction(100.0, 135.0) > carm.scatter_fraction(100.0, 90.0)


@pytest.mark.parametrize("kvp,expected", [(50.0, 1.0655), (100.0, 4.692), (150.0, 9.7495)])
def test_air_kerma_per_workload_matches_the_figure_b1_polynomial(kvp, expected):
    assert carm.air_kerma_per_workload(kvp) == pytest.approx(expected, rel=1e-3)


def test_the_fits_are_refused_rather_than_extrapolated():
    with pytest.raises(carm.ScatterFractionError, match="50-150"):
        carm.scatter_fraction(40.0, 90.0)
    with pytest.raises(carm.ScatterFractionError, match="degrees"):
        carm.scatter_fraction(100.0, 170.0)
    with pytest.raises(carm.WorkloadFitError):
        carm.air_kerma_per_workload(200.0)


# --- unshielded kerma ---------------------------------------------------


def test_scatter_follows_equation_c2_with_the_field_area_cancelling():
    """K_S = KAP * a1 / dS^2 -- the field area drops out, so changing it must
    move only the leakage term."""
    wide = carm.unshielded_kerma(base_inputs(field_area_cm2=1800.0))
    narrow = carm.unshielded_kerma(base_inputs(field_area_cm2=450.0))
    assert wide[0] == pytest.approx(narrow[0])       # scatter unchanged
    assert wide[1] < narrow[1]                       # leakage does change

    scatter, _, _, _ = carm.unshielded_kerma(base_inputs())
    a1 = carm.scatter_fraction(100.0, carm.DEFAULT_SCATTER_ANGLE_DEG)
    assert scatter == pytest.approx(9.648e5 * a1 / 9.0)


def test_leakage_follows_the_equation_c6_regulatory_cap_model():
    """K_L(1 m) is the 0.876 mGy/h cap scaled by kVp^2, housing transmission
    and the workload KAP implies -- roughly 2e-4 of the primary at 100 kVp."""
    ratio = carm.leakage_fraction_of_primary(100.0)
    assert ratio == pytest.approx(2.1e-4, rel=0.05)

    _, leakage, _, _ = carm.unshielded_kerma(base_inputs())
    primary_1m = 9.648e5 * 1.0 / 900.0     # 1072 mGy/week at 1 m
    assert leakage == pytest.approx(primary_1m * ratio / 9.0)


def test_leakage_collapses_below_a_hundred_kvp():
    """The 2.32 mm housing all but stops a 70 kVp beam, which is why the
    report calls the leakage contribution negligible there."""
    assert carm.leakage_fraction_of_primary(70.0) < carm.leakage_fraction_of_primary(100.0) / 100
    assert carm.leakage_fraction_of_primary(150.0) > carm.leakage_fraction_of_primary(100.0)


def test_a_measured_leakage_figure_replaces_the_model():
    _, leakage, _, notes = carm.unshielded_kerma(
        base_inputs(leakage_at_1m_uGy_week=1200.0))
    assert leakage == pytest.approx(1.2 / 9.0)
    assert any("measured" in n for n in notes)


def test_an_entered_leakage_fraction_replaces_the_model():
    _, leakage, _, notes = carm.unshielded_kerma(base_inputs(leakage_fraction=1e-3))
    assert leakage == pytest.approx(1.072 / 9.0)
    assert any("entered as" in n for n in notes)


def test_an_entered_scatter_fraction_overrides_the_figure_and_says_so():
    scatter, _, _, notes = carm.unshielded_kerma(base_inputs(scatter_fraction=2.0e-5))
    assert any("entered for this source" in n for n in notes)
    tabulated, _, _, _ = carm.unshielded_kerma(base_inputs())
    assert scatter > tabulated


def test_the_result_keeps_scatter_and_leakage_visible_separately():
    result = carm.evaluate(base_inputs(), ncrp147_goal("uncontrolled"))
    assert result.scatter_mGy > 0 and result.leakage_mGy > 0
    assert result.unshielded_weekly_kerma_mGy == pytest.approx(
        result.scatter_mGy + result.leakage_mGy)
    assert any("primary term is zero" in n for n in result.notes)


# --- attenuation --------------------------------------------------------


def test_the_components_are_attenuated_by_their_own_transmissions():
    """Equation C.3 gives scatter the primary fit; Equation C.8 gives leakage
    a bare exponential at the high-attenuation HVL, which is more penetrating."""
    from radshield.physics import archer
    from radshield.physics.ncrp147 import tables

    inputs = base_inputs()
    params = tables.primary_archer_by_kvp(100.0, "lead")
    assert carm.scatter_transmission(inputs, "lead", 2.0) == pytest.approx(
        archer.transmission(params, 2.0))
    assert carm.leakage_transmission(inputs, "lead", 2.0) == pytest.approx(
        math.exp(-params.alpha * 2.0))
    assert carm.leakage_transmission(inputs, "lead", 2.0) > carm.scatter_transmission(
        inputs, "lead", 2.0)


def test_the_leakage_share_grows_with_depth_then_plateaus():
    """Unshielded, scatter is almost everything.  Beam hardening in the
    barrier lifts leakage's share roughly tenfold -- the report's reason for
    not dropping it -- but the two transmissions share the same asymptotic
    slope alpha, so the share levels off rather than taking over."""
    inputs = base_inputs()
    scatter, leakage, _, _ = carm.unshielded_kerma(inputs)

    def share(x: float) -> float:
        s = scatter * carm.scatter_transmission(inputs, "lead", x)
        el = leakage * carm.leakage_transmission(inputs, "lead", x)
        return el / (s + el)

    assert share(0.0) < 0.05
    assert share(1.0) > 5 * share(0.0)
    assert share(12.0) == pytest.approx(share(5.0), rel=1e-3)


def test_the_thickness_solve_hits_the_requested_transmission():
    inputs = base_inputs()
    for b in (0.5, 0.05, 1e-3):
        x = carm.thickness_for_transmission(inputs, "lead", b)
        assert carm.transmitted_fraction(inputs, "lead", x) == pytest.approx(b, rel=1e-6)


def test_no_shielding_is_required_when_transmission_is_already_met():
    assert carm.thickness_for_transmission(base_inputs(), "lead", 1.5) == 0.0
    assert carm.thickness_for_transmission(base_inputs(), "lead", float("inf")) == 0.0


def test_the_component_solve_tracks_the_combined_table_c1_fit_within_a_few_percent():
    """A cross-check on the whole construction: Table C.1's own mix differs
    from a C-arm's, but not enough to matter much -- the two routes land
    within a few percent, with the tabulated one the conservative side."""
    from radshield.physics.archer import thickness as archer_thickness

    inputs = base_inputs()
    result = carm.evaluate(inputs, ncrp147_goal("uncontrolled"))
    b = result.required_transmission
    combined = archer_thickness(carm.barrier_params(inputs, "lead"), b)
    component_wise = carm.thickness_for_transmission(inputs, "lead", b)
    assert component_wise == pytest.approx(combined, rel=0.05)
    assert combined > component_wise


# --- through the engine -------------------------------------------------


def test_a_carm_source_solves_through_the_engine_like_any_other():
    project = build_project()
    project.materials = ["lead"]
    project.sources.append(carm_source())
    poi = PointOfInterest(
        id="poi1", floor_id="fl1", x=40.0, y=0.0, auto_height=False,
        height_above_floor_m=1.0, occupancy=1.0, area_class="uncontrolled",
        offset_applied=True, linked_source_ids=["src1"],
    )
    project.pois.append(poi)
    result = evaluate_point(project, poi)

    assert not result.errors
    assert result.contributions[0].method == "carm"
    assert result.contributions[0].value > 0
    lead = result.governing_thickness_mm["lead"]
    assert lead > 0 and math.isfinite(lead)


def test_a_carm_source_reports_its_one_metre_reference_split():
    project = build_project()
    source = carm_source()
    project.sources.append(source)
    reference = reference_dose(project, source)
    assert reference["unit"] == "mGy/week"
    assert [c["label"] for c in reference["components"]] == ["scatter", "leakage"]
    assert reference["value"] == pytest.approx(
        sum(c["value"] for c in reference["components"]))


def test_carm_inputs_reject_impossible_geometry():
    with pytest.raises(ValueError):
        base_inputs(scatter_distance_m=0.0)
    with pytest.raises(ValueError):
        base_inputs(occupancy=0.0)
    with pytest.raises(ValueError):
        base_inputs(field_area_cm2=0.0)
    with pytest.raises(ValueError):
        base_inputs(kap_week_mGy_cm2=-1.0)


def test_a_project_saved_with_the_microgray_key_still_reads_its_kap():
    """The field was renamed from uGy cm2 to mGy cm2. Ignoring the old key would
    read a saved project as zero KAP and report no shielding required, so it is
    converted rather than dropped."""
    from radshield.engine.evaluate import _carm_kap_mGy_cm2

    assert _carm_kap_mGy_cm2({"kap_week_uGy_cm2": 9.648e8}) == pytest.approx(9.648e5)
    assert _carm_kap_mGy_cm2({"kap_week_mGy_cm2": 9.648e5}) == pytest.approx(9.648e5)
    # A project carrying both is taken at the new key, not silently rescaled.
    assert _carm_kap_mGy_cm2(
        {"kap_week_mGy_cm2": 500.0, "kap_week_uGy_cm2": 9.648e8}) == pytest.approx(500.0)
    assert _carm_kap_mGy_cm2({}) == 0.0


def test_the_legacy_key_gives_the_same_answer_through_the_engine():
    legacy = carm_source()
    legacy.params = {k: v for k, v in legacy.params.items() if k != "kap_week_mGy_cm2"}
    legacy.params["kap_week_uGy_cm2"] = 9.648e8

    project = build_project()
    project.sources.append(legacy)
    assert reference_dose(project, legacy)["value"] == pytest.approx(
        reference_dose(build_project(), carm_source())["value"])
