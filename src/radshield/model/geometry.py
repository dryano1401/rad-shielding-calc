"""Converting placed points into the distances the physics engines need.

Three things happen here, and each is reported in the audit trail because each
is a modelling choice a reviewer may want to check:

1. **Scale.** Each floor converts its own PDF-space offsets to metres using
   its own calibration, since drawings may be plotted at different scales.
2. **Alignment.** Cross-floor horizontal distance is only meaningful once the
   drawings share an origin.  Each floor's alignment point marks the same
   physical feature; offsets are measured from it.  Without alignment points
   the drawings are assumed co-registered and a warning is emitted.
3. **Height conventions.** TG-108 Fig. 5 places the source 1 m above its floor
   and the protected point 0.5 m above the floor above, or 1.7 m above the
   floor below.  NCRP 147 places the point of protection 0.3 m beyond the
   barrier.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .project import (
    LENGTH_UNITS,
    Floor,
    Measurement,
    PointOfInterest,
    Project,
    SourcePoint,
    Wall,
)

# TG-108 Fig. 5 conventions.
SOURCE_HEIGHT_M = 1.0
TARGET_HEIGHT_ABOVE_M = 0.5
TARGET_HEIGHT_BELOW_M = 1.7
# NCRP 147 point of protection standoff from the distal barrier surface.
NCRP_STANDOFF_M = 0.3


class GeometryError(ValueError):
    """Raised when a distance cannot be computed from the placed geometry."""


@dataclass
class Distance:
    """A source-to-point distance with its full derivation.

    ``metres`` is the value the calculation uses.  When an override is in
    force that is the entered figure, and ``geometric_m`` retains what the
    placed geometry would have given, so a report can show both.
    """

    metres: float
    horizontal_m: float
    vertical_m: float
    same_floor: bool
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    geometric_m: float | None = None

    @property
    def is_overridden(self) -> bool:
        """True when an entered distance replaced the geometric one."""
        return self.geometric_m is not None


def format_length(metres: float, unit: str = "ft") -> str:
    """Render a length in the requested display unit, with metres alongside.

    Architectural drawings are usually dimensioned in feet while the physics
    works in metres, so both are shown rather than forcing a mental conversion.
    """
    if unit == "m":
        return f"{metres:.2f} m"
    converted = metres / LENGTH_UNITS[unit]
    if unit == "ft":
        total_inches = round(converted * 12.0, 1)
        feet, inches = divmod(total_inches, 12.0)
        return f"{int(feet)}' {inches:.1f}\" ({metres:.2f} m)"
    return f"{converted:.2f} {unit} ({metres:.2f} m)"


def measure(floor: Floor, p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Real-world distance in metres between two points on one drawing.

    This is a straight scale conversion within a single floor, so no alignment
    point is needed -- only the calibration.
    """
    if floor.calibration is None:
        raise GeometryError(
            f"floor {floor.name!r} has no scale calibration; set the scale before measuring"
        )
    scale = floor.calibration.metres_per_unit
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1]) * scale


def measurement_length(floor: Floor, item: Measurement) -> float:
    """Real-world length of a stored measurement, in metres."""
    return measure(floor, item.p1, item.p2)


def floor_frame(floor: Floor) -> tuple[float, float, float, float, float, list[str]]:
    """The frame a floor's reference features define, for placing its points.

    Returns ``(origin_x, origin_y, along_x, along_y, scale, warnings)`` where
    ``along`` is a unit vector in PDF space pointing from the first reference
    feature to the second.

    With two features the frame carries the drawing's rotation, so sheets laid
    out at different orientations still resolve to the same real-world
    positions.  With one, the drawing is assumed square to the page, which is
    only right when every sheet happens to share an orientation.  With none,
    the page origin is assumed common, which is weaker still.  Each fallback
    says so.
    """
    if floor.calibration is None:
        raise GeometryError(
            f"floor {floor.name!r} has no scale calibration; calibrate it before calculating"
        )
    scale = floor.calibration.metres_per_unit
    warnings: list[str] = []

    if floor.alignment is None:
        warnings.append(
            f"floor {floor.name!r} has no alignment point; its drawing origin is assumed "
            "co-registered with the other floors. Cross-floor horizontal distances may be wrong."
        )
        return 0.0, 0.0, 1.0, 0.0, scale, warnings

    origin_x, origin_y = floor.alignment
    if floor.alignment2 is None:
        warnings.append(
            f"floor {floor.name!r} has only one alignment point, which fixes where its drawing "
            "sits but not how it is turned. Add a second reference feature if the sheets are "
            "laid out at different orientations."
        )
        return origin_x, origin_y, 1.0, 0.0, scale, warnings

    dx = floor.alignment2[0] - origin_x
    dy = floor.alignment2[1] - origin_y
    span = math.hypot(dx, dy)
    if span < 1e-9:
        warnings.append(
            f"floor {floor.name!r} has both alignment points in the same place, so they cannot "
            "fix its rotation; the drawing is assumed square to the page."
        )
        return origin_x, origin_y, 1.0, 0.0, scale, warnings

    return origin_x, origin_y, dx / span, dy / span, scale, warnings


def floor_offset_m(floor: Floor, x: float, y: float) -> tuple[float, float, list[str]]:
    """Convert a PDF-space point to metres in the frame the floor's features define.

    Returns:
        ``(along_m, across_m, warnings)``.  The axes run along the line joining
        the two reference features and perpendicular to it.  PDF space has y
        increasing downward, so the perpendicular is taken as ``(dy, -dx)`` to
        keep a conventional plan handedness; with a single reference feature
        this reduces to plain east and north.
    """
    origin_x, origin_y, along_x, along_y, scale, warnings = floor_frame(floor)
    local_x = x - origin_x
    local_y = y - origin_y
    along = (local_x * along_x + local_y * along_y) * scale
    across = (local_x * along_y - local_y * along_x) * scale
    return along, across, warnings


def alignment_span_m(floor: Floor) -> float | None:
    """Real distance between a floor's two reference features, if it has two."""
    if not floor.is_oriented or floor.calibration is None:
        return None
    dx = floor.alignment2[0] - floor.alignment[0]
    dy = floor.alignment2[1] - floor.alignment[1]
    return math.hypot(dx, dy) * floor.calibration.metres_per_unit


def target_height(source_floor: Floor, target_floor: Floor, poi: PointOfInterest) -> tuple[float, str]:
    """Return the protected point's height above its own floor, and why.

    Applies the TG-108 Fig. 5 conventions when ``poi.auto_height`` is set.
    """
    if not poi.auto_height:
        return poi.height_above_floor_m, "height entered manually"
    if target_floor.elevation_m > source_floor.elevation_m:
        return TARGET_HEIGHT_ABOVE_M, "TG-108 Fig. 5: 0.5 m above the floor of the room above"
    if target_floor.elevation_m < source_floor.elevation_m:
        return TARGET_HEIGHT_BELOW_M, "TG-108 Fig. 5: 1.7 m above the floor of the room below"
    return poi.height_above_floor_m, "same floor; height taken from the point"


def distance(
    project: Project,
    source: SourcePoint,
    poi: PointOfInterest,
    *,
    apply_ncrp_standoff: bool = False,
    vertical_only: bool = False,
    override_m: float | None = None,
) -> Distance:
    """Compute the source-to-point distance in metres.

    Args:
        project: Owning project, for floor lookup.
        source: The source point.
        poi: The protected point.
        apply_ncrp_standoff: Add the 0.3 m NCRP 147 standoff when the placed
            coordinate marks the barrier rather than the protected point.  The
            caller passes ``not poi.offset_applied`` for NCRP 147 sources.
        vertical_only: Ignore horizontal separation.  Appropriate for a point
            directly above or below the source, which is what TG-108's floor
            and ceiling examples assume.
        override_m: Distance entered by the user, replacing the geometric
            result.  The standoff is not added on top of an override, since an
            entered distance is taken to be the final source-to-point figure.

    Returns:
        A :class:`Distance` carrying the components, notes and any warnings.
    """
    source_floor = project.floor(source.floor_id)
    poi_floor = project.floor(poi.floor_id)

    notes: list[str] = []
    warnings: list[str] = []

    sx, sy, w1 = floor_offset_m(source_floor, source.x, source.y)
    px, py, w2 = floor_offset_m(poi_floor, poi.x, poi.y)
    warnings.extend(w1)
    warnings.extend(w2)

    same_floor = source.floor_id == poi.floor_id
    horizontal = math.hypot(px - sx, py - sy)

    poi_height, height_reason = target_height(source_floor, poi_floor, poi)
    notes.append(height_reason)
    source_z = source_floor.elevation_m + source.height_above_floor_m
    poi_z = poi_floor.elevation_m + poi_height
    vertical = poi_z - source_z
    notes.append(
        f"source at {source_z:.2f} m, protected point at {poi_z:.2f} m above project datum"
    )

    if vertical_only:
        if same_floor:
            raise GeometryError(
                "vertical-only distance is meaningless for two points on the same floor"
            )
        horizontal = 0.0
        notes.append("horizontal separation ignored (vertical-only mode)")

    result = math.hypot(horizontal, vertical)

    if apply_ncrp_standoff and override_m is None:
        result += NCRP_STANDOFF_M
        notes.append(
            f"NCRP 147 standoff of {NCRP_STANDOFF_M} m added: the placed point marks the "
            "barrier, not the point of protection"
        )

    geometric = None
    if override_m is not None:
        if override_m <= 0:
            raise GeometryError(f"entered distance must be positive, got {override_m}")
        geometric = result
        notes.append(
            f"distance entered manually as {override_m:.3f} m, replacing the {geometric:.3f} m "
            "derived from the placed geometry"
        )
        if geometric > 0 and abs(override_m - geometric) / geometric > 0.25:
            warnings.append(
                f"entered distance {override_m:.2f} m differs from the drawing geometry "
                f"({geometric:.2f} m) by more than 25%; check it is intended"
            )
        result = override_m

    if result <= 0 and override_m is None:
        raise GeometryError(
            f"source {source.label or source.id!r} and point {poi.label or poi.id!r} are "
            "coincident; move one of them"
        )

    if same_floor and abs(vertical) < 1e-9 and horizontal < 0.5:
        warnings.append(
            f"{poi.label or poi.id!r} is only {horizontal:.2f} m from "
            f"{source.label or source.id!r}; check the placement and the floor scale"
        )

    return Distance(
        metres=result,
        horizontal_m=horizontal,
        vertical_m=vertical,
        same_floor=same_floor,
        notes=notes,
        warnings=warnings,
        geometric_m=geometric,
    )


@dataclass
class Crossing:
    """A barrier a source-to-point path passes through."""

    material: str
    thickness_mm: float
    effective_thickness_mm: float
    label: str
    angle_deg: float = 0.0
    wall_id: str | None = None
    floor_name: str = ""

    @property
    def is_oblique(self) -> bool:
        """True when the obliquity correction changed the traversed thickness."""
        return abs(self.effective_thickness_mm - self.thickness_mm) > 1e-9


def world_point(project: Project, floor: Floor, x: float, y: float, height_m: float
                ) -> tuple[float, float, float]:
    """Convert a point on a floor to project world coordinates in metres."""
    east, north, _ = floor_offset_m(floor, x, y)
    return east, north, floor.elevation_m + height_m


def wall_crossing(
    project: Project,
    floor: Floor,
    wall: Wall,
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    *,
    apply_obliquity: bool = False,
) -> Crossing | None:
    """Return the crossing if the segment ``start``-``end`` passes through ``wall``.

    The wall is treated as a vertical rectangle: its plan segment extruded
    between its base and top heights.  This is what makes a single test work
    for paths within a storey and paths between storeys -- a partition that
    stops at 3 m simply is not in the way of a ray that has already climbed
    above it.

    Args:
        apply_obliquity: Scale the traversed thickness by ``1 / cos(theta)``
            for a path crossing at ``theta`` from the wall normal.  Off by
            default, which under-counts material and so errs safe.

    Returns:
        The crossing, or None when the path misses the wall.
    """
    ax, ay, _ = world_point(project, floor, wall.p1[0], wall.p1[1], 0.0)
    bx, by, _ = world_point(project, floor, wall.p2[0], wall.p2[1], 0.0)

    wall_dx, wall_dy = bx - ax, by - ay
    wall_length = math.hypot(wall_dx, wall_dy)
    if wall_length < 1e-9:
        return None

    # Horizontal normal of the wall plane.
    nx, ny = -wall_dy / wall_length, wall_dx / wall_length

    dx = end[0] - start[0]
    dy = end[1] - start[1]
    dz = end[2] - start[2]

    denominator = dx * nx + dy * ny
    if abs(denominator) < 1e-12:
        # The path runs parallel to the wall, so it never crosses it.
        return None

    t = ((ax - start[0]) * nx + (ay - start[1]) * ny) / denominator
    if not 0.0 <= t <= 1.0:
        return None

    hit_x = start[0] + t * dx
    hit_y = start[1] + t * dy
    hit_z = start[2] + t * dz

    # Within the wall's extent in plan?
    along = ((hit_x - ax) * wall_dx + (hit_y - ay) * wall_dy) / (wall_length**2)
    if not 0.0 <= along <= 1.0:
        return None

    # Within the wall's height band?
    base_z = floor.elevation_m + wall.base_height_m
    top_z = floor.elevation_m + wall.top_height_m
    if not base_z <= hit_z <= top_z:
        return None

    path_length = math.sqrt(dx * dx + dy * dy + dz * dz)
    cos_theta = abs(dx * nx + dy * ny) / path_length if path_length else 1.0
    angle = math.degrees(math.acos(min(max(cos_theta, -1.0), 1.0)))

    effective = wall.thickness_mm
    if apply_obliquity and cos_theta > 1e-6:
        effective = wall.thickness_mm / cos_theta

    return Crossing(
        material=wall.material,
        thickness_mm=wall.thickness_mm,
        effective_thickness_mm=effective,
        label=wall.label or f"{wall.material} wall",
        angle_deg=angle,
        wall_id=wall.id,
        floor_name=floor.name,
    )


def path_barriers(
    project: Project,
    source: SourcePoint,
    poi: PointOfInterest,
    *,
    apply_obliquity: bool = False,
) -> tuple[list[Crossing], list[str]]:
    """Every barrier between a source and a point: walls crossed, plus named ones.

    Walls on *all* floors are tested, not just the source's or the point's.
    The height band does the filtering, so a wall only counts when the path
    genuinely runs through it.

    Returns:
        ``(crossings, warnings)``.
    """
    warnings: list[str] = []
    try:
        source_floor = project.floor(source.floor_id)
        poi_floor = project.floor(poi.floor_id)
    except KeyError as exc:
        return [], [str(exc)]

    poi_height, _ = target_height(source_floor, poi_floor, poi)
    start = world_point(project, source_floor, source.x, source.y, source.height_above_floor_m)
    end = world_point(project, poi_floor, poi.x, poi.y, poi_height)

    crossings: list[Crossing] = []
    for floor in project.floors:
        if floor.calibration is None:
            if floor.walls:
                warnings.append(
                    f"floor {floor.name!r} has walls but no scale calibration; they were ignored"
                )
            continue
        for wall in floor.walls:
            hit = wall_crossing(
                project, floor, wall, start, end, apply_obliquity=apply_obliquity
            )
            if hit is not None:
                crossings.append(hit)

    for barrier in poi.manual_barriers.get(source.id, []):
        crossings.append(
            Crossing(
                material=barrier.material,
                thickness_mm=barrier.thickness_mm,
                effective_thickness_mm=barrier.thickness_mm,
                label=barrier.label or f"{barrier.material} barrier",
            )
        )

    return crossings, warnings


@dataclass
class ChartDirection:
    """Where a point lies relative to a source's scatter chart.

    ``x_m`` and ``y_m`` are the point's position in the chart's own axes,
    which is what allows the chart to be read directly rather than projected
    along a bearing.
    """

    bearing_deg: float
    distance_m: float
    plane: str
    x_m: float = 0.0
    y_m: float = 0.0
    note: str = ""


def chart_direction(
    project: Project,
    source: SourcePoint,
    poi: PointOfInterest,
    plane: str = "plan",
) -> ChartDirection:
    """Bearing and distance from a source's isocentre to a point, in chart axes.

    The placed source point is the isocentre.  ``source.rotation_deg`` turns
    the chart's axes to match how the equipment sits on the plan.

    For a ``"plan"`` chart the bearing is measured in the horizontal plane,
    anticlockwise from the chart's +x axis.  For an ``"elevation"`` chart the
    chart's x axis is the table axis and its y axis is height, so the bearing
    is the angle above or below the isocentre plane, measured in the vertical
    plane that contains the table axis.

    The distance returned is always the true three-dimensional separation,
    since that is what the inverse-square correction must use.
    """
    source_floor = project.floor(source.floor_id)
    poi_floor = project.floor(poi.floor_id)

    sx, sy, _ = floor_offset_m(source_floor, source.x, source.y)
    px, py, _ = floor_offset_m(poi_floor, poi.x, poi.y)
    east, north = px - sx, py - sy

    poi_height, _ = target_height(source_floor, poi_floor, poi)
    rise = (poi_floor.elevation_m + poi_height) - (
        source_floor.elevation_m + source.height_above_floor_m
    )

    # Rotate the horizontal offset into the chart's frame.
    angle = math.radians(source.rotation_deg)
    local_x = east * math.cos(angle) + north * math.sin(angle)
    local_y = -east * math.sin(angle) + north * math.cos(angle)

    distance = math.sqrt(east**2 + north**2 + rise**2)
    if distance <= 0:
        raise GeometryError(
            f"{poi.label or poi.id!r} is at the isocentre of "
            f"{source.label or source.id!r}; move one of them"
        )

    if plane == "elevation":
        # The elevation chart runs along the table axis, which is the plan
        # chart's +y, so that component becomes the chart's horizontal axis.
        chart_x, chart_y = local_y, rise
        note = (
            f"elevation chart: {chart_x:+.2f} m along the table axis, "
            f"{chart_y:+.2f} m in height"
        )
    else:
        chart_x, chart_y = local_x, local_y
        note = f"plan chart: {chart_x:+.2f}, {chart_y:+.2f} m from the isocentre"
        if abs(rise) > 0.5:
            note += f", {rise:+.2f} m in height"

    return ChartDirection(
        bearing_deg=math.degrees(math.atan2(chart_y, chart_x)),
        distance_m=distance,
        plane=plane,
        x_m=chart_x,
        y_m=chart_y,
        note=note,
    )


def check_project(project: Project) -> list[str]:
    """Return a list of problems that would block or degrade a calculation."""
    problems: list[str] = []
    for floor in project.floors:
        if not floor.is_calibrated:
            problems.append(f"floor {floor.name!r} is not calibrated")
    if len({f.elevation_m for f in project.floors}) < len(project.floors):
        problems.append("two or more floors share the same elevation")
    multi_floor = len({p.floor_id for p in project.pois} | {s.floor_id for s in project.sources}) > 1
    if multi_floor:
        unaligned = [f.name for f in project.floors if f.alignment is None]
        if unaligned:
            problems.append(
                "points span more than one floor but these floors have no alignment point: "
                + ", ".join(unaligned)
            )
        unoriented = [
            f.name for f in project.floors if f.alignment is not None and f.alignment2 is None
        ]
        if unoriented:
            problems.append(
                "these floors have only one alignment point, which cannot fix how the drawing "
                "is turned: " + ", ".join(unoriented)
            )

        # Both features are the same two physical things, so every floor must
        # agree on how far apart they are. Disagreement means a bad scale or a
        # misplaced feature, and would quietly skew cross-floor geometry.
        spans = {
            f.name: alignment_span_m(f)
            for f in project.floors
            if alignment_span_m(f) is not None
        }
        if len(spans) > 1:
            shortest, longest = min(spans.values()), max(spans.values())
            if shortest > 0 and (longest - shortest) / shortest > 0.02:
                detail = ", ".join(f"{name} {span:.2f} m" for name, span in spans.items())
                problems.append(
                    "floors disagree on the distance between their two alignment points "
                    f"({detail}); check the scales and that the same two features were "
                    "marked on each"
                )
    for poi in project.pois:
        if not poi.linked_source_ids:
            problems.append(f"point {poi.label or poi.id!r} has no linked sources")
    return problems
