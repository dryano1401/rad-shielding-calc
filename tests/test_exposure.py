"""Worst-case exposure mapping: screening a floor for the areas worth points."""

from __future__ import annotations

import pytest

from radshield.engine.evaluate import evaluate_point
from radshield.engine.exposure import exposure_map
from radshield.model.project import PointOfInterest

from .test_geometry_and_engine import build_project, uptake_source
from .test_walls import add_wall


def mapped_project():
    """The standard three-floor fixture with one source and a page to map."""
    project = build_project()
    project.materials = ["lead"]
    for floor in project.floors:
        floor.page_width, floor.page_height = 600.0, 400.0
    project.sources.append(uptake_source(floor_id="fl1", x=300.0, y=200.0))
    return project


def test_the_map_covers_the_page_in_square_cells():
    project = mapped_project()
    result = exposure_map(project, "fl1", columns=30)
    assert result.columns == 30
    assert result.step == pytest.approx(600.0 / 30)
    # 400 units of page at the same step, rounded up.
    assert result.rows == 20
    assert len(result.ratios) == result.rows
    assert all(len(row) == result.columns for row in result.ratios)


def test_dose_falls_away_from_the_source():
    project = mapped_project()
    result = exposure_map(project, "fl1", columns=30)
    centre = result.ratios[result.rows // 2][result.columns // 2]
    corner = result.ratios[0][0]
    assert centre is not None and corner is not None
    assert centre > corner


def test_a_mapped_cell_agrees_with_a_point_placed_there():
    """The map is only usable for triage if a cell is the same calculation a
    placed point would be -- not a cheaper approximation of one."""
    project = mapped_project()
    result = exposure_map(project, "fl1", columns=20)

    row, column = 4, 6
    poi = PointOfInterest(
        id="check", floor_id="fl1",
        x=(column + 0.5) * result.step, y=(row + 0.5) * result.step,
        occupancy=1.0, area_class="uncontrolled", height_above_floor_m=1.7,
        auto_height=True, offset_applied=True, linked_source_ids=["src1"],
    )
    placed = evaluate_point(project, poi)
    governing = next((m for m in placed.methods if m.method == "combined"), placed.methods[0])
    assert result.ratios[row][column] == pytest.approx(1.0 / governing.required_transmission)


def test_a_wall_shows_up_as_a_step_in_the_map():
    """Shielding is what the map is for, so it has to be visible in it."""
    project = mapped_project()
    bare = exposure_map(project, "fl1", columns=40)
    add_wall(project, "fl1", p1=(320.0, 0.0), p2=(320.0, 400.0),
             material="lead", thickness_mm=6.0, top_height_m=3.0)
    shielded = exposure_map(project, "fl1", columns=40)

    # Sample well to the east of the wall, on the far side from the source.
    row, column = 10, 34
    assert bare.ratios[row][column] > shielded.ratios[row][column]


def test_a_project_with_no_sources_maps_nothing_and_says_so():
    project = build_project()
    for floor in project.floors:
        floor.page_width, floor.page_height = 600.0, 400.0
    result = exposure_map(project, "fl1", columns=10)
    assert all(value is None for row in result.ratios for value in row)
    assert any("no sources" in w for w in result.warnings)
    assert result.worst_ratio == 0.0


def test_an_uncalibrated_floor_is_reported_rather_than_guessed():
    project = mapped_project()
    project.floor("fl1").calibration = None
    result = exposure_map(project, "fl1", columns=10)
    assert all(value is None for row in result.ratios for value in row)
    assert any("no scale" in w for w in result.warnings)


def test_a_floor_with_no_page_size_returns_an_empty_map():
    project = mapped_project()
    project.floor("fl1").page_width = 0.0
    result = exposure_map(project, "fl1", columns=10)
    assert result.ratios == []
    assert any("no page size" in w for w in result.warnings)
