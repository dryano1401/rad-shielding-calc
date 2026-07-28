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

from .project import Floor, LENGTH_UNITS, Measurement, PointOfInterest, Project, SourcePoint

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


def floor_offset_m(floor: Floor, x: float, y: float) -> tuple[float, float, list[str]]:
    """Convert a PDF-space point to metres relative to the floor's alignment point.

    Returns:
        ``(east_m, north_m, warnings)``.  PDF space has y increasing downward,
        so the north component is negated to give a conventional plan
        orientation.  Only differences matter, so the sign convention just has
        to be consistent.
    """
    if floor.calibration is None:
        raise GeometryError(
            f"floor {floor.name!r} has no scale calibration; calibrate it before calculating"
        )
    scale = floor.calibration.metres_per_unit

    warnings: list[str] = []
    if floor.alignment is None:
        origin_x, origin_y = 0.0, 0.0
        warnings.append(
            f"floor {floor.name!r} has no alignment point; its drawing origin is assumed "
            "co-registered with the other floors. Cross-floor horizontal distances may be wrong."
        )
    else:
        origin_x, origin_y = floor.alignment

    return (x - origin_x) * scale, -(y - origin_y) * scale, warnings


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
    for poi in project.pois:
        if not poi.linked_source_ids:
            problems.append(f"point {poi.label or poi.id!r} has no linked sources")
    return problems
