"""Nuclide registry, including extension to isotopes TG-108 does not cover."""

from __future__ import annotations

import pytest

from radshield.physics import nuclides
from radshield.physics.archer import ArcherParams
from radshield.physics.nuclides import Nuclide, NuclideError
from radshield.physics.tg108 import PatientSource, weekly_dose


def test_builtin_nuclides_loaded_from_table_ii():
    assert "F-18" in nuclides.available_nuclides()
    f18 = nuclides.get_nuclide("F-18")
    assert f18.half_life_min == 110
    assert f18.gamma_eff == 0.143
    assert f18.gamma_patient == 0.092
    assert nuclides.get_nuclide("Rb-82").gamma_eff == 0.159


def test_f18_patient_constant_is_tabulated_not_derived():
    value, provenance = nuclides.patient_dose_rate_constant("F-18")
    assert value == 0.092
    assert "tabulated" in provenance


def test_other_positron_emitters_derive_from_f18_ratio():
    """511 keV emitters share body attenuation, so the ratio transfers with a note."""
    value, provenance = nuclides.patient_dose_rate_constant("C-11")
    assert value == pytest.approx(0.148 * (0.092 / 0.143))
    assert "derived" in provenance


def test_unknown_nuclide_raises():
    with pytest.raises(NuclideError, match="not registered"):
        nuclides.get_nuclide("Xx-99")


def test_511kev_transmission_shared_across_positron_emitters():
    """The fit belongs to the photon energy, not the parent nuclide."""
    assert nuclides.get_archer("F-18", "lead") is nuclides.get_archer("Ga-68", "lead")


def test_registering_an_isotope_absent_from_tg108():
    """The stated requirement: add Archer values for a missing isotope, process unchanged."""
    nuclides.register_nuclide(
        Nuclide(
            name="Tc-99m-test",
            half_life_min=360.6,
            gamma_eff=0.0195,
            gamma_patient=0.0140,
            is_511_kev=False,
            source="test fixture",
        )
    )
    nuclides.register_archer(
        "Tc-99m-test",
        ArcherParams(
            alpha=2.9,
            beta=0.0,
            gamma=1.0,
            unit="cm",
            material="lead",
            source="test fixture, 140 keV",
        ),
    )

    source = PatientSource(
        kind="uptake",
        nuclide="Tc-99m-test",
        administered_activity_MBq=740,
        patients_per_week=25,
        uptake_time_h=1.0,
    )
    dose = weekly_dose(source, distance_m=3.0)
    assert dose.weekly_dose_uSv > 0
    assert nuclides.get_archer("Tc-99m-test", "lead").alpha == 2.9


def test_non_511kev_isotope_without_patient_constant_fails_loudly():
    """Borrowing F-18's body-attenuation ratio for a 140 keV isotope would be wrong."""
    nuclides.register_nuclide(
        Nuclide(
            name="I-131-test",
            half_life_min=11563,
            gamma_eff=0.0658,
            gamma_patient=None,
            is_511_kev=False,
            source="test fixture",
        )
    )
    with pytest.raises(NuclideError, match="cannot be borrowed"):
        nuclides.patient_dose_rate_constant("I-131-test")


def test_duplicate_registration_is_rejected():
    with pytest.raises(ValueError, match="already registered"):
        nuclides.register_nuclide(
            Nuclide(name="F-18", half_life_min=1, gamma_eff=1, source="typo")
        )


def test_missing_material_lists_what_is_available():
    with pytest.raises(NuclideError, match="register_archer"):
        nuclides.get_archer("F-18", "tungsten")


def test_mixed_nuclides_at_a_point_require_explicit_choice():
    """Summing across photon energies must not silently pick one nuclide's attenuation."""
    from radshield.physics.limits import tg108_goal
    from radshield.physics.tg108 import solve_barrier

    f18 = PatientSource(
        kind="uptake",
        nuclide="F-18",
        administered_activity_MBq=555,
        patients_per_week=40,
        uptake_time_h=1.0,
    )
    ga68 = PatientSource(
        kind="uptake",
        nuclide="Ga-68",
        administered_activity_MBq=185,
        patients_per_week=5,
        uptake_time_h=1.0,
    )
    with pytest.raises(ValueError, match="nuclide_for_attenuation"):
        solve_barrier([(f18, 4.0), (ga68, 4.0)], tg108_goal("uncontrolled"), 1.0, ["lead"])

    result = solve_barrier(
        [(f18, 4.0), (ga68, 4.0)],
        tg108_goal("uncontrolled"),
        1.0,
        ["lead"],
        nuclide_for_attenuation="F-18",
    )
    assert result.total_weekly_dose_uSv > 0
