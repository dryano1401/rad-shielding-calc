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
