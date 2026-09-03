"""Elevation (cross-section) view: a vertical slice along one source-to-point path."""

from __future__ import annotations

import pytest

from radshield.model.geometry import GeometryError, elevation_profile
from radshield.model.project import Barrier, PointOfInterest

from .test_geometry_and_engine import build_project, uptake_source
from .test_walls import add_wall, horizontal_pair


def test_flat_path_has_zero_vertical_angle_and_full_horizontal_span():
    project = build_project()
    source, poi = horizontal_pair(project)
    profile = elevation_profile(project, source, poi)
    assert profile.source.horizontal_m == 0.0
    assert profile.target.horizontal_m == pytest.approx(8.0)
    assert profile.horizontal_total_m == pytest.approx(8.0)
    assert profile.vertical_angle_deg == pytest.approx(0.0)
    assert profile.source.height_m == pytest.approx(4.3 + 1.0)
    assert profile.target.height_m == pytest.approx(4.3 + 1.0)


def test_crossed_wall_is_placed_at_its_true_distance_and_height():
    project = build_project()
    add_wall(project, "fl1")  # x = 0, the midpoint of the 8 m horizontal_pair path
    source, poi = horizontal_pair(project)
    profile = elevation_profile(project, source, poi)
    assert len(profile.crossings) == 1
    crossing = profile.crossings[0]
    assert crossing.distance_along_m == pytest.approx(4.0)
    assert crossing.base_z_m == pytest.approx(4.3 + 0.0)
    assert crossing.top_z_m == pytest.approx(4.3 + 3.0)
    assert crossing.hit_height_m == pytest.approx(5.3)


def test_manually_declared_barriers_are_excluded_from_the_drawing():
    """A declared (not drawn) barrier has no position to plot in a cross-section."""
    project = build_project()
    source, poi = horizontal_pair(project)
    poi.manual_barriers[source.id] = [Barrier(material="lead", thickness_mm=2.0)]
    profile = elevation_profile(project, source, poi)
    assert profile.crossings == []


def test_a_climbing_path_is_reported_as_ninety_degrees_overhead():
    project = build_project()
    source = uptake_source(floor_id="fl0", x=0.0, y=0.0)
    poi = PointOfInterest(
        id="poi1", floor_id="fl2", x=0.0, y=0.0,
        auto_height=False, height_above_floor_m=1.0, linked_source_ids=["src1"],
    )
    profile = elevation_profile(project, source, poi)
    assert profile.vertical_angle_deg == pytest.approx(90.0)
    assert profile.target.height_m > profile.source.height_m


def test_coincident_source_and_point_raises():
    project = build_project()
    source = uptake_source(floor_id="fl1", x=0.0, y=0.0)
    poi = PointOfInterest(
        id="poi1", floor_id="fl1", x=0.0, y=0.0,
        auto_height=False, height_above_floor_m=1.0, linked_source_ids=["src1"],
    )
    with pytest.raises(GeometryError):
        elevation_profile(project, source, poi)


def test_floors_are_listed_sorted_by_elevation():
    project = build_project()
    source, poi = horizontal_pair(project)
    profile = elevation_profile(project, source, poi)
    names = [name for name, _ in profile.floors]
    assert names == ["Below", "Source", "Above"]
