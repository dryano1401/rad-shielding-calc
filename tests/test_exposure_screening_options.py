"""Screening options on the worst-case map: which goal, and what barrier to assume."""

from __future__ import annotations

import pytest

from radshield.engine.exposure import LEAD_GAUGES_MM, exposure_map
from radshield.model.geometry import path_barriers
from radshield.model.project import PointOfInterest, Project, SourcePoint, TrialBarrier

from .test_geometry_and_engine import build_project


def carm() -> SourcePoint:
    return SourcePoint(
        id="src1", floor_id="fl1", x=300.0, y=200.0, label="C-arm",
        method="carm", height_above_floor_m=1.0,
        params={"kvp": 100, "kap_week_mGy_cm2": 9.648e5,
                "field_area_cm2": 900.0, "field_distance_m": 1.0},
    )


@pytest.fixture
def project():
    p = build_project()
    p.materials = ["lead"]
    p.sources.append(carm())
    return p


def peak(project, **kwargs) -> float:
    return exposure_map(project, "fl1", columns=16, **kwargs).worst_ratio


# --- the design goal ----------------------------------------------------


def test_the_controlled_goal_is_exactly_five_times_looser(project):
    """P is 0.1 mGy/week controlled against 0.02 uncontrolled, so every ratio
    on the map scales by the same 5 -- nothing else about the cell changes."""
    assert peak(project, area_class="uncontrolled") == pytest.approx(
        5 * peak(project, area_class="controlled"), rel=1e-9)


def test_the_goal_the_map_was_drawn_against_is_recorded_on_it(project):
    """A screenshot read against the wrong goal is wrong by 5x, so the map has
    to carry which one it used."""
    grid = exposure_map(project, "fl1", columns=8, area_class="controlled")
    assert grid.area_class == "controlled"


# --- the trial barrier --------------------------------------------------


def test_a_trial_barrier_is_added_to_every_path(project):
    project.trial_barrier = TrialBarrier("Lead", 1.58)
    poi = PointOfInterest(
        id="poi1", floor_id="fl1", x=400.0, y=200.0, occupancy=1.0,
        area_class="uncontrolled", height_above_floor_m=1.7,
        offset_applied=True, linked_source_ids=["src1"],
    )
    crossings, _ = path_barriers(project, project.sources[0], poi)
    trial = [c for c in crossings if c.label.startswith("trial ")]
    assert len(trial) == 1
    assert trial[0].material == "Lead"
    assert trial[0].thickness_mm == pytest.approx(1.58)
    # Notional, so no obliquity is applied to it.
    assert trial[0].effective_thickness_mm == pytest.approx(trial[0].thickness_mm)


@pytest.mark.parametrize("gauge", list(LEAD_GAUGES_MM))
def test_each_lead_gauge_lowers_the_peak(project, gauge):
    bare = peak(project)
    assert peak(project, trial_material="Lead",
                trial_thickness_mm=LEAD_GAUGES_MM[gauge]) < bare


def test_thicker_lead_always_leaves_less(project):
    gauges = sorted(LEAD_GAUGES_MM.values())
    peaks = [peak(project, trial_material="Lead", trial_thickness_mm=mm) for mm in gauges]
    assert peaks == sorted(peaks, reverse=True)


def test_gypsum_shields_far_less_than_lead_for_the_same_millimetre(project):
    """A sanity check that the material is actually reaching the fits rather
    than being taken as a thickness of something generic."""
    lead = peak(project, trial_material="Lead", trial_thickness_mm=16.0)
    gypsum = peak(project, trial_material="Gypsum Wallboard", trial_thickness_mm=16.0)
    assert gypsum > 1000 * lead


def test_concrete_sits_between_the_two(project):
    lead = peak(project, trial_material="Lead", trial_thickness_mm=16.0)
    concrete = peak(project, trial_material="Concrete", trial_thickness_mm=16.0)
    gypsum = peak(project, trial_material="Gypsum Wallboard", trial_thickness_mm=16.0)
    assert lead < concrete < gypsum


def test_a_zero_thickness_trial_is_the_same_as_none(project):
    assert peak(project, trial_material="Lead", trial_thickness_mm=0.0) == pytest.approx(
        peak(project))


def test_the_trial_barrier_is_recorded_on_the_map(project):
    grid = exposure_map(project, "fl1", columns=8,
                        trial_material="Lead", trial_thickness_mm=1.58)
    assert grid.trial_material == "Lead"
    assert grid.trial_thickness_mm == pytest.approx(1.58)


# --- it must not escape the screening overlay ---------------------------


def test_a_trial_barrier_is_never_saved_with_the_project():
    """It is an assumption held while looking at a map. Persisting it would
    shield every placed point with a barrier nobody built."""
    p = Project(trial_barrier=TrialBarrier("Lead", 3.17))
    assert "trial_barrier" not in p.to_dict()
    assert Project.from_dict(p.to_dict()).trial_barrier is None


def test_mapping_does_not_leave_a_trial_barrier_on_the_real_project(project):
    """The map works on a copy; the caller's project must come back untouched
    or the next Calculate would quietly include the barrier."""
    exposure_map(project, "fl1", columns=8,
                 trial_material="Lead", trial_thickness_mm=3.17)
    assert project.trial_barrier is None


# --- what a zero in the thickness column means --------------------------


def test_a_shielded_path_needs_nothing_added_and_an_unshielded_one_does():
    """The thickness columns report what to add *beyond* the drawn barriers.

    A wall already carrying enough lead leaves zero to add, which is a result
    rather than a failure to compute one -- so the two cases are pinned
    together to keep a real regression distinguishable from that.
    """
    from radshield.model.project import Barrier, PointOfInterest

    project = build_project()
    project.materials = ["lead", "concrete"]
    project.sources.append(carm())

    def point(poi_id, barriers):
        poi = PointOfInterest(
            id=poi_id, floor_id="fl1", x=330.0, y=200.0, auto_height=False,
            height_above_floor_m=1.0, occupancy=1.0, area_class="uncontrolled",
            offset_applied=True, linked_source_ids=["src1"],
            manual_barriers={"src1": barriers} if barriers else {},
        )
        from radshield.engine.evaluate import evaluate_point
        return evaluate_point(project, poi)

    bare = point("bare", [])
    assert bare.governing_thickness_mm["lead"] > 0
    assert bare.governing_thickness_mm["concrete"] > 0

    lined = point("lined", [Barrier(material="lead", thickness_mm=3.17, label="1/8 in")])
    assert lined.governing_thickness_mm["lead"] == 0.0
    assert lined.governing_thickness_mm["concrete"] == 0.0
    # Zero because the point is under its goal, not because nothing was solved.
    governing = lined.methods[-1]
    assert governing.required_transmission > 1.0
    assert not governing.unavailable
