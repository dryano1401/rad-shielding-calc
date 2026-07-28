"""TG-108 engine validated against the worked examples in the source report.

Every expected value here is printed in Madsen et al., Med Phys 33(1), 2006.
Tolerances reflect the report's own rounding: it quotes R(t) as 0.83 and F_U as
0.68, and carries intermediate results to 3 significant figures, so agreement
is checked to ~1% rather than to machine precision.
"""

from __future__ import annotations

import math

import pytest

from radshield.physics.decay import decay_factor, dose_reduction_factor
from radshield.physics.limits import tg108_goal
from radshield.physics.tg108 import (
    PatientSource,
    equivalent_thickness,
    required_transmission,
    solve_barrier,
    weekly_dose,
)

F18_HALF_LIFE_H = 110.0 / 60.0


def test_decay_reduction_factors_match_table_vi():
    """TG-108 quotes R(t) = 0.91, 0.83, 0.76 for F-18 at 30, 60 and 90 minutes."""
    assert dose_reduction_factor(F18_HALF_LIFE_H, 0.5) == pytest.approx(0.91, abs=0.005)
    assert dose_reduction_factor(F18_HALF_LIFE_H, 1.0) == pytest.approx(0.83, abs=0.005)
    assert dose_reduction_factor(F18_HALF_LIFE_H, 1.5) == pytest.approx(0.76, abs=0.005)


def test_uptake_decay_factor_matches_report():
    """F_U = exp(-0.693 * 60 / 110) for a 1 hour uptake; the report prints this as 0.68."""
    assert decay_factor(F18_HALF_LIFE_H, 1.0) == pytest.approx(0.685, abs=0.002)


def test_reduction_factor_tends_to_one_for_short_exposures():
    assert dose_reduction_factor(F18_HALF_LIFE_H, 1e-9) == pytest.approx(1.0, abs=1e-6)
    assert dose_reduction_factor(F18_HALF_LIFE_H, 0.0) == 1.0


def test_example_1_uptake_room():
    """Example 1: 4 m from an uptake chair, T=1, 40 pt/wk, 555 MBq, 1 h uptake -> B = 0.189."""
    source = PatientSource(
        kind="uptake",
        nuclide="F-18",
        administered_activity_MBq=555,
        patients_per_week=40,
        uptake_time_h=1.0,
        label="Uptake room",
    )
    dose = weekly_dose(source, distance_m=4.0)
    b = required_transmission(dose.weekly_dose_uSv, tg108_goal("uncontrolled"), occupancy=1.0)
    assert b == pytest.approx(0.189, rel=0.01)


def test_example_1_shielding_thickness():
    """Example 1 concludes ~1.2 cm lead or 15 cm concrete."""
    source = PatientSource(
        kind="uptake",
        nuclide="F-18",
        administered_activity_MBq=555,
        patients_per_week=40,
        uptake_time_h=1.0,
    )
    result = solve_barrier(
        sources=[(source, 4.0)],
        goal=tg108_goal("uncontrolled"),
        occupancy=1.0,
        materials=["lead", "concrete"],
    )
    assert result.thickness_by_material["lead"] == pytest.approx(1.2, abs=0.15)
    assert result.thickness_by_material["concrete"] == pytest.approx(15.0, abs=1.0)
    assert result.thickness_unit["lead"] == "cm"


def test_example_2_imaging_room():
    """Example 2: 3 m from the scanner -> 59.7 uSv/week, B = 0.34, ~0.8 cm Pb / 11 cm concrete."""
    source = PatientSource(
        kind="imaging",
        nuclide="F-18",
        administered_activity_MBq=555,
        patients_per_week=40,
        uptake_time_h=1.0,
        imaging_time_h=0.5,
        label="PET tomograph",
    )
    dose = weekly_dose(source, distance_m=3.0)
    assert dose.weekly_dose_uSv == pytest.approx(59.7, rel=0.01)

    result = solve_barrier(
        sources=[(source, 3.0)],
        goal=tg108_goal("uncontrolled"),
        occupancy=1.0,
        materials=["lead", "concrete"],
    )
    # The report quotes B to two significant figures.
    assert result.required_transmission == pytest.approx(0.34, rel=0.03)
    assert result.thickness_by_material["lead"] == pytest.approx(0.8, abs=0.15)
    assert result.thickness_by_material["concrete"] == pytest.approx(11.0, abs=1.0)


def test_example_4_room_above():
    """Example 4: d = 3.8 m above an uptake room -> 117 uSv, B = 0.17, ~1.3 cm Pb."""
    source = PatientSource(
        kind="uptake",
        nuclide="F-18",
        administered_activity_MBq=555,
        patients_per_week=40,
        uptake_time_h=1.0,
    )
    dose = weekly_dose(source, distance_m=3.8)
    assert dose.weekly_dose_uSv == pytest.approx(117.0, rel=0.01)

    result = solve_barrier(
        sources=[(source, 3.8)],
        goal=tg108_goal("uncontrolled"),
        occupancy=1.0,
        materials=["lead", "concrete"],
    )
    assert result.required_transmission == pytest.approx(0.17, rel=0.02)
    assert result.thickness_by_material["lead"] == pytest.approx(1.3, abs=0.15)
    # The example's prose says 17 cm of concrete, but that contradicts its own
    # Table IV: B = 0.170 falls between the 14 cm (0.2243) and 16 cm (0.1662)
    # entries, so the table-consistent answer is ~15.8 cm.  The Archer fit
    # reproduces Table IV to better than 1% (see test_tg108_table_iv_concrete),
    # so the fit is followed here and the discrepancy is documented rather than
    # tuned away.  The lead figure in the same example is self-consistent.
    assert result.thickness_by_material["concrete"] == pytest.approx(15.8, abs=0.5)


def test_example_4_existing_slab_credit():
    """Example 4 credits a 10 cm concrete slab (~0.65 cm Pb) leaving ~0.65 cm Pb to add."""
    pb_equivalent = equivalent_thickness("concrete", 10.0, "lead")
    assert pb_equivalent == pytest.approx(0.65, abs=0.1)

    source = PatientSource(
        kind="uptake",
        nuclide="F-18",
        administered_activity_MBq=555,
        patients_per_week=40,
        uptake_time_h=1.0,
    )
    result = solve_barrier(
        sources=[(source, 3.8)],
        goal=tg108_goal("uncontrolled"),
        occupancy=1.0,
        materials=["lead"],
        existing_barriers={"lead": pb_equivalent},
    )
    assert result.thickness_by_material["lead"] == pytest.approx(0.65, abs=0.15)


def test_example_5_room_below():
    """Example 5: d = 3.6 m below an uptake room -> 131 uSv, B = 0.15."""
    source = PatientSource(
        kind="uptake",
        nuclide="F-18",
        administered_activity_MBq=555,
        patients_per_week=40,
        uptake_time_h=1.0,
    )
    dose = weekly_dose(source, distance_m=3.6)
    assert dose.weekly_dose_uSv == pytest.approx(131.0, rel=0.01)
    b = required_transmission(dose.weekly_dose_uSv, tg108_goal("uncontrolled"), occupancy=1.0)
    assert b == pytest.approx(0.15, rel=0.02)


def test_example_6_console_distance():
    """Example 6: the console must be 2.32 m away to stay under 5 mSv/year."""
    source = PatientSource(
        kind="imaging",
        nuclide="F-18",
        administered_activity_MBq=555,
        patients_per_week=40,
        uptake_time_h=1.0,
        imaging_time_h=0.5,
    )
    # Weekly dose at 1 m, scaled to 50 weeks, must equal the 5 mSv annual goal.
    at_1m = weekly_dose(source, distance_m=1.0).weekly_dose_uSv
    annual_at_1m = at_1m * 50.0
    distance = math.sqrt(annual_at_1m / 5000.0)
    assert distance == pytest.approx(2.32, rel=0.01)


@pytest.mark.parametrize(
    "room,uptake_d,tomo_d,expected_total,expected_b",
    [
        ("Office 1", 8.0, 3.0, 97.2, 0.206),
        ("Office 2", 6.0, 3.0, 118.8, 0.169),
        ("Office 3", 8.0, 7.0, 40.0, 0.500),
        ("Office 8", 7.0, 8.0, 45.3, 0.442),
        ("Office 9", 9.0, 9.0, 29.2, 0.685),
    ],
)
def test_table_vii_sums_sources_before_solving(room, uptake_d, tomo_d, expected_total, expected_b):
    """Table VII sums uptake and tomograph dose at each point, then derives one B.

    Note the tomograph column of Table VII does *not* apply the 0.85 voiding
    credit that Example 2 applies: Example 2 reports 59.7 uSv at 3 m while
    Table VII reports 70.1 uSv for the same facility, and 59.7 / 0.85 = 70.2.
    Reproducing the table therefore requires void_factor = 1.0.  With that,
    every row agrees to better than 1.5%.
    """
    uptake = PatientSource(
        kind="uptake",
        nuclide="F-18",
        administered_activity_MBq=555,
        patients_per_week=40,
        uptake_time_h=1.0,
        label="Uptake room",
    )
    tomograph = PatientSource(
        kind="imaging",
        nuclide="F-18",
        administered_activity_MBq=555,
        patients_per_week=40,
        uptake_time_h=1.0,
        imaging_time_h=0.5,
        void_factor=1.0,
        label="Tomograph",
    )
    result = solve_barrier(
        sources=[(uptake, uptake_d), (tomograph, tomo_d)],
        goal=tg108_goal("uncontrolled"),
        occupancy=1.0,
        materials=["lead"],
    )
    assert result.total_weekly_dose_uSv == pytest.approx(expected_total, rel=0.02), room
    assert result.required_transmission == pytest.approx(expected_b, rel=0.02), room


def test_table_vii_corridor_uses_occupancy_and_controlled_goal():
    """Corridor 1: T = 0.25, P = 100 uSv, total 378.8 uSv -> no shielding required."""
    uptake = PatientSource(
        kind="uptake",
        nuclide="F-18",
        administered_activity_MBq=555,
        patients_per_week=40,
        uptake_time_h=1.0,
    )
    tomograph = PatientSource(
        kind="imaging",
        nuclide="F-18",
        administered_activity_MBq=555,
        patients_per_week=40,
        uptake_time_h=1.0,
        imaging_time_h=0.5,
        void_factor=1.0,
    )
    result = solve_barrier(
        sources=[(uptake, 2.5), (tomograph, 2.5)],
        goal=tg108_goal("controlled"),
        occupancy=0.25,
        materials=["lead"],
    )
    assert result.total_weekly_dose_uSv == pytest.approx(378.8, rel=0.02)
    # B = 100 / (0.25 * 378.8) > 1, so the report marks this "no shielding required".
    assert not result.shielding_required
    assert result.thickness_by_material["lead"] == 0.0


def test_occupancy_scales_the_goal_not_the_reported_dose():
    """Table VII footnote: occupancy enters B, but the reported dose is unmodified."""
    source = PatientSource(
        kind="uptake",
        nuclide="F-18",
        administered_activity_MBq=555,
        patients_per_week=40,
        uptake_time_h=1.0,
    )
    full = solve_barrier([(source, 4.0)], tg108_goal("uncontrolled"), 1.0, ["lead"])
    quarter = solve_barrier([(source, 4.0)], tg108_goal("uncontrolled"), 0.25, ["lead"])
    assert full.total_weekly_dose_uSv == pytest.approx(quarter.total_weekly_dose_uSv)
    assert quarter.required_transmission == pytest.approx(full.required_transmission * 4.0)


def test_scanner_self_shielding_credit_reduces_dose():
    """TG-108 suggests ~15% gantry credit; the default takes none."""
    base = PatientSource(
        kind="imaging",
        nuclide="F-18",
        administered_activity_MBq=555,
        patients_per_week=40,
        uptake_time_h=1.0,
        imaging_time_h=0.5,
    )
    credited = PatientSource(
        kind="imaging",
        nuclide="F-18",
        administered_activity_MBq=555,
        patients_per_week=40,
        uptake_time_h=1.0,
        imaging_time_h=0.5,
        scanner_attenuation=0.85,
    )
    assert weekly_dose(credited, 3.0).weekly_dose_uSv == pytest.approx(
        weekly_dose(base, 3.0).weekly_dose_uSv * 0.85
    )


def test_audit_trail_exposes_intermediates():
    """Results must carry the inputs and intermediates, not just the answer."""
    source = PatientSource(
        kind="imaging",
        nuclide="F-18",
        administered_activity_MBq=555,
        patients_per_week=40,
        uptake_time_h=1.0,
        imaging_time_h=0.5,
        label="Tomograph",
    )
    dose = weekly_dose(source, 3.0)
    assert "uptake decay F_U" in dose.terms
    assert "decay reduction R(t_I)" in dose.terms
    assert "void factor" in dose.terms
    assert any("provenance" in note for note in dose.notes)
    assert "Tomograph" in dose.audit_lines()[0]
