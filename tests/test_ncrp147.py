"""NCRP 147 table lookups and barrier calculations.

The extracted table set does not include NCRP 147's own worked examples, so
these tests verify table integrity, unit handling, equation structure and the
known-gap error paths rather than reproducing published answers.  Once the
report's Section 5 examples are available they should be added here as
end-to-end fixtures, matching the TG-108 test file.
"""

from __future__ import annotations

import pytest

from radshield.physics.limits import ncrp147_goal, tg108_goal
from radshield.physics.ncrp147 import barriers, ct, tables
from radshield.physics.ncrp147.barriers import XrayBarrierInputs


def test_material_aliases_resolve():
    assert tables.canonical_material("lead") == "Lead"
    assert tables.canonical_material("Gypsum") == "Gypsum Wallboard"
    assert tables.canonical_material("glass") == "Plate Glass"
    with pytest.raises(tables.TableLookupError):
        tables.canonical_material("adamantium")


def test_primary_archer_by_kvp_spot_values():
    """Table A.1 values must load exactly as transcribed."""
    lead = tables.primary_archer_by_kvp(100, "lead")
    assert (lead.alpha, lead.beta, lead.gamma) == (2.5, 15.28, 0.7557)
    concrete = tables.primary_archer_by_kvp(150, "concrete")
    assert (concrete.alpha, concrete.beta, concrete.gamma) == (0.03243, 0.08599, 1.467)


def test_known_extraction_gap_raises_informatively():
    """40 and 45 kVp were captured for concrete only; lead must fail loudly."""
    assert tables.primary_archer_by_kvp(40, "concrete") is not None
    with pytest.raises(tables.TableLookupError) as exc:
        tables.primary_archer_by_kvp(40, "lead")
    assert "available" in str(exc.value)


def test_secondary_workload_gap_for_steel_raises():
    """Table C.1 workload rows were not captured for steel, plate glass or wood."""
    assert tables.secondary_archer("Rad Room (all barriers)", "lead") is not None
    with pytest.raises(tables.TableLookupError):
        tables.secondary_archer("Rad Room (all barriers)", "steel")


def test_workload_totals_match_table_4_2():
    rad = tables.workload_totals("Rad Room (all barriers)")
    assert rad.wnorm_ma_min_per_patient == 2.5
    assert rad.patients_per_week == 110
    cardiac = tables.workload_totals("Cardiac Angiography")
    assert cardiac.wnorm_ma_min_per_patient == 160
    assert cardiac.patients_per_week == 19


def test_air_kerma_tables():
    assert tables.primary_air_kerma("Rad Room (floor or other barriers)") == 5.2
    assert tables.secondary_air_kerma("Rad Room (all barriers)", geometry="side") == 0.034
    assert (
        tables.secondary_air_kerma("Rad Room (all barriers)", geometry="forward_back") == 0.049
    )


def test_primary_air_kerma_absent_for_secondary_only_rooms():
    """Table 4.5 covers primary-beam rooms only; mammography is not among them."""
    with pytest.raises(tables.TableLookupError):
        tables.primary_air_kerma("Mammography Room")


def test_use_factors():
    assert tables.use_factor("Floor") == 0.89
    assert tables.use_factor("Cross-table wall") == 0.09


def test_occupancy_table_is_flagged_unverified():
    """These values were seeded from the published table, not from the extraction."""
    factors = tables.occupancy_factors()
    assert (1.0, True) not in [(f, v) for f, _, v in factors]
    assert all(not verified for _, _, verified in factors)
    assert 1.0 in [f for f, _, _ in factors]


def test_secondary_barrier_equation():
    """B_sec = P * d^2 / (K1^sec * N * T), computed by hand."""
    inputs = XrayBarrierInputs(
        workload="Rad Room (all barriers)",
        distance_m=3.0,
        occupancy=1.0,
        barrier_type="secondary",
    )
    result = barriers.evaluate(inputs, ncrp147_goal("uncontrolled"))
    expected_kerma = 0.034 * 110 / 9.0
    assert result.unshielded_weekly_kerma_mGy == pytest.approx(expected_kerma)
    assert result.required_transmission == pytest.approx(0.02 / expected_kerma)
    assert result.shielding_required


def test_primary_barrier_equation_includes_use_factor():
    """B_p = P * d^2 / (K1^P * U * N * T)."""
    inputs = XrayBarrierInputs(
        workload="Rad Room (floor or other barriers)",
        distance_m=2.5,
        occupancy=0.2,
        use_factor=0.89,
        barrier_type="primary",
    )
    result = barriers.evaluate(inputs, ncrp147_goal("uncontrolled"))
    expected_kerma = 5.2 * 0.89 * 110 / 6.25
    assert result.unshielded_weekly_kerma_mGy == pytest.approx(expected_kerma)
    assert result.required_transmission == pytest.approx(0.02 / (0.2 * expected_kerma))


def test_patients_per_week_defaults_to_surveyed_value_and_notes_it():
    inputs = XrayBarrierInputs(
        workload="Chest Room", distance_m=3.0, occupancy=1.0, barrier_type="secondary"
    )
    result = barriers.evaluate(inputs, ncrp147_goal("uncontrolled"))
    assert result.terms["patients per week N"] == 210
    assert any("defaulted" in note for note in result.notes)


def test_explicit_patient_load_overrides_survey():
    inputs = XrayBarrierInputs(
        workload="Chest Room",
        distance_m=3.0,
        occupancy=1.0,
        patients_per_week=50,
        barrier_type="secondary",
    )
    result = barriers.evaluate(inputs, ncrp147_goal("uncontrolled"))
    assert result.terms["patients per week N"] == 50
    assert not any("defaulted" in note for note in result.notes)


def test_thickness_returned_in_millimetres():
    inputs = XrayBarrierInputs(
        workload="Cardiac Angiography",
        distance_m=3.0,
        occupancy=1.0,
        barrier_type="secondary",
    )
    result = barriers.evaluate(inputs, ncrp147_goal("uncontrolled"))
    mm = barriers.required_thickness([result], "lead")
    # A cardiac angio suite at 3 m needs a substantial but plausible lead barrier.
    assert 0.5 < mm < 10.0


def test_existing_shielding_is_credited():
    inputs = XrayBarrierInputs(
        workload="Cardiac Angiography",
        distance_m=3.0,
        occupancy=1.0,
        barrier_type="secondary",
    )
    result = barriers.evaluate(inputs, ncrp147_goal("uncontrolled"))
    gross = barriers.required_thickness([result], "lead")
    net = barriers.required_thickness([result], "lead", existing_thickness_mm=0.5)
    assert net == pytest.approx(gross - 0.5)


def test_credit_cannot_go_negative():
    inputs = XrayBarrierInputs(
        workload="Chest Room", distance_m=6.0, occupancy=0.05, barrier_type="secondary"
    )
    result = barriers.evaluate(inputs, ncrp147_goal("uncontrolled"))
    assert barriers.required_thickness([result], "lead", existing_thickness_mm=99.0) == 0.0


def test_multiple_sources_sum_before_solving():
    """Two rooms feeding one point must produce more shielding than either alone."""
    a = barriers.evaluate(
        XrayBarrierInputs(
            workload="Cardiac Angiography",
            distance_m=3.0,
            occupancy=1.0,
            barrier_type="secondary",
        ),
        ncrp147_goal("uncontrolled"),
    )
    b = barriers.evaluate(
        XrayBarrierInputs(
            workload="Rad Room (all barriers)",
            distance_m=3.0,
            occupancy=1.0,
            barrier_type="secondary",
        ),
        ncrp147_goal("uncontrolled"),
    )
    combined = barriers.required_thickness([a, b], "lead")
    assert combined > barriers.required_thickness([a], "lead")
    assert combined > barriers.required_thickness([b], "lead")


def test_mismatched_goals_or_occupancy_are_rejected():
    a = barriers.evaluate(
        XrayBarrierInputs(workload="Chest Room", distance_m=3.0, occupancy=1.0),
        ncrp147_goal("uncontrolled"),
    )
    b = barriers.evaluate(
        XrayBarrierInputs(workload="Chest Room", distance_m=3.0, occupancy=0.2),
        ncrp147_goal("uncontrolled"),
    )
    with pytest.raises(ValueError, match="occupancy"):
        barriers.required_thickness([a, b], "lead")


def test_wrong_quantity_goal_is_rejected():
    """NCRP 147 works in air kerma; a TG-108 effective-dose goal must not be accepted."""
    inputs = XrayBarrierInputs(workload="Chest Room", distance_m=3.0, occupancy=1.0)
    with pytest.raises(ValueError, match="air-kerma"):
        barriers.evaluate(inputs, tg108_goal("uncontrolled"))


def test_ct_requires_explicit_scatter_data():
    """CT scatter constants were not extracted, so they cannot be silently defaulted."""
    with pytest.raises(ct.MissingScatterDataError):
        ct.CTScatterModel(method="dlp", source="test")
    with pytest.raises(ct.MissingScatterDataError):
        ct.CTScatterModel(method="isodose", source="test")
    with pytest.raises(ValueError, match="source is required"):
        ct.CTScatterModel(method="dlp", kappa_mGy_per_mGy_cm=3e-4, source="")


def test_ct_dlp_method():
    scatter = ct.CTScatterModel(
        method="dlp",
        kappa_mGy_per_mGy_cm=3.0e-4,
        body_region="body",
        source="placeholder value for test only",
    )
    inputs = ct.CTBarrierInputs(
        scatter=scatter,
        distance_m=4.0,
        occupancy=1.0,
        procedures_per_week=100,
        dlp_per_procedure_mGy_cm=600,
        kvp=125,
    )
    result = ct.evaluate(inputs, ncrp147_goal("uncontrolled"))
    expected = 3.0e-4 * 600 * 100 / 16.0
    assert result.unshielded_weekly_kerma_mGy == pytest.approx(expected)
    assert result.terms["total weekly DLP (mGy cm)"] == 60000
    mm = ct.required_thickness(result, "lead")
    assert mm > 0
