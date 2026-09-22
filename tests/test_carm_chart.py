"""C-arm barriers read from a manufacturer stray-radiation map."""

from __future__ import annotations

import pytest

from radshield.engine.evaluate import evaluate_point
from radshield.model.project import PointOfInterest, ScatterMapData, SourcePoint
from radshield.physics import isodose
from radshield.physics.limits import ncrp147_goal
from radshield.physics.ncrp147 import carm

from .test_geometry_and_engine import build_project

# A KAP-normalised map in the shape vendors publish: values in uGy per Gy cm2
# of KAP, on a plan grid in metres around the isocentre.
GRID_M = [-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0]
VALUES = [
    [0.5, 0.8, 1.2, 1.5, 1.2, 0.8, 0.5],
    [0.8, 1.5, 3.0, 4.0, 3.0, 1.5, 0.8],
    [1.2, 3.0, 8.0, 16.0, 8.0, 3.0, 1.2],
    [1.5, 4.0, 16.0, None, 16.0, 4.0, 1.5],
    [1.2, 3.0, 8.0, 16.0, 8.0, 3.0, 1.2],
    [0.8, 1.5, 3.0, 4.0, 3.0, 1.5, 0.8],
    [0.5, 0.8, 1.2, 1.5, 1.2, 0.8, 0.5],
]


def stray_map(map_id: str = "map1") -> ScatterMapData:
    return ScatterMapData(
        id=map_id, name="OEC stray radiation, 90 kVp", plane="plan",
        coordinate_unit="m", value_unit="uGy", per="Gy cm2",
        x_coords=list(GRID_M), y_coords=list(GRID_M),
        values=[list(row) for row in VALUES],
    )


def chart_source(**params) -> SourcePoint:
    defaults = {
        "kvp": 90, "kap_week_mGy_cm2": 9.648e5,
        "scatter_method": "chart", "plan_map_id": "map1",
        "field_area_cm2": 900.0, "field_distance_m": 1.0,
    }
    defaults.update(params)
    return SourcePoint(
        id="src1", floor_id="fl1", x=0.0, y=0.0, label="C-arm",
        method="carm", height_above_floor_m=1.0, params=defaults,
    )


@pytest.fixture
def project():
    p = build_project()
    p.materials = ["lead"]
    p.scatter_maps.append(stray_map())
    p.sources.append(chart_source())
    return p


def point(x_pdf: float) -> PointOfInterest:
    return PointOfInterest(
        id="poi1", floor_id="fl1", x=x_pdf, y=0.0, auto_height=False,
        height_above_floor_m=1.0, occupancy=1.0, area_class="uncontrolled",
        offset_applied=True, linked_source_ids=["src1"],
    )


# --- the KAP basis ------------------------------------------------------


def test_a_kap_normalised_chart_takes_the_weekly_kap_as_its_workload():
    """A map published per Gy cm2 needs the week's KAP, not a procedure count."""
    assert isodose.weekly_multiplier("Gy cm2", 0.0, 0.0, kap_week_Gy_cm2=964.8) == 964.8
    assert "Gy cm2" in isodose.WORKLOAD_BASIS


def test_the_other_bases_are_untouched():
    assert isodose.weekly_multiplier("procedure", 25.0, 0.0) == 25.0
    assert isodose.weekly_multiplier("mAs", 0.0, 4000.0) == 4000.0
    assert isodose.weekly_multiplier("100 mAs", 0.0, 4000.0) == 40.0


# --- a measured map already contains the leakage ------------------------


def test_the_modelled_leakage_is_dropped_rather_than_added_to_a_measurement():
    """A chamber in the room records scatter and leakage together, so adding
    the Eq. C.6-C.8 estimate on top would count the leakage twice."""
    inputs = carm.CArmInputs(
        kvp=90.0, kap_week_mGy_cm2=9.648e5, scatter_distance_m=3.0,
        leakage_distance_m=3.0, occupancy=1.0, field_area_cm2=900.0,
    )
    result = carm.evaluate_from_chart(
        inputs, ncrp147_goal("uncontrolled"), 8.0e-3, workload_per_week=964.8)

    assert result.leakage_mGy == 0.0
    assert result.scatter_mGy == pytest.approx(result.unshielded_weekly_kerma_mGy)
    assert result.unshielded_weekly_kerma_mGy == pytest.approx(8.0e-3 * 964.8)
    assert any("already includes tube-housing leakage" in n for n in result.notes)


def test_the_chart_route_is_attenuated_with_the_combined_secondary_fit():
    """The reading is a mixture in unknown proportion, which is what Table C.1
    is fitted to -- so one curve, not the component-wise split."""
    from radshield.engine.evaluate import _carm_chart_thickness_for
    from radshield.physics.archer import thickness as archer_thickness

    inputs = carm.CArmInputs(
        kvp=90.0, kap_week_mGy_cm2=9.648e5, scatter_distance_m=3.0,
        leakage_distance_m=3.0, occupancy=1.0, field_area_cm2=900.0,
    )
    result = carm.evaluate_from_chart(
        inputs, ncrp147_goal("uncontrolled"), 8.0e-3, workload_per_week=964.8)
    expected = archer_thickness(carm.barrier_params(inputs, "lead"), 0.05)
    assert _carm_chart_thickness_for(result, "lead", 0.05) == pytest.approx(expected)


# --- through the engine -------------------------------------------------


def test_a_chart_backed_carm_source_solves_end_to_end(project):
    poi = point(30.0)                       # 3 m out at 10 pdf units per metre
    project.pois.append(poi)
    result = evaluate_point(project, poi)

    assert not result.errors
    contribution = result.contributions[0]
    assert contribution.method == "carm"
    assert contribution.value > 0
    assert result.governing_thickness_mm["lead"] > 0
    assert any("stray" in n or "already includes" in n for n in contribution.notes)


def test_the_chart_value_scales_with_the_weekly_kap(project):
    """Doubling the KAP has to double the dose: the map is per unit KAP."""
    poi = point(30.0)
    project.pois.append(poi)
    once = evaluate_point(project, poi).contributions[0].unshielded_value

    project.sources[0] = chart_source(kap_week_mGy_cm2=2 * 9.648e5)
    twice = evaluate_point(project, poi).contributions[0].unshielded_value
    assert twice == pytest.approx(2 * once)


def test_the_model_and_chart_routes_give_different_answers_from_the_same_source(project):
    """Sanity that the mode switch is actually taking a different path rather
    than silently falling through to the model."""
    poi = point(30.0)
    project.pois.append(poi)
    charted = evaluate_point(project, poi).contributions[0].unshielded_value

    project.sources[0] = chart_source(scatter_method="model")
    modelled = evaluate_point(project, poi).contributions[0].unshielded_value
    assert charted != pytest.approx(modelled)


def test_a_chart_mode_source_with_no_map_assigned_reports_it(project):
    project.sources[0] = chart_source(plan_map_id="")
    poi = point(30.0)
    project.pois.append(poi)
    result = evaluate_point(project, poi)
    assert any("no chart is assigned" in e for e in result.errors)
