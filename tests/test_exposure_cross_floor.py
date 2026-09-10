"""The worst-case map covers floors above and below the source, not just its own."""

from __future__ import annotations

import pytest

from radshield.engine.exposure import exposure_map
from radshield.model.project import SourcePoint

from .test_geometry_and_engine import build_project


def carm_on_source_floor() -> SourcePoint:
    return SourcePoint(
        id="src1", floor_id="fl1", x=300.0, y=200.0, label="C-arm",
        method="carm", height_above_floor_m=1.0,
        params={"kvp": 100, "kap_week_mGy_cm2": 9.648e5,
                "field_area_cm2": 900.0, "field_distance_m": 1.0},
    )


@pytest.fixture
def project():
    p = build_project()          # fl0 Below, fl1 Source, fl2 Above
    p.materials = ["lead"]
    p.sources.append(carm_on_source_floor())
    return p


@pytest.mark.parametrize("floor_id,name", [("fl2", "above"), ("fl0", "below")])
def test_an_adjacent_floor_is_mapped_from_a_source_it_does_not_hold(project, floor_id, name):
    """A floor with no source of its own is still exposed through the slab, so
    its grid has to solve rather than come back empty."""
    grid = exposure_map(project, floor_id, columns=24)
    solved = [v for row in grid.ratios for v in row if v is not None]

    assert grid.warnings == []
    assert len(solved) == grid.columns * grid.rows
    assert grid.worst_ratio > 1.0, f"floor {name} shows no exposure at all"


def test_the_source_floor_is_the_hottest_but_not_the_only_one(project):
    """Sanity on the vertical fall-off: the slab and the extra distance cut the
    adjacent floors well below the source floor without clearing them."""
    same = exposure_map(project, "fl1", columns=24).worst_ratio
    above = exposure_map(project, "fl2", columns=24).worst_ratio
    below = exposure_map(project, "fl0", columns=24).worst_ratio

    assert same > above and same > below
    assert above > 1.0 and below > 1.0


def test_every_floor_reports_against_the_same_goal(project):
    """The ratios are comparable across floors only if the goal is, so a cell
    on one floor can be read against a cell on another."""
    grids = [exposure_map(project, f, columns=8) for f in ("fl0", "fl1", "fl2")]
    assert len({g.area_class for g in grids}) == 1
    assert len({g.occupancy for g in grids}) == 1
    assert all(g.source_count == 1 for g in grids)
