"""Project data model: floors, calibration, points and their linkage.

Coordinate convention
---------------------
Every placed point is stored in **PDF page space**: units of 1/72 inch, origin
at the top-left of the page, y increasing downward.  This is the raster
convention PyMuPDF produces at zoom 1.0, and it is deliberately *not* screen
pixels (which change with zoom) nor calibrated real-world units (which would
make every point move when a floor is re-calibrated).  Scale and alignment are
applied at calculation time.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Method = Literal["tg108", "ncrp147", "ncrp147_ct"]
AreaClass = Literal["controlled", "uncontrolled"]

# Metres per unit, for the distance units offered during calibration.
LENGTH_UNITS: dict[str, float] = {
    "ft": 0.3048,
    "in": 0.0254,
    "m": 1.0,
    "cm": 0.01,
    "mm": 0.001,
}


# No structural barrier is metres thick.  Anything beyond this is a units
# slip, and saying so is far better than silently shielding with a 200 m wall.
MAX_WALL_THICKNESS_MM = 3000.0


def new_id(prefix: str) -> str:
    """Return a short unique identifier with a readable prefix."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# A PDF user-space unit is 1/72 inch of paper.  A drawing scale relates that
# paper length to a real one, so the two together fix metres per PDF unit
# without anything being clicked.
PDF_UNIT_METRES = 0.0254 / 72.0

_SCALE_TOKEN = re.compile(
    r"^\s*(\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?)\s*"
    r"(in|inch|inches|\"|ft|foot|feet|'|mm|cm|m)?\s*$",
    re.IGNORECASE,
)

_SCALE_UNITS = {
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254, '"': 0.0254,
    "ft": 0.3048, "foot": 0.3048, "feet": 0.3048, "'": 0.3048,
    "mm": 0.001, "cm": 0.01, "m": 1.0,
}


def _scale_length(token: str) -> tuple[float, str | None]:
    """Parse one side of a scale, e.g. ``1/4"`` or ``2.4 m``, into metres."""
    match = _SCALE_TOKEN.match(token)
    if not match:
        raise ValueError(f"could not read {token.strip()!r} as a length")
    number, unit = match.group(1), match.group(2)
    if "/" in number:
        top, bottom = (float(part) for part in number.split("/"))
        if bottom == 0:
            raise ValueError("a scale cannot divide by zero")
        value = top / bottom
    else:
        value = float(number)
    if value <= 0:
        raise ValueError(f"{token.strip()!r} must be greater than zero")
    return value, (unit.lower() if unit else None)


def parse_scale(text: str) -> tuple[float, str]:
    """Read a written drawing scale and return ``(real per paper, description)``.

    Accepts the forms a drawing states its scale in:

    - a bare ratio, ``1:50`` or ``1:100``
    - paper against real with units, ``1/4" = 1'`` or ``1 cm = 1 m``

    Both sides are interpreted as *paper length* then *real length*, which is
    how a scale is conventionally written.  A ratio with no units on either
    side is taken as paper:real directly.
    """
    cleaned = text.strip().replace("=", ":")
    if ":" not in cleaned:
        raise ValueError(
            "write a scale as paper:real, for example '1:50', '1/4\" = 1\'' or '1 cm = 1 m'"
        )
    left, _, right = cleaned.partition(":")
    paper, paper_unit = _scale_length(left)
    real, real_unit = _scale_length(right)

    if paper_unit is None and real_unit is None:
        ratio = real / paper
        return ratio, f"1:{ratio:g}"
    if paper_unit is None or real_unit is None:
        raise ValueError("give units on both sides of the scale, or on neither")

    ratio = (real * _SCALE_UNITS[real_unit]) / (paper * _SCALE_UNITS[paper_unit])
    return ratio, f"{left.strip()} = {right.strip()} (1:{ratio:g})"


@dataclass
class Calibration:
    """Two clicked points and the real distance between them.

    Attributes:
        p1: First point in PDF page space.
        p2: Second point in PDF page space.
        known_distance: Real-world distance between them.
        unit: Unit of ``known_distance``; a key of :data:`LENGTH_UNITS`.
    """

    p1: tuple[float, float] = (0.0, 0.0)
    p2: tuple[float, float] = (0.0, 0.0)
    known_distance: float = 0.0
    unit: str = "ft"
    scale_text: str = ""
    scale_ratio: float | None = None

    @property
    def is_typed(self) -> bool:
        """True when the scale was written down rather than measured off the page."""
        return self.scale_ratio is not None

    @property
    def pixel_distance(self) -> float:
        """Separation of the two clicked points in PDF units."""
        dx = self.p2[0] - self.p1[0]
        dy = self.p2[1] - self.p1[1]
        return (dx * dx + dy * dy) ** 0.5

    @property
    def metres_per_unit(self) -> float:
        """Metres of real world per PDF unit."""
        if self.scale_ratio is not None:
            if self.scale_ratio <= 0:
                raise ValueError("scale ratio must be positive")
            return PDF_UNIT_METRES * self.scale_ratio
        if self.pixel_distance <= 0:
            raise ValueError("calibration points are coincident; pick two distinct points")
        if self.known_distance <= 0:
            raise ValueError("calibration distance must be positive")
        return (self.known_distance * LENGTH_UNITS[self.unit]) / self.pixel_distance

    @classmethod
    def from_scale(cls, text: str) -> Calibration:
        """Build a calibration from a written scale such as ``1/4" = 1'``."""
        ratio, description = parse_scale(text)
        return cls(scale_text=description, scale_ratio=ratio)

    def describe(self) -> str:
        """One-line summary for display, so the scale can be eyeballed."""
        mpu = self.metres_per_unit
        if self.scale_ratio is not None:
            return f"{self.scale_text} = {mpu:.5f} m/unit ({1 / mpu:.2f} units/m)"
        return (
            f"{self.known_distance:g} {self.unit} over {self.pixel_distance:.1f} pdf units "
            f"= {mpu:.5f} m/unit ({1 / mpu:.2f} units/m)"
        )


@dataclass
class ScatterMapData:
    """A manufacturer scatter chart as published, stored verbatim.

    The grid is kept in the units it was read in rather than converted on
    import, so the saved project shows the same numbers as the vendor page and
    a reviewer can check them cell by cell.

    Attributes:
        plane: ``"plan"`` for the view looking down, ``"elevation"`` for the
            side view.
        x_coords: Column offsets from the isocentre.
        y_coords: Row offsets from the isocentre.  On an elevation chart this
            is height above the isocentre.
        values: ``values[row][column]``; None for blank or "NA" cells.
        per: What each value is per, e.g. ``"procedure"``.
        flip_x: Mirror the column axis -- a vendor's left/right convention
            does not always match the source's rotation arrow, and pasting
            the grid mirrored the same way every time it prints is easier
            than transcribing it backwards.
        flip_y: Mirror the row axis, likewise.
    """

    id: str
    name: str
    plane: str = "plan"
    coordinate_unit: str = "in"
    value_unit: str = "mGy"
    per: str = "procedure"
    source: str = ""
    x_coords: list[float] = field(default_factory=list)
    y_coords: list[float] = field(default_factory=list)
    values: list[list[float | None]] = field(default_factory=list)
    flip_x: bool = False
    flip_y: bool = False


@dataclass
class Barrier:
    """A shielding barrier declared by name rather than drawn on the plan.

    Used where the attenuating structure is not on the drawing, or not worth
    drawing: a slab, a leaded door, a control booth window.  Thickness is
    always in millimetres regardless of the material's tabulated unit, so the
    stored value never depends on which methodology consumes it.
    """

    material: str
    thickness_mm: float
    label: str = ""


@dataclass
class Wall:
    """A wall drawn on a floor plan, acting as a barrier when a path crosses it.

    The wall is a vertical rectangle: the plan segment ``p1``-``p2`` extruded
    between ``base_height_m`` and ``top_height_m`` above its own floor.  Giving
    it a height is what lets a path between floors be tested honestly -- a ray
    climbing to the storey above crosses a partition only while it is still
    below the top of that partition.

    Attributes:
        p1: One end of the wall in PDF page space.
        p2: The other end, same space.
        material: Material name, resolved against whichever methodology is
            evaluating the path.
        thickness_mm: Wall thickness in millimetres.
        base_height_m: Bottom of the wall above its floor.
        top_height_m: Top of the wall above its floor.
        label: Free text for the audit trail.
        color: Display override for the drawn wall, as a CSS color string
            (e.g. ``"#ff0000"``).  Empty string means "use the material's
            default color" -- this is purely a display choice and never
            affects the calculation.
    """

    id: str
    p1: tuple[float, float]
    p2: tuple[float, float]
    material: str = "concrete"
    thickness_mm: float = 150.0
    base_height_m: float = 0.0
    top_height_m: float = 3.0
    label: str = ""
    color: str = ""

    def __post_init__(self) -> None:
        if self.thickness_mm <= 0:
            raise ValueError(f"wall thickness must be positive, got {self.thickness_mm}")
        if self.thickness_mm > MAX_WALL_THICKNESS_MM:
            raise ValueError(
                f"wall thickness of {self.thickness_mm:g} mm exceeds "
                f"{MAX_WALL_THICKNESS_MM:g} mm, which is thicker than any real barrier; "
                "check the entry is in millimetres and not metres"
            )
        if self.top_height_m <= self.base_height_m:
            raise ValueError(
                f"wall top ({self.top_height_m}) must be above its base ({self.base_height_m})"
            )


@dataclass
class Measurement:
    """A recorded distance between two points on one drawing.

    Measurements are kept with the project rather than being transient, so a
    dimension taken off the drawing (a wall standoff, a room width) survives
    into the saved file and can be cited in a report.
    """

    id: str
    p1: tuple[float, float]
    p2: tuple[float, float]
    label: str = ""


@dataclass
class Floor:
    """One architectural drawing and its place in the vertical stack.

    Attributes:
        id: Stable identifier.
        name: Display name, e.g. "Level 2 - Nuclear Medicine".
        pdf_name: File name of the PDF inside the project archive.
        page: Zero-based page index within that PDF.
        elevation_m: Height of this floor's slab above the project datum.
        calibration: Scale for this drawing; None until calibrated.
        alignment: PDF-space point marking a physical feature common to every
            floor (a column, stair core, lift shaft).  None means the drawings
            are assumed already co-registered, which is reported as a warning.
        alignment2: A second such feature.  One point fixes only where a
            drawing sits; it cannot tell how the drawing is turned, so sheets
            laid out at different orientations will not line up from one point
            alone.  Two points give the rotation and the relative scale as
            well, which is a complete registration for architectural drawings:
            they are never sheared and never scaled differently along the two
            axes.
        page_width: Page width in PDF units, cached for the viewer.
        page_height: Page height in PDF units.
    """

    id: str
    name: str
    pdf_name: str
    page: int = 0
    elevation_m: float = 0.0
    calibration: Calibration | None = None
    alignment: tuple[float, float] | None = None
    alignment2: tuple[float, float] | None = None
    page_width: float = 0.0
    page_height: float = 0.0
    measurements: list[Measurement] = field(default_factory=list)
    walls: list[Wall] = field(default_factory=list)

    @property
    def is_calibrated(self) -> bool:
        """True when a usable scale has been established."""
        return self.calibration is not None

    @property
    def is_oriented(self) -> bool:
        """True when two reference features fix the drawing's rotation."""
        return self.alignment is not None and self.alignment2 is not None


@dataclass
class SourcePoint:
    """A radiation source placed on a floor.

    ``params`` holds method-specific inputs; its keys mirror the argument
    names of the corresponding physics dataclass.

    For equipment with a scatter chart, the placed point is the **isocentre**
    and ``rotation_deg`` orients the chart on the plan: it is the angle the
    chart's +x axis makes with east, measured anticlockwise, so 0 leaves the
    chart's +y (usually the table axis) pointing up the page.
    """

    id: str
    floor_id: str
    x: float
    y: float
    label: str = ""
    method: Method = "tg108"
    height_above_floor_m: float = 1.0
    rotation_deg: float = 0.0
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class PointOfInterest:
    """A location to be protected.

    Attributes:
        occupancy: Occupancy factor T.
        area_class: Selects the default design goal.
        height_above_floor_m: Height of the protected point above its own
            floor.  When ``auto_height`` is set this is recomputed from the
            method's convention instead.
        auto_height: Apply TG-108 Fig. 5 heights automatically (0.5 m above the
            floor for a room above the source, 1.7 m for a room below).
        offset_applied: Whether the placed coordinate already represents the
            point of protection.  False means the click marks the barrier and
            the NCRP-recommended 0.3 m standoff must be added by the app.
        existing_material: Shielding already present in the structure.
        existing_thickness: Its thickness, in that material's tabulated unit
            (cm for TG-108 materials, mm for NCRP 147).
        linked_source_ids: Sources incident on this point.  All of them
            contribute; their doses are summed before solving.
        manual_barriers: Source id to barriers declared for that path
            specifically, added to whatever walls the path crosses.  A point
            may be shielded from different sources by different structures, so
            these are per source rather than per point.
        distance_overrides: Source id to distance in metres, replacing the
            distance derived from the placed geometry.  Use when the drawing
            geometry is not the distance the calculation should use -- an
            angled path, a dimension taken from a different drawing, or a
            figure agreed with the reviewer.  Overrides are always stated in
            the audit trail alongside the geometric distance they replaced.
    """

    id: str
    floor_id: str
    x: float
    y: float
    label: str = ""
    occupancy: float = 1.0
    area_class: AreaClass = "uncontrolled"
    height_above_floor_m: float = 1.7
    auto_height: bool = True
    offset_applied: bool = False
    existing_material: str = ""
    existing_thickness: float = 0.0
    linked_source_ids: list[str] = field(default_factory=list)
    distance_overrides: dict[str, float] = field(default_factory=dict)
    manual_barriers: dict[str, list[Barrier]] = field(default_factory=dict)


def _poi_from_dict(raw: dict[str, Any]) -> PointOfInterest:
    """Rebuild a point of interest, restoring its nested barrier objects."""
    data = dict(raw)
    data["manual_barriers"] = {
        source_id: [Barrier(**b) for b in barriers]
        for source_id, barriers in (raw.get("manual_barriers") or {}).items()
    }
    return PointOfInterest(**data)


@dataclass
class Project:
    """A complete shielding project."""

    name: str = "Untitled project"
    floors: list[Floor] = field(default_factory=list)
    sources: list[SourcePoint] = field(default_factory=list)
    pois: list[PointOfInterest] = field(default_factory=list)
    materials: list[str] = field(default_factory=lambda: ["lead", "concrete"])
    scatter_maps: list[ScatterMapData] = field(default_factory=list)
    display_unit: str = "ft"
    apply_obliquity: bool = False
    schema_version: int = 1

    def floor(self, floor_id: str) -> Floor:
        """Return a floor by id."""
        for f in self.floors:
            if f.id == floor_id:
                return f
        raise KeyError(f"no floor {floor_id!r}")

    def source(self, source_id: str) -> SourcePoint:
        """Return a source by id."""
        for s in self.sources:
            if s.id == source_id:
                return s
        raise KeyError(f"no source {source_id!r}")

    def scatter_map(self, map_id: str) -> ScatterMapData:
        """Return a stored scatter chart by id."""
        for m in self.scatter_maps:
            if m.id == map_id:
                return m
        raise KeyError(f"no scatter map {map_id!r}")

    def poi(self, poi_id: str) -> PointOfInterest:
        """Return a point of interest by id."""
        for p in self.pois:
            if p.id == poi_id:
                return p
        raise KeyError(f"no point of interest {poi_id!r}")

    def remove_floor(self, floor_id: str) -> None:
        """Delete a floor along with everything placed on it."""
        self.floors = [f for f in self.floors if f.id != floor_id]
        removed = {s.id for s in self.sources if s.floor_id == floor_id}
        self.sources = [s for s in self.sources if s.floor_id != floor_id]
        self.pois = [p for p in self.pois if p.floor_id != floor_id]
        for poi in self.pois:
            poi.linked_source_ids = [i for i in poi.linked_source_ids if i not in removed]
            for source_id in removed:
                poi.distance_overrides.pop(source_id, None)
                poi.manual_barriers.pop(source_id, None)
            poi.manual_barriers.pop(source_id, None)

    def remove_source(self, source_id: str) -> None:
        """Delete a source and unlink it everywhere."""
        self.sources = [s for s in self.sources if s.id != source_id]
        for poi in self.pois:
            poi.linked_source_ids = [i for i in poi.linked_source_ids if i != source_id]
            poi.distance_overrides.pop(source_id, None)
            poi.manual_barriers.pop(source_id, None)

    def set_floor_to_floor(self, heights_m: list[float]) -> None:
        """Set elevations from consecutive floor-to-floor heights.

        ``heights_m[i]`` is the distance from floor ``i`` to floor ``i+1``, so
        the list is one shorter than the floor list.  The lowest floor sits at
        elevation 0.
        """
        if len(heights_m) != max(len(self.floors) - 1, 0):
            raise ValueError(
                f"expected {max(len(self.floors) - 1, 0)} floor-to-floor heights, "
                f"got {len(heights_m)}"
            )
        elevation = 0.0
        for index, floor in enumerate(self.floors):
            floor.elevation_m = elevation
            if index < len(heights_m):
                elevation += heights_m[index]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to plain JSON-compatible types."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Project:
        """Rebuild a project from :meth:`to_dict` output."""
        version = data.get("schema_version", 1)
        if version != 1:
            raise ValueError(f"unsupported project schema version {version}")

        floors = []
        for raw in data.get("floors", []):
            cal_raw = raw.get("calibration")
            calibration = (
                Calibration(
                    p1=tuple(cal_raw.get("p1", (0.0, 0.0))),
                    p2=tuple(cal_raw.get("p2", (0.0, 0.0))),
                    known_distance=cal_raw.get("known_distance", 0.0),
                    unit=cal_raw.get("unit", "ft"),
                    scale_text=cal_raw.get("scale_text", ""),
                    scale_ratio=cal_raw.get("scale_ratio"),
                )
                if cal_raw
                else None
            )
            alignment = raw.get("alignment")
            alignment2 = raw.get("alignment2")
            measurements = [
                Measurement(
                    id=m["id"],
                    p1=tuple(m["p1"]),
                    p2=tuple(m["p2"]),
                    label=m.get("label", ""),
                )
                for m in raw.get("measurements", [])
            ]
            walls = [
                Wall(
                    id=w["id"],
                    p1=tuple(w["p1"]),
                    p2=tuple(w["p2"]),
                    material=w.get("material", "concrete"),
                    thickness_mm=w.get("thickness_mm", 150.0),
                    base_height_m=w.get("base_height_m", 0.0),
                    top_height_m=w.get("top_height_m", 3.0),
                    label=w.get("label", ""),
                    color=w.get("color", ""),
                )
                for w in raw.get("walls", [])
            ]
            floors.append(
                Floor(
                    id=raw["id"],
                    name=raw["name"],
                    pdf_name=raw["pdf_name"],
                    page=raw.get("page", 0),
                    elevation_m=raw.get("elevation_m", 0.0),
                    calibration=calibration,
                    alignment=tuple(alignment) if alignment else None,
                    alignment2=tuple(alignment2) if alignment2 else None,
                    page_width=raw.get("page_width", 0.0),
                    page_height=raw.get("page_height", 0.0),
                    measurements=measurements,
                    walls=walls,
                )
            )

        return cls(
            name=data.get("name", "Untitled project"),
            floors=floors,
            sources=[SourcePoint(**raw) for raw in data.get("sources", [])],
            pois=[_poi_from_dict(raw) for raw in data.get("pois", [])],
            materials=data.get("materials", ["lead", "concrete"]),
            scatter_maps=[ScatterMapData(**raw) for raw in data.get("scatter_maps", [])],
            display_unit=data.get("display_unit", "ft"),
            apply_obliquity=data.get("apply_obliquity", False),
            schema_version=1,
        )
