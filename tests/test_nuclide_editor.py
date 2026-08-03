"""The editable isotope/Archer overlay: add, edit, reset, and persist across restarts."""

from __future__ import annotations

import pytest

from radshield.physics import nuclides
from radshield.physics.archer import ArcherError


@pytest.fixture(autouse=True)
def _isolated_overlay(tmp_path, monkeypatch):
    """Route the overlay file to a scratch dir and undo any registry mutation."""
    monkeypatch.setenv("RADSHIELD_HOME", str(tmp_path))
    before_registry = dict(nuclides._registry)
    before_archer = dict(nuclides._archer)
    yield tmp_path
    nuclides._registry.clear()
    nuclides._registry.update(before_registry)
    nuclides._archer.clear()
    nuclides._archer.update(before_archer)


def test_default_511_archer_matches_f18():
    defaults = nuclides.default_511_archer()
    assert set(defaults) == {"lead", "concrete", "iron"}
    assert defaults["lead"].alpha == pytest.approx(1.543)
    assert defaults["lead"].unit == "cm"


def test_upsert_adds_a_new_isotope_prefilled_from_511kev_defaults():
    defaults = nuclides.default_511_archer()
    record = nuclides.upsert_record(
        "Test-Iso-1",
        half_life_min=100.0,
        gamma_eff=0.05,
        gamma_patient=None,
        is_511_kev=True,
        source="unit test",
        archer_by_material={
            "lead": (defaults["lead"].alpha, defaults["lead"].beta, defaults["lead"].gamma, "cm", "511 keV default"),
        },
    )
    assert record.name == "Test-Iso-1"
    assert record.is_builtin is False
    assert record.is_customized is True
    assert record.archer["lead"].alpha == pytest.approx(1.543)
    assert nuclides.get_archer("Test-Iso-1", "lead").alpha == pytest.approx(1.543)


def test_upsert_persists_across_a_simulated_restart(tmp_path):
    nuclides.upsert_record(
        "Test-Iso-2",
        half_life_min=200.0,
        gamma_eff=0.02,
        gamma_patient=0.01,
        is_511_kev=False,
        source="unit test",
        archer_by_material={
            "lead": (2.0, -0.1, 1.0, "cm", "unit test lead fit"),
        },
    )
    # Simulate a fresh process: clear the in-memory registry to just the
    # builtins, then reload the overlay from disk.
    nuclides._registry.clear()
    nuclides._registry.update(nuclides._BUILTIN_NUCLIDES)
    nuclides._archer.clear()
    nuclides._archer.update(nuclides._BUILTIN_ARCHER)

    nuclides.load_custom_overlay()

    reloaded = nuclides.get_nuclide("Test-Iso-2")
    assert reloaded.half_life_min == 200.0
    assert reloaded.gamma_patient == 0.01
    assert nuclides.get_archer("Test-Iso-2", "lead").alpha == 2.0


def test_editing_a_builtin_isotope_is_flagged_customized_and_persists():
    original = nuclides.get_nuclide("F-18")
    try:
        nuclides.upsert_record(
            "F-18",
            half_life_min=original.half_life_min,
            gamma_eff=0.150,  # deliberately different from the shipped 0.143
            gamma_patient=original.gamma_patient,
            is_511_kev=True,
            source="unit test override",
            archer_by_material={
                m: (p.alpha, p.beta, p.gamma, p.unit, p.source)
                for m, p in nuclides.default_511_archer().items()
            },
        )
        record = next(r for r in nuclides.list_records() if r.name == "F-18")
        assert record.is_builtin is True
        assert record.is_customized is True
        assert record.gamma_eff == 0.150
    finally:
        nuclides.delete_or_reset_record("F-18")

    restored = nuclides.get_nuclide("F-18")
    assert restored.gamma_eff == original.gamma_eff
    record = next(r for r in nuclides.list_records() if r.name == "F-18")
    assert record.is_customized is False


def test_delete_removes_a_purely_custom_isotope():
    nuclides.upsert_record(
        "Test-Iso-3",
        half_life_min=50.0,
        gamma_eff=0.01,
        gamma_patient=None,
        is_511_kev=False,
        source="unit test",
        archer_by_material={},
    )
    result = nuclides.delete_or_reset_record("Test-Iso-3")
    assert result is None
    assert "Test-Iso-3" not in nuclides.available_nuclides()


def test_delete_unknown_nuclide_raises():
    with pytest.raises(nuclides.NuclideError):
        nuclides.delete_or_reset_record("Nonexistent-99")


def test_upsert_rejects_non_positive_half_life():
    with pytest.raises(ValueError, match="half_life_min"):
        nuclides.upsert_record(
            "Test-Iso-4",
            half_life_min=0.0,
            gamma_eff=0.01,
            gamma_patient=None,
            is_511_kev=False,
            source="unit test",
            archer_by_material={},
        )


def test_upsert_rejects_non_positive_archer_alpha():
    with pytest.raises(ArcherError):
        nuclides.upsert_record(
            "Test-Iso-5",
            half_life_min=100.0,
            gamma_eff=0.01,
            gamma_patient=None,
            is_511_kev=False,
            source="unit test",
            archer_by_material={"lead": (0.0, 0.0, 1.0, "cm", "bad fit")},
        )


def test_removing_a_material_from_an_existing_record_drops_its_archer_data():
    nuclides.upsert_record(
        "Test-Iso-6",
        half_life_min=100.0,
        gamma_eff=0.01,
        gamma_patient=None,
        is_511_kev=False,
        source="unit test",
        archer_by_material={"lead": (2.0, 0.0, 1.0, "cm", "v1")},
    )
    nuclides.upsert_record(
        "Test-Iso-6",
        half_life_min=100.0,
        gamma_eff=0.01,
        gamma_patient=None,
        is_511_kev=False,
        source="unit test",
        archer_by_material={},
    )
    assert nuclides.available_materials("Test-Iso-6") == []


def test_load_custom_overlay_skips_malformed_entries(tmp_path):
    (tmp_path / "custom_nuclides.json").write_text('{"nuclides": [{"name": "Broken"}]}')
    # Should not raise, and should not register the incomplete entry.
    nuclides.load_custom_overlay(tmp_path / "custom_nuclides.json")
    assert "Broken" not in nuclides.available_nuclides()
