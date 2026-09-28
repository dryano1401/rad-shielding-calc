"""A registered isotope's own materials are honoured, not a hardcoded list."""

from __future__ import annotations

import pytest

from radshield.engine.evaluate import evaluate_point
from radshield.engine.exposure import exposure_map
from radshield.model.project import PointOfInterest, SourcePoint
from radshield.physics import materials, nuclides
from radshield.physics.archer import ArcherParams
from radshield.physics.nuclides import Nuclide

from .test_geometry_and_engine import build_project

# Stand-in fits. The numbers are arbitrary -- what is under test is whether
# registered data is reached at all, not its value.
FITS = {"lead": 1.2, "concrete": 0.12, "gypsum": 0.05}


@pytest.fixture
def lu177():
    nuclides.register_nuclide(
        Nuclide("Lu-177-test", half_life_min=9590.4, gamma_eff=0.0045,
                gamma_patient=0.0035, is_511_kev=False, source="test fixture"),
        overwrite=True,
    )
    for material, alpha in FITS.items():
        nuclides.register_archer(
            "Lu-177-test",
            ArcherParams(alpha=alpha, beta=alpha * 2, gamma=0.7, unit="cm",
                         material=material, source="test fixture"),
            overwrite=True,
        )
    return "Lu-177-test"


def project_with(nuclide: str, *, report_materials: list[str]):
    p = build_project()
    p.materials = report_materials
    p.sources.append(SourcePoint(
        id="src1", floor_id="fl1", x=300.0, y=200.0, label="therapy",
        method="tg108", height_above_floor_m=1.0,
        params={"nuclide": nuclide, "kind": "uptake",
                "administered_activity_MBq": 7400, "patients_per_week": 5,
                "uptake_time_h": 4, "imaging_time_h": 0},
    ))
    return p


# --- the shared naming ---------------------------------------------------


@pytest.mark.parametrize("spelling", ["gypsum", "Gypsum Wallboard", "GYPSUM", "drywall"])
def test_every_accepted_spelling_of_a_material_normalises_together(spelling):
    assert materials.normalise(spelling) == "gypsum"


def test_steel_and_iron_stay_distinct():
    """NCRP 147 fits steel and TG-108 fits iron. They are different materials
    with different data, so merging them would attribute one's attenuation to
    the other."""
    assert materials.normalise("steel") != materials.normalise("iron")


def test_an_unknown_name_still_matches_itself():
    assert materials.normalise(" Tungsten ") == "tungsten"


# --- the thickness columns ----------------------------------------------


def test_a_custom_isotope_is_solved_for_the_materials_it_registers(lu177):
    """The engine used a hardcoded lead/concrete/iron set, so a published
    gypsum fit entered through the isotope editor was refused."""
    project = project_with(lu177, report_materials=["lead", "concrete", "gypsum"])
    poi = PointOfInterest(
        id="poi1", floor_id="fl1", x=330.0, y=200.0, auto_height=False,
        height_above_floor_m=1.0, occupancy=1.0, area_class="uncontrolled",
        offset_applied=True, linked_source_ids=["src1"],
    )
    project.pois.append(poi)
    method = evaluate_point(project, poi).methods[0]

    assert method.thickness_mm["gypsum"] > 0
    assert "gypsum" not in method.unavailable


def test_a_material_the_isotope_has_no_fit_for_says_what_it_does_have(lu177):
    project = project_with(lu177, report_materials=["lead", "steel"])
    poi = PointOfInterest(
        id="poi1", floor_id="fl1", x=330.0, y=200.0, auto_height=False,
        height_above_floor_m=1.0, occupancy=1.0, area_class="uncontrolled",
        offset_applied=True, linked_source_ids=["src1"],
    )
    project.pois.append(poi)
    method = evaluate_point(project, poi).methods[0]

    assert method.thickness_mm["lead"] > 0
    reason = method.unavailable["steel"]
    assert "gypsum" in reason and "lead" in reason      # names what it does have
    assert "511 keV" not in reason                     # Lu-177 is not a positron emitter


def test_f18_still_reports_gypsum_as_unavailable():
    """The shipped 511 keV set really is lead, concrete and iron, so removing
    the hardcoded list must not invent data that was never registered."""
    project = project_with("F-18", report_materials=["lead", "gypsum"])
    poi = PointOfInterest(
        id="poi1", floor_id="fl1", x=330.0, y=200.0, auto_height=False,
        height_above_floor_m=1.0, occupancy=1.0, area_class="uncontrolled",
        offset_applied=True, linked_source_ids=["src1"],
    )
    project.pois.append(poi)
    method = evaluate_point(project, poi).methods[0]

    assert method.thickness_mm["lead"] > 0
    assert "gypsum" in method.unavailable


# --- and the screening overlay ------------------------------------------


@pytest.mark.parametrize("spelling", ["gypsum", "Gypsum Wallboard", "drywall"])
def test_a_trial_barrier_reaches_a_custom_isotopes_own_fit(lu177, spelling):
    project = project_with(lu177, report_materials=["lead"])
    bare = exposure_map(project, "fl1", columns=12).worst_ratio
    shielded = exposure_map(project, "fl1", columns=12,
                            trial_material=spelling, trial_thickness_mm=15.9)
    assert shielded.worst_ratio < bare
    assert not [w for w in shielded.warnings if "does not apply" in w]


def test_a_trial_barrier_the_isotope_has_no_fit_for_is_still_reported(lu177):
    project = project_with(lu177, report_materials=["lead"])
    grid = exposure_map(project, "fl1", columns=12,
                        trial_material="Plate Glass", trial_thickness_mm=15.9)
    assert any("does not apply to every source" in w for w in grid.warnings)
