"""Registering drawings to each other with one and with two reference features.

One feature fixes only where a drawing sits.  Two also fix how it is turned,
which matters as soon as two sheets lay the building out at different
orientations -- and that affects the calculated cross-floor geometry, not just
what is drawn on screen.
"""

from __future__ import annotations

import math

import pytest

from radshield.model.geometry import (
    alignment_span_m,
    check_project,
    distance,
    floor_frame,
    floor_offset_m,
)
from radshield.model.project import Calibration, Floor, PointOfInterest, Project

from .test_geometry_and_engine import build_project, uptake_source


def rotated(point: tuple[float, float], degrees: float,
            about: tuple[float, float] = (0.0, 0.0)) -> tuple[float, float]:
    """Rotate a PDF-space point, as a sheet laid out at another orientation would."""
    angle = math.radians(degrees)
    dx, dy = point[0] - about[0], point[1] - about[1]
    return (
        about[0] + dx * math.cos(angle) - dy * math.sin(angle),
        about[1] + dx * math.sin(angle) + dy * math.cos(angle),
    )


def test_two_collinear_features_reproduce_the_single_point_frame():
    """Adding a second feature along +x must not move anything."""
    project = build_project()
    floor = project.floor("fl1")
    with_two = floor_offset_m(floor, 40.0, 25.0)[:2]

    floor.alignment2 = None
    with_one = floor_offset_m(floor, 40.0, 25.0)[:2]
    assert with_two == pytest.approx(with_one)


def test_one_feature_cannot_correct_a_rotated_sheet():
    """The failure this exists to fix, shown directly.

    The upper floor's sheet lays the building out turned 90 degrees.  A point
    at the same physical place therefore sits at different PDF coordinates,
    and with one reference feature the two floors disagree about where it is.
    """
    project = build_project()
    upper = project.floor("fl2")
    upper.alignment = (0.0, 0.0)
    upper.alignment2 = None

    lower_position = floor_offset_m(project.floor("fl1"), 40.0, 0.0)[:2]
    upper_position = floor_offset_m(upper, *rotated((40.0, 0.0), 90.0))[:2]
    assert lower_position != pytest.approx(upper_position)


def test_two_features_do_correct_a_rotated_sheet():
    """With both features marked, the same physical point resolves identically."""
    project = build_project()
    upper = project.floor("fl2")
    # The whole sheet, reference features included, is turned 90 degrees.
    upper.alignment = rotated((0.0, 0.0), 90.0)
    upper.alignment2 = rotated((100.0, 0.0), 90.0)

    lower_position = floor_offset_m(project.floor("fl1"), 40.0, 0.0)[:2]
    upper_position = floor_offset_m(upper, *rotated((40.0, 0.0), 90.0))[:2]
    assert lower_position == pytest.approx(upper_position, abs=1e-9)


@pytest.mark.parametrize("degrees", [0, 30, 90, 180, 270, 47.5])
def test_registration_holds_at_any_rotation(degrees):
    project = build_project()
    upper = project.floor("fl2")
    upper.alignment = rotated((0.0, 0.0), degrees)
    upper.alignment2 = rotated((100.0, 0.0), degrees)

    for point in [(40.0, 0.0), (-25.0, 60.0), (13.5, -42.0)]:
        expected = floor_offset_m(project.floor("fl1"), *point)[:2]
        got = floor_offset_m(upper, *rotated(point, degrees))[:2]
        assert got == pytest.approx(expected, abs=1e-9)


def test_a_sheet_at_a_different_scale_and_rotation_still_registers():
    """Rotation and scale together, which is the general case."""
    project = build_project()
    upper = project.floor("fl2")
    # Plotted at half the scale, so its calibration says twice the metres per unit.
    upper.calibration = Calibration(p1=(0, 0), p2=(100, 0), known_distance=20, unit="m")
    upper.alignment = rotated((0.0, 0.0), 60.0)
    upper.alignment2 = rotated((50.0, 0.0), 60.0)   # half as many units for the same run

    expected = floor_offset_m(project.floor("fl1"), 40.0, 20.0)[:2]
    got = floor_offset_m(upper, *rotated((20.0, 10.0), 60.0))[:2]
    assert got == pytest.approx(expected, abs=1e-9)


def test_cross_floor_distance_is_wrong_with_one_feature_and_right_with_two():
    """The registration reaches the physics, not only the drawing.

    A point placed at the very same physical spot as the source, one storey
    up, on a sheet turned 90 degrees.  Registered properly it sits directly
    overhead, 3.8 m away.  With a single reference feature the rotation goes
    uncorrected and the same point is reckoned 5.7 m off to the side.

    Note the source must not sit on the reference feature itself: rotating
    about a point leaves distances from that point unchanged, so the error
    would hide.
    """
    project = build_project()
    upper = project.floor("fl2")
    source = uptake_source(floor_id="fl1", x=40.0, y=0.0)

    placed = rotated((40.0, 0.0), 90.0)
    poi = PointOfInterest(id="poi1", floor_id="fl2", x=placed[0], y=placed[1],
                          auto_height=True)

    upper.alignment, upper.alignment2 = (0.0, 0.0), None
    with_one = distance(project, source, poi)

    upper.alignment = rotated((0.0, 0.0), 90.0)
    upper.alignment2 = rotated((100.0, 0.0), 90.0)
    with_two = distance(project, source, poi)

    assert with_two.horizontal_m == pytest.approx(0.0, abs=1e-9)
    assert with_two.metres == pytest.approx(3.8, abs=1e-9)

    assert with_one.horizontal_m == pytest.approx(math.hypot(4.0, 4.0), abs=1e-9)
    assert with_one.metres == pytest.approx(math.hypot(5.6569, 3.8), rel=1e-4)
    assert with_one.metres > 1.7 * with_two.metres


def test_frame_falls_back_and_says_so():
    project = build_project()
    floor = project.floor("fl1")

    floor.alignment2 = None
    assert any("not how it is turned" in w for w in floor_frame(floor)[5])

    floor.alignment = None
    assert any("no alignment point" in w for w in floor_frame(floor)[5])

    floor.alignment, floor.alignment2 = (10.0, 10.0), (10.0, 10.0)
    assert any("same place" in w for w in floor_frame(floor)[5])


def test_alignment_span_is_reported_for_verification():
    project = build_project()
    # 100 PDF units at 0.1 m per unit.
    assert alignment_span_m(project.floor("fl1")) == pytest.approx(10.0)
    project.floor("fl1").alignment2 = None
    assert alignment_span_m(project.floor("fl1")) is None


def test_floors_disagreeing_on_the_feature_separation_are_flagged():
    """The two features are the same two things, so every floor must agree.

    A mismatch means a wrong scale or a misplaced feature, either of which
    would quietly skew every cross-floor distance.
    """
    project = build_project()
    project.sources.append(uptake_source())
    project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, linked_source_ids=["src1"])
    )
    assert not any("disagree on the distance" in p for p in check_project(project))

    # Same two features marked 20% further apart on one floor.
    project.floor("fl2").alignment2 = (120.0, 0.0)
    problems = check_project(project)
    assert any("disagree on the distance" in p for p in problems)
    assert any("10.00 m" in p and "12.00 m" in p for p in problems)


def test_a_floor_with_only_one_feature_is_flagged_when_points_span_floors():
    project = build_project()
    project.floor("fl2").alignment2 = None
    project.sources.append(uptake_source())
    project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, linked_source_ids=["src1"])
    )
    assert any("cannot fix how the drawing is turned" in p for p in check_project(project))


def test_registration_survives_a_save_and_reload(tmp_path):
    from radshield.model.store import load, save

    project = build_project()
    project.floor("fl2").alignment = (12.0, 34.0)
    project.floor("fl2").alignment2 = (56.0, 78.0)
    path = save(project, tmp_path / "p.rsproj", {"plan.pdf": b"%PDF-1.4 fake"})
    reloaded, _ = load(path)
    floor = reloaded.floor("fl2")
    assert floor.alignment == (12.0, 34.0)
    assert floor.alignment2 == (56.0, 78.0)
    assert floor.is_oriented


def test_projects_saved_before_the_second_feature_existed_still_load():
    project = build_project()
    data = project.to_dict()
    for floor in data["floors"]:
        floor.pop("alignment2")
    restored = Project.from_dict(data)
    assert restored.floor("fl1").alignment == (0.0, 0.0)
    assert restored.floor("fl1").alignment2 is None
    assert not restored.floor("fl1").is_oriented
