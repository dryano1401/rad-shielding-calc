"""Isotope mixes at one source region."""
from __future__ import annotations
import pytest
from radshield.engine.evaluate import evaluate_point
from radshield.model.project import PointOfInterest, SourcePoint
from radshield.model.store import load, save
from radshield.physics.tg108 import PatientSource, combined_weekly_dose
from tests.test_geometry_and_engine import build_project, uptake_source


def mixed_source(**overrides) -> SourcePoint:
    """A PET uptake region running FDG plus a Ga-68 tracer."""
    params = {"components": [
        {"kind": "uptake", "nuclide": "F-18", "administered_activity_MBq": 555,
         "patients_per_week": 40, "uptake_time_h": 1.0, "label": "F-18 FDG"},
        {"kind": "uptake", "nuclide": "Ga-68", "administered_activity_MBq": 185,
         "patients_per_week": 6, "uptake_time_h": 0.75, "label": "Ga-68 DOTATATE"},
    ]}
    params.update(overrides)
    return SourcePoint(id="src1", floor_id="fl1", x=0, y=0, label="Uptake room",
                       method="tg108", height_above_floor_m=1.0, params=params)


def poi_above(project):
    poi = PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, auto_height=True,
                          occupancy=1.0, linked_source_ids=["src1"])
    project.pois.append(poi)
    return poi


def test_each_isotope_keeps_its_own_decay_factors():
    """F-18 over 1 h and Ga-68 over 45 min give different R(t)."""
    mix = [
        PatientSource(kind="uptake", nuclide="F-18", administered_activity_MBq=555,
                      patients_per_week=40, uptake_time_h=1.0),
        PatientSource(kind="uptake", nuclide="Ga-68", administered_activity_MBq=185,
                      patients_per_week=6, uptake_time_h=0.75),
    ]
    combined = combined_weekly_dose(mix, 4.0)
    factors = [
        next(v for k, v in d.terms.items() if "decay reduction" in k)
        for d in combined.per_nuclide
    ]
    assert factors[0] == pytest.approx(0.8327, abs=1e-3)   # F-18, 110 min, 1 h
    assert factors[1] == pytest.approx(0.8010, abs=1e-3)   # Ga-68, 67.6 min, 0.75 h
    assert combined.weekly_dose_uSv == pytest.approx(
        sum(d.weekly_dose_uSv for d in combined.per_nuclide)
    )


def test_mixed_source_dose_is_the_sum_of_its_isotopes():
    project = build_project()
    project.sources.append(mixed_source())
    poi = poi_above(project)
    result = evaluate_point(project, poi)

    contribution = result.contributions[0]
    assert len(contribution.components) == 2
    assert {c["nuclide"] for c in contribution.components} == {"F-18", "Ga-68"}
    assert contribution.unshielded_value == pytest.approx(
        sum(c["unshielded_uSv"] for c in contribution.components)
    )


def test_adding_an_isotope_raises_the_dose_and_the_barrier():
    project = build_project()
    project.materials = ["lead"]
    single = mixed_source()
    single.params = {"components": [single.params["components"][0]]}
    project.sources.append(single)
    poi = poi_above(project)
    fdg_only = evaluate_point(project, poi)

    project.sources[0] = mixed_source()
    both = evaluate_point(project, poi)

    assert both.methods[0].total > fdg_only.methods[0].total
    assert both.governing_thickness_mm["lead"] > fdg_only.governing_thickness_mm["lead"]


def test_a_mix_reproduces_two_separate_co_located_sources():
    """The list is a convenience, not a different calculation."""
    combined_project = build_project()
    combined_project.materials = ["lead"]
    combined_project.sources.append(mixed_source())
    combined_project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, auto_height=True,
                        occupancy=1.0, linked_source_ids=["src1"])
    )

    separate = build_project()
    separate.materials = ["lead"]
    for index, entry in enumerate(mixed_source().params["components"], start=1):
        separate.sources.append(
            SourcePoint(id=f"s{index}", floor_id="fl1", x=0, y=0, method="tg108",
                        height_above_floor_m=1.0, params=entry)
        )
    separate.pois.append(
        PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, auto_height=True,
                        occupancy=1.0, linked_source_ids=["s1", "s2"])
    )

    a = evaluate_point(combined_project, combined_project.pois[0])
    b = evaluate_point(separate, separate.pois[0])
    assert a.methods[0].total == pytest.approx(b.methods[0].total, rel=1e-9)
    assert a.governing_thickness_mm["lead"] == pytest.approx(
        b.governing_thickness_mm["lead"], rel=1e-9
    )


def test_a_source_without_components_still_works():
    """Projects written before mixes existed keep calculating unchanged."""
    project = build_project()
    project.sources.append(uptake_source())
    poi = poi_above(project)
    result = evaluate_point(project, poi)
    assert result.methods[0].total == pytest.approx(117.0, rel=0.01)   # TG-108 Example 4
    assert len(result.contributions[0].components) == 1


def test_mixed_uptake_and_imaging_isotopes_in_one_region():
    """A region can run a tracer that is injected in the scanner alongside one
    that needs an uptake wait."""
    project = build_project()
    source = mixed_source(components=[
        {"kind": "uptake", "nuclide": "F-18", "administered_activity_MBq": 555,
         "patients_per_week": 40, "uptake_time_h": 1.0, "imaging_time_h": 0.5},
        {"kind": "imaging", "nuclide": "Rb-82", "administered_activity_MBq": 1480,
         "patients_per_week": 8, "uptake_time_h": 0.05, "imaging_time_h": 0.17,
         "void_factor": 1.0},
    ])
    project.sources.append(source)
    poi = poi_above(project)
    contribution = evaluate_point(project, poi).contributions[0]
    kinds = {c["nuclide"]: c["kind"] for c in contribution.components}
    assert kinds == {"F-18": "uptake", "Rb-82": "imaging"}
    # Rb-82 decays in 76 s, so despite the large activity it adds very little.
    by_nuclide = {c["nuclide"]: c["unshielded_uSv"] for c in contribution.components}
    assert by_nuclide["Rb-82"] < by_nuclide["F-18"]


def test_mixes_survive_a_save_and_reload(tmp_path):
    project = build_project()
    project.sources.append(mixed_source())
    path = save(project, tmp_path / "p.rsproj", {"plan.pdf": b"%PDF-1.4 fake"})
    reloaded, _ = load(path)
    components = reloaded.source("src1").params["components"]
    assert [c["nuclide"] for c in components] == ["F-18", "Ga-68"]
    assert components[1]["uptake_time_h"] == 0.75
