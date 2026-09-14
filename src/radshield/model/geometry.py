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
   barrier -- and TG-108's own default source-to-wall/floor distances are
   drawn from the same NCRP guidance, so this standoff applies to both
   methodologies alike, not just NCRP 147 sources.
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

# How close a path may pass to a wall's edge -- its top, its base, or either
# end -- before the crossing stops being credited as shielding.
#
# Whether a ray at 2.06 m is stopped by a wall built to "seven feet" is not
# knowable from a floor plan: the drawn line has a width, the height is a
# nominal figure rather than a measurement, and the source and target heights
# are conventions from TG-108 Fig. 5 rather than surveyed points. Crediting
# the barrier in that case would lower the computed dose on the strength of a
# coincidence. Where the clearance is uncertain the conservative reading is
# that the wall is not in the way, so a grazing crossing is discarded and the
# section marks it, rather than the answer quietly depending on a few
# centimetres. Discarding attenuation only ever raises the required
# thickness, so this can err generous but never unsafe.
GRAZE_MARGIN_M = 0.1


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
            coordinate marks the barrier rather than the protected point.
            Applies to every source, TG-108 included -- TG-108's own default
            distances are drawn from the same NCRP guidance -- so the caller
            passes ``not poi.offset_applied`` regardless of source method.
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

    if same_floor:
        # Both heights are entered independently -- a beam height for the
        # source, an occupied height for the point -- and are never meant to
        # imply the point sits obliquely above or below the source. Only a
        # floor-to-floor gap does that.
        vertical = 0.0
        notes.append("same floor: no vertical separation is assumed between source and point")
    else:
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

    if same_floor and horizontal < 0.5:
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
    """A barrier a source-to-point path passes through.

    ``distance_along_m``, ``hit_height_m``, ``base_z_m`` and ``top_z_m`` are
    only set for a wall actually drawn on a floor (``wall_id`` not None) --
    a manually declared barrier has no placement on the path, so it cannot be
    drawn in a cross-section.  ``base_z_m``/``top_z_m`` are the wall's own
    base and top expressed as height above the project datum, i.e. what the
    elevation view draws as the wall's vertical extent.
    """

    material: str
    thickness_mm: float
    effective_thickness_mm: float
    label: str
    angle_deg: float = 0.0
    wall_id: str | None = None
    floor_name: str = ""
    distance_along_m: float | None = None
    hit_height_m: float | None = None
    base_z_m: float | None = None
    top_z_m: float | None = None
    # How the path relates to this wall.  ``"through"`` is the only one that
    # shields anything and the only one the physics ever sees; the others
    # exist so a cross-section can draw a wall the ray misses rather than
    # silently omitting it, which reads as a fault rather than as geometry.
    #
    #   "through"  the path passes through the wall -- a real barrier
    #   "cleared"  it crosses the wall in plan but above or below it
    #   "grazed"   it passes inside the wall, but within GRAZE_MARGIN_M of an
    #              edge, so the shielding is not relied on
    #   "beyond"   the cut plane crosses the wall, but outside the path's
    #              own extent: past the point, or behind the source
    relation: str = "through"

    @property
    def is_oblique(self) -> bool:
        """True when the obliquity correction changed the traversed thickness."""
        return abs(self.effective_thickness_mm - self.thickness_mm) > 1e-9


def world_point(project: Project, floor: Floor, x: float, y: float, height_m: float
                ) -> tuple[float, float, float]:
    """Convert a point on a floor to project world coordinates in metres."""
    east, north, _ = floor_offset_m(floor, x, y)
    return east, north, floor.elevation_m + height_m


@dataclass(frozen=True)
class _WallHit:
    """Where a path meets a wall's plane, before the height band is tested."""

    t: float
    hit_z: float
    base_z: float
    top_z: float
    cos_theta: float
    distance_along_m: float
    # Where the hit falls along the wall itself, and how long the wall is, so
    # a caller can tell a solid hit from one clipping an end.
    along_wall_m: float
    wall_length_m: float

    def grazes(self, margin: float) -> bool:
        """True when the hit is inside the wall but within ``margin`` of an edge.

        Being inside is not the question -- whether it is *reliably* inside
        is.  A hit this close to the top, the base or an end is inside only
        by an amount smaller than the drawing can be trusted to.
        """
        return (
            self.hit_z - self.base_z < margin
            or self.top_z - self.hit_z < margin
            or self.along_wall_m < margin
            or self.wall_length_m - self.along_wall_m < margin
        )


def _wall_plane_hit(
    project: Project,
    floor: Floor,
    wall: Wall,
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    *,
    bounded: bool = True,
) -> _WallHit | None:
    """Where the segment meets the wall's vertical plane, within its plan extent.

    Height is deliberately not tested here.  Keeping the plan intersection
    separate from the height band is what lets one caller ask "does this go
    through the wall" and another ask "does it pass clear over it", off the
    same arithmetic rather than two copies that could drift apart.

    Args:
        bounded: Restrict the hit to the segment itself.  False extends the
            path to the infinite line through it, which is what a drawn
            section wants -- an architectural section cuts the whole building
            along a line, not just the stretch between two markers.
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
    if bounded and not 0.0 <= t <= 1.0:
        return None

    hit_x = start[0] + t * dx
    hit_y = start[1] + t * dy

    # Within the wall's extent in plan?
    along = ((hit_x - ax) * wall_dx + (hit_y - ay) * wall_dy) / (wall_length**2)
    if not 0.0 <= along <= 1.0:
        return None

    path_length = math.sqrt(dx * dx + dy * dy + dz * dz)
    return _WallHit(
        t=t,
        hit_z=start[2] + t * dz,
        base_z=floor.elevation_m + wall.base_height_m,
        top_z=floor.elevation_m + wall.top_height_m,
        cos_theta=abs(denominator) / path_length if path_length else 1.0,
        distance_along_m=t * math.hypot(dx, dy),
        along_wall_m=along * wall_length,
        wall_length_m=wall_length,
    )


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
    hit = _wall_plane_hit(project, floor, wall, start, end)
    if hit is None:
        return None

    # Within the wall's height band?
    if not hit.base_z <= hit.hit_z <= hit.top_z:
        return None

    # Inside, but only just?  Then the shielding is not relied on -- see
    # GRAZE_MARGIN_M.  The elevation view still draws the wall, marked
    # "grazed", so the decision is visible rather than silent.
    if hit.grazes(GRAZE_MARGIN_M):
        return None

    hit_z, base_z, top_z = hit.hit_z, hit.base_z, hit.top_z
    cos_theta = hit.cos_theta
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
        distance_along_m=hit.distance_along_m,
        hit_height_m=hit_z,
        base_z_m=base_z,
        top_z_m=top_z,
    )


def wall_in_section(
    project: Project,
    floor: Floor,
    wall: Wall,
    start: tuple[float, float, float],
    end: tuple[float, float, float],
) -> Crossing | None:
    """Return a wall the cut plane crosses but the path does not go through.

    The counterpart to :func:`wall_crossing`, off the same plan intersection.
    An architectural section cuts the whole building along a line and draws
    every wall on it, not only the stretch between two markers, so the path is
    extended to its infinite line here.  What comes back is never a barrier --
    it is what lets the section be checked as geometry: a wall the ray goes
    over is drawn with the ray skimming it, and a wall past the point is drawn
    where the ray stopped short of it.  Both would otherwise be absent, and an
    empty section reads as a fault rather than as a ray missing everything.

    Returns:
        The wall with its ``relation`` set to ``"cleared"`` or ``"beyond"``,
        or None when the cut misses the wall or the path goes through it (in
        which case :func:`wall_crossing` is what reports it).
    """
    hit = _wall_plane_hit(project, floor, wall, start, end, bounded=False)
    if hit is None:
        return None
    within_path = 0.0 <= hit.t <= 1.0
    inside = hit.base_z <= hit.hit_z <= hit.top_z
    if within_path and inside and not hit.grazes(GRAZE_MARGIN_M):
        return None  # a real crossing; wall_crossing owns it
    relation = "beyond"
    if within_path:
        relation = "grazed" if inside else "cleared"
    return Crossing(
        material=wall.material,
        thickness_mm=wall.thickness_mm,
        effective_thickness_mm=0.0,
        label=wall.label or f"{wall.material} wall",
        wall_id=wall.id,
        floor_name=floor.name,
        distance_along_m=hit.distance_along_m,
        hit_height_m=hit.hit_z,
        base_z_m=hit.base_z,
        top_z_m=hit.top_z,
        relation=relation,
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

    # The screening overlay's hypothetical barrier, applied last so it stacks
    # on whatever the drawing already provides rather than replacing it.  It
    # goes in here, where both methodologies assemble their paths, so neither
    # can be given it without the other.
    trial = getattr(project, "trial_barrier", None)
    if trial is not None and trial.thickness_mm > 0:
        crossings.append(
            Crossing(
                material=trial.material,
                thickness_mm=trial.thickness_mm,
                effective_thickness_mm=trial.thickness_mm,
                label=f"trial {trial.material} {trial.thickness_mm:g} mm",
            )
        )

    return crossings, warnings


@dataclass
class ElevationEndpoint:
    """One end of an elevation cross-section: where it sits in the side view."""

    label: str
    floor_name: str
    horizontal_m: float
    height_m: float


@dataclass
class ElevationProfile:
    """A vertical slice through the project along one source-to-point path.

    The cutting plane is vertical and contains the straight line from source
    to point: ``horizontal_m`` on either endpoint, and on every crossing, is
    the signed distance along that line's horizontal bearing, measured from
    the source.  Walls are reported only where they are actually crossed
    (``wall_id`` set); a manually declared barrier has no drawing position and
    is left out.
    """

    source: ElevationEndpoint
    target: ElevationEndpoint
    horizontal_total_m: float
    vertical_angle_deg: float
    floors: list[tuple[str, float]]
    crossings: list[Crossing]
    section: list[Crossing] = field(default_factory=list)
    declared: list[Crossing] = field(default_factory=list)
    floor_crossings: list[tuple[str, float, float]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def elevation_profile(
    project: Project,
    source: SourcePoint,
    poi: PointOfInterest,
    *,
    apply_obliquity: bool = False,
) -> ElevationProfile:
    """Build a side-view cross-section along one source-to-point path.

    Reuses :func:`path_barriers` for which walls are in the way, then keeps
    only the ones actually drawn on a floor (a manually declared barrier has
    no geometric position to plot) and works out where along the path, and at
    what height, each one is met.
    """
    source_floor = project.floor(source.floor_id)
    poi_floor = project.floor(poi.floor_id)

    warnings: list[str] = []
    sx, sy, w1 = floor_offset_m(source_floor, source.x, source.y)
    px, py, w2 = floor_offset_m(poi_floor, poi.x, poi.y)
    warnings.extend(w1)
    warnings.extend(w2)

    poi_height, _ = target_height(source_floor, poi_floor, poi)
    source_z = source_floor.elevation_m + source.height_above_floor_m
    poi_z = poi_floor.elevation_m + poi_height

    dx, dy = px - sx, py - sy
    horizontal_total = math.hypot(dx, dy)
    if horizontal_total < 1e-9 and abs(poi_z - source_z) < 1e-9:
        raise GeometryError(
            f"source {source.label or source.id!r} and point {poi.label or poi.id!r} are "
            "coincident; move one of them"
        )
    vertical_angle = math.degrees(math.atan2(poi_z - source_z, horizontal_total))

    crossings, wall_warnings = path_barriers(project, source, poi, apply_obliquity=apply_obliquity)
    warnings.extend(wall_warnings)
    drawable = [c for c in crossings if c.wall_id is not None]
    # A declared barrier has no drawn position, but it is still on the path
    # and is often the only thing shielding a route between floors, so it is
    # reported rather than dropped -- leaving the section empty would say
    # there is nothing in the way when there is.
    declared = [c for c in crossings if c.wall_id is None]

    # Every other wall the cut plane crosses: gone over, gone under, or simply
    # further along the line than the path reaches.  None of them shield
    # anything; they are what makes the drawing checkable as geometry.
    poi_height, _ = target_height(source_floor, poi_floor, poi)
    ray_start = world_point(project, source_floor, source.x, source.y,
                            source.height_above_floor_m)
    ray_end = world_point(project, poi_floor, poi.x, poi.y, poi_height)
    section: list[Crossing] = []
    for floor in project.floors:
        if floor.calibration is None:
            continue
        for wall in floor.walls:
            missed = wall_in_section(project, floor, wall, ray_start, ray_end)
            if missed is not None:
                section.append(missed)
    section.sort(key=lambda c: c.distance_along_m or 0.0)

    floors = sorted(((f.name, f.elevation_m) for f in project.floors), key=lambda item: item[1])

    # Where the ray passes each floor level between its two ends.  This is
    # real geometry, and for a path between storeys it is where a slab sits,
    # which is the only place a declared barrier can honestly be drawn.
    floor_crossings: list[tuple[str, float, float]] = []
    span = poi_z - source_z
    if abs(span) > 1e-9:
        for floor in project.floors:
            fraction = (floor.elevation_m - source_z) / span
            if 0.0 < fraction < 1.0:
                floor_crossings.append(
                    (floor.name, floor.elevation_m, fraction * horizontal_total)
                )
        floor_crossings.sort(key=lambda item: item[2])

    return ElevationProfile(
        source=ElevationEndpoint(
            label=source.label or source.id, floor_name=source_floor.name,
            horizontal_m=0.0, height_m=source_z,
        ),
        target=ElevationEndpoint(
            label=poi.label or poi.id, floor_name=poi_floor.name,
            horizontal_m=horizontal_total, height_m=poi_z,
        ),
        horizontal_total_m=horizontal_total,
        vertical_angle_deg=vertical_angle,
        floors=floors,
        crossings=drawable,
        section=section,
        declared=declared,
        floor_crossings=floor_crossings,
        warnings=warnings,
    )


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
    anticlockwise from the chart's +x axis.

    An ``"elevation"`` chart is a vertical slice through the same 3D scatter
    field, so a point is traced back into that slice along its own bearing --
    its angle from the table axis -- and the cell on that bearing is what the
    inverse-square scaling works from.

    Two things about the chart's x are deliberate.  Its *magnitude* is the
    point's whole horizontal distance from the isocentre, not the component
    along the table axis: the slice turns to contain the point, so a point off
    to the side is as far out in the slice as one straight down the table.
    Taking only the along-table component would discard most of the separation
    for a point that is mostly lateral, and place it at the wrong angle.  Its
    *sign* is which end of the table the point lies toward, so a chart whose
    head and foot ends differ is not read on the wrong one.  A point exactly
    abeam the isocentre has no end to prefer and is taken as positive.

    ``y`` is the rise: positive above the plane of the table, negative below.

    The distance returned is always the true three-dimensional separation,
    since that is what the inverse-square correction must use.
    """
    source_floor = project.floor(source.floor_id)
    poi_floor = project.floor(poi.floor_id)

    sx, sy, _ = floor_offset_m(source_floor, source.x, source.y)
    px, py, _ = floor_offset_m(poi_floor, poi.x, poi.y)
    east, north = px - sx, py - sy

    # Same as distance(): each height is entered independently and is not
    # meant to imply a floor-to-floor gap when there isn't one.
    if source.floor_id == poi.floor_id:
        rise = 0.0
    else:
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
        # The chart is a vertical slice through the field, turned to contain
        # the point: the whole horizontal separation carries into the slice,
        # signed by which end of the table the point lies toward.
        chart_x = math.copysign(math.hypot(east, north), local_y)
        chart_y = rise
        # The angle from the table axis is what fixes which cell is read, so
        # it is stated outright -- it is the one number a reviewer can check
        # against the room without redoing the trigonometry.
        note = (
            f"elevation chart: {abs(chart_x):.2f} m out from the isocentre at "
            f"{math.degrees(math.atan2(chart_y, chart_x)):.1f}° from the table axis "
            f"({chart_y:+.2f} m in height)"
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
