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


def new_id(prefix: str) -> str:
    """Return a short unique identifier with a readable prefix."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@dataclass
class Calibration:
    """Two clicked points and the real distance between them.

    Attributes:
        p1: First point in PDF page space.
        p2: Second point in PDF page space.
        known_distance: Real-world distance between them.
        unit: Unit of ``known_distance``; a key of :data:`LENGTH_UNITS`.
    """

    p1: tuple[float, float]
    p2: tuple[float, float]
    known_distance: float
    unit: str = "ft"

    @property
    def pixel_distance(self) -> float:
        """Separation of the two clicked points in PDF units."""
        dx = self.p2[0] - self.p1[0]
        dy = self.p2[1] - self.p1[1]
        return (dx * dx + dy * dy) ** 0.5

    @property
    def metres_per_unit(self) -> float:
        """Metres of real world per PDF unit."""
        if self.pixel_distance <= 0:
            raise ValueError("calibration points are coincident; pick two distinct points")
        if self.known_distance <= 0:
            raise ValueError("calibration distance must be positive")
        return (self.known_distance * LENGTH_UNITS[self.unit]) / self.pixel_distance

    def describe(self) -> str:
        """One-line summary for display, so the scale can be eyeballed."""
        mpu = self.metres_per_unit
        return (
            f"{self.known_distance:g} {self.unit} over {self.pixel_distance:.1f} pdf units "
            f"= {mpu:.5f} m/unit ({1 / mpu:.2f} units/m)"
        )


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
            floor (a column, stair core, lift shaft).  Cross-floor horizontal
            distances are measured relative to it.  None means the drawings
            are assumed already co-registered, which is reported as a warning.
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
    page_width: float = 0.0
    page_height: float = 0.0

    @property
    def is_calibrated(self) -> bool:
        """True when a usable scale has been established."""
        return self.calibration is not None


@dataclass
class SourcePoint:
    """A radiation source placed on a floor.

    ``params`` holds method-specific inputs; its keys mirror the argument
    names of the corresponding physics dataclass.
    """

    id: str
    floor_id: str
    x: float
    y: float
    label: str = ""
    method: Method = "tg108"
    height_above_floor_m: float = 1.0
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


@dataclass
class Project:
    """A complete shielding project."""

    name: str = "Untitled project"
    floors: list[Floor] = field(default_factory=list)
    sources: list[SourcePoint] = field(default_factory=list)
    pois: list[PointOfInterest] = field(default_factory=list)
    materials: list[str] = field(default_factory=lambda: ["lead", "concrete"])
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

    def remove_source(self, source_id: str) -> None:
        """Delete a source and unlink it everywhere."""
        self.sources = [s for s in self.sources if s.id != source_id]
        for poi in self.pois:
            poi.linked_source_ids = [i for i in poi.linked_source_ids if i != source_id]

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
                    p1=tuple(cal_raw["p1"]),
                    p2=tuple(cal_raw["p2"]),
                    known_distance=cal_raw["known_distance"],
                    unit=cal_raw.get("unit", "ft"),
                )
                if cal_raw
                else None
            )
            alignment = raw.get("alignment")
            floors.append(
                Floor(
                    id=raw["id"],
                    name=raw["name"],
                    pdf_name=raw["pdf_name"],
                    page=raw.get("page", 0),
                    elevation_m=raw.get("elevation_m", 0.0),
                    calibration=calibration,
                    alignment=tuple(alignment) if alignment else None,
                    page_width=raw.get("page_width", 0.0),
                    page_height=raw.get("page_height", 0.0),
                )
            )

        return cls(
            name=data.get("name", "Untitled project"),
            floors=floors,
            sources=[SourcePoint(**raw) for raw in data.get("sources", [])],
            pois=[PointOfInterest(**raw) for raw in data.get("pois", [])],
            materials=data.get("materials", ["lead", "concrete"]),
            schema_version=1,
        )
