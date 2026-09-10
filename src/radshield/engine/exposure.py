"""Worst-case exposure mapping: which parts of a floor need looking at.

A point-by-point assessment answers "is this spot within its goal".  Deciding
*where to put the points* is the part that costs a reviewer time, and it is
normally done by eye from the plan.  This does it by exhaustion instead: solve
a grid of locations under deliberately unfavourable assumptions and colour the
result, so the areas with no chance of exceeding a goal can be set aside and
the effort spent on the ones that might.

The map is a screening tool and nothing more.  What makes it usable as one is
that every cell goes through :func:`evaluate_point` -- the same function a
placed point of interest uses, with the same distances, the same barrier
crossings and the same physics.  A cell is not a cheaper approximation of a
point; it is a point.  So a region the map calls clear cannot hide a placed
point that is not, which is the only property that makes it safe to skip
anything on the strength of it.

"Worst case" means, per cell:

* occupancy T = 1, however the space is really used;
* the uncontrolled design goal, the more stringent of the two;
* no NCRP standoff, so the point sits where the cell is rather than 0.3 m
  further away;
* every source in the project contributing, not a chosen subset.

Each of those pushes the answer up.  A cell under its goal here is under its
goal for any milder set of assumptions, which is what lets the clear regions
be dismissed rather than merely deprioritised.

Two of those assumptions can be relaxed deliberately, because a reviewer
often wants a different question answered.  Switching to the controlled goal
asks what a staff-only area needs; that goal is five times looser, so a map
read against it is *not* a bound on an uncontrolled space and the two must not
be confused.  A trial barrier asks what a candidate specification would leave
hot -- lay 1/16 inch of lead on every path and see what stays red -- which
turns the map from "where is the problem" into "does this fix it".  Both are
recorded on the returned map so a screenshot cannot be read against the wrong
assumption.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

from ..model.project import PointOfInterest, Project, TrialBarrier
from .evaluate import evaluate_point

# Thresholds the map's colours are cut at, as a fraction of the design goal.
# The upper one is the goal itself.  The lower is NCRP-adjacent practice for
# "close enough to want a look": a cell between them is under its goal today
# but has little room for a workload increase or a geometry correction.
OVER_GOAL = 1.0
TIGHT_FRACTION = 2.0 / 3.0

# Height above the floor a mapped cell is taken at, when the source is on the
# same floor.  Cross-floor cells follow the TG-108 Fig. 5 conventions instead,
# exactly as a placed point does.
DEFAULT_MAP_HEIGHT_M = 1.7

# Sheet lead is sold by weight, so the gauges a drawing actually calls out are
# these, not round millimetres.  1 lb/ft2 is nominally 1/64 inch.
LEAD_GAUGES_MM = {
    "1/32 in": 0.79,
    "1/16 in": 1.58,
    "1/8 in": 3.17,
}


@dataclass
class ExposureMap:
    """A grid of worst-case dose over one floor, as a fraction of its goal.

    ``ratios[row][column]`` is dose divided by the design goal, so 1.0 is
    exactly at the goal.  None marks a cell that could not be solved -- no
    transmission data for a material on the path, or a geometry failure --
    which is shown as a gap rather than as a low number.
    """

    floor_id: str
    floor_name: str
    columns: int
    rows: int
    origin_x: float
    origin_y: float
    step: float
    ratios: list[list[float | None]]
    height_m: float
    occupancy: float
    area_class: str
    source_count: int
    over_goal: float = OVER_GOAL
    tight_fraction: float = TIGHT_FRACTION
    trial_material: str = ""
    trial_thickness_mm: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def worst_ratio(self) -> float:
        """The highest ratio anywhere on the grid, or 0 if nothing solved."""
        values = [v for row in self.ratios for v in row if v is not None]
        return max(values) if values else 0.0


def _ratio(result) -> float | None:
    """Dose as a fraction of the goal, from a solved point.

    Taken as ``1 / required_transmission`` -- the same quantity the CSV export
    reports as "% of goal", rather than a second definition that could drift
    from it.
    """
    governing = next((m for m in result.methods if m.method == "combined"), None)
    if governing is None:
        governing = result.methods[0] if result.methods else None
    if governing is None:
        return None
    transmission = governing.required_transmission
    if transmission == float("inf"):
        return 0.0
    if transmission <= 0:
        return None
    return 1.0 / transmission


def exposure_map(
    project: Project,
    floor_id: str,
    *,
    columns: int = 90,
    height_m: float = DEFAULT_MAP_HEIGHT_M,
    occupancy: float = 1.0,
    area_class: str = "uncontrolled",
    trial_material: str = "",
    trial_thickness_mm: float = 0.0,
) -> ExposureMap:
    """Solve a grid of worst-case points across one floor's drawing.

    The grid spans the floor's page, squared to the page's aspect so cells are
    square in PDF space and therefore square on the drawing.

    Args:
        columns: Cells across the page.  Cost is quadratic in this and every
            cell is a full point solve, so it trades resolution against the
            wait before the overlay appears.
        area_class: Which design goal the ratios are against.  "controlled" is
            the looser of the two, so a map drawn against it says nothing
            about an uncontrolled space.
        trial_material: Material of a hypothetical barrier added to every
            path.  Empty means none.
        trial_thickness_mm: Thickness of that barrier.
    """
    floor = project.floor(floor_id)
    warnings: list[str] = []

    if floor.calibration is None:
        warnings.append(
            f"floor {floor.name!r} has no scale, so no distance on it can be resolved"
        )
    source_ids = [s.id for s in project.sources]
    if not source_ids:
        warnings.append("this project has no sources, so there is nothing to map")

    width = floor.page_width or 0.0
    height = floor.page_height or 0.0
    if width <= 0 or height <= 0:
        warnings.append(f"floor {floor.name!r} has no page size to map over")
        return ExposureMap(
            floor_id=floor_id, floor_name=floor.name, columns=0, rows=0,
            origin_x=0.0, origin_y=0.0, step=0.0, ratios=[], height_m=height_m,
            occupancy=occupancy, area_class=area_class,
            source_count=len(source_ids), warnings=warnings,
        )

    columns = max(2, min(int(columns), 400))
    step = width / columns
    rows = max(2, int(math.ceil(height / step)))

    # Materials are trimmed to one: the map needs each cell's dose against its
    # goal, not a thickness per material, and solving every material at every
    # cell would multiply the wait for a number nothing here reads.
    probe_project = copy.copy(project)
    probe_project.materials = project.materials[:1] or ["lead"]
    if trial_material and trial_thickness_mm > 0:
        probe_project.trial_barrier = TrialBarrier(
            material=trial_material, thickness_mm=float(trial_thickness_mm)
        )
    else:
        probe_project.trial_barrier = None
        trial_material, trial_thickness_mm = "", 0.0

    ratios: list[list[float | None]] = []
    unsolved = 0
    for row in range(rows):
        line: list[float | None] = []
        for column in range(columns):
            if not source_ids or floor.calibration is None:
                line.append(None)
                continue
            poi = PointOfInterest(
                id="__exposure_probe__",
                floor_id=floor_id,
                x=(column + 0.5) * step,
                y=(row + 0.5) * step,
                occupancy=occupancy,
                area_class=area_class,
                height_above_floor_m=height_m,
                # Cross-floor cells take the TG-108 Fig. 5 heights, as a
                # placed point would; on the source's own floor this leaves
                # height_above_floor_m standing.
                auto_height=True,
                # The cell *is* the point of protection, so nothing is added
                # to push it further from the source.
                offset_applied=True,
                linked_source_ids=list(source_ids),
            )
            try:
                value = _ratio(evaluate_point(probe_project, poi))
            except Exception:  # a cell that cannot be solved is a gap, not a zero
                value = None
            if value is None:
                unsolved += 1
            line.append(value)
        ratios.append(line)

    if unsolved:
        warnings.append(
            f"{unsolved} of {columns * rows} cells could not be solved and are "
            "left blank rather than being drawn as low"
        )

    return ExposureMap(
        floor_id=floor_id,
        floor_name=floor.name,
        columns=columns,
        rows=rows,
        origin_x=0.0,
        origin_y=0.0,
        step=step,
        ratios=ratios,
        height_m=height_m,
        occupancy=occupancy,
        area_class=area_class,
        source_count=len(source_ids),
        trial_material=trial_material,
        trial_thickness_mm=float(trial_thickness_mm),
        warnings=warnings,
    )
