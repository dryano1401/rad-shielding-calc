"""Manufacturer scatter isodose maps.

Vendors publish scatter as a grid of air kerma values on a plane through the
isocentre -- a plan view looking down, and an elevation view looking from the
side.  This module turns such a grid into a value at an arbitrary point.

The method mirrors what a physicist does by hand with the printed chart: find
the direction from isocentre to the point of interest, read the chart value in
that direction, then correct from the chart's distance to the real distance by
inverse square.

Doing that numerically means normalising each cell to a direction-dependent
quantity that is distance-independent:

    S = K * r^2         [mGy m^2 per procedure]

``S`` is effectively the scatter source strength on that bearing, and on real
charts it is impressively constant: along the table axis of the chart used to
develop this, ``S`` holds to within 2% from 0.5 m out to 2.5 m.  So for a
query bearing, ``S`` is read off the chart and

    K = S / d^2

gives the value at the true distance ``d``.

Where several cells share a bearing, the **largest** ``S`` is used rather than
the one nearest in radius.  Charts contain shadowed cells -- the pedestal
column on a typical plan view reads an order of magnitude below its
neighbours -- and picking by radius makes the answer jump by that order of
magnitude as the point of interest moves a little further out, in the
unsafe direction.  A wall is wide and such shadows are narrow, so the
unshadowed envelope is both the conservative choice and the one a physicist
reading the printed chart would make.  When the cells on a bearing disagree
substantially, that is reported rather than hidden.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

Plane = Literal["plan", "elevation"]

# Grid coordinate units accepted on import, in metres.
COORDINATE_UNITS: dict[str, float] = {
    "in": 0.0254,
    "ft": 0.3048,
    "mm": 0.001,
    "cm": 0.01,
    "m": 1.0,
}

# Cell value units, in milligray.
VALUE_UNITS: dict[str, float] = {"mGy": 1.0, "uGy": 1e-3, "Gy": 1000.0}

# Bearings further than this from any cell are reported rather than guessed.
DEFAULT_ANGLE_TOLERANCE_DEG = 30.0

# Cells closer than this to the isocentre are excluded: they sit inside the
# gantry, where the scatter is not a point source and r^2 scaling is meaningless.
MIN_RADIUS_M = 0.05


class IsodoseError(ValueError):
    """Raised when a scatter map cannot be built or sampled."""


@dataclass(frozen=True)
class Cell:
    """One grid point of a scatter map, in metres and milligray."""

    x_m: float
    y_m: float
    value_mGy: float

    @property
    def radius_m(self) -> float:
        """Distance from the isocentre."""
        return math.hypot(self.x_m, self.y_m)

    @property
    def bearing_deg(self) -> float:
        """Direction from the isocentre, degrees, measured from the +x axis."""
        return math.degrees(math.atan2(self.y_m, self.x_m))

    @property
    def strength(self) -> float:
        """``K * r^2``: the distance-normalised scatter strength on this bearing."""
        return self.value_mGy * self.radius_m**2


@dataclass
class ScatterMap:
    """A vendor scatter chart.

    Attributes:
        name: Display name, e.g. "Revolution CT - plan view".
        plane: ``"plan"`` for the view looking down (bearings in the
            horizontal plane) or ``"elevation"`` for the side view (angles
            above and below the isocentre plane).
        cells: Grid points, already converted to metres and milligray.
        per: What each value is per -- ``"procedure"``, ``"scan"``, or
            ``"week"``.  Carried into the audit trail; the caller multiplies
            by the matching count.
        source: Citation for the chart.
    """

    name: str
    plane: Plane
    cells: list[Cell] = field(default_factory=list)
    per: str = "procedure"
    source: str = ""

    def __post_init__(self) -> None:
        usable = [c for c in self.cells if c.radius_m >= MIN_RADIUS_M and c.value_mGy > 0]
        if not usable:
            raise IsodoseError(
                f"scatter map {self.name!r} has no usable cells: every value is zero, blank, "
                f"or within {MIN_RADIUS_M} m of the isocentre"
            )
        self.cells = usable

    @property
    def radius_range_m(self) -> tuple[float, float]:
        """Smallest and largest cell radius, for reporting extrapolation."""
        radii = [c.radius_m for c in self.cells]
        return min(radii), max(radii)


@dataclass(frozen=True)
class Sample:
    """The result of reading a scatter map on one bearing."""

    value_mGy: float
    distance_m: float
    cell: Cell
    bearing_deg: float
    angle_error_deg: float
    extrapolation_ratio: float
    strength: float = 0.0
    cells_considered: int = 1
    notes: tuple[str, ...] = ()

    def describe(self) -> str:
        """One line explaining the read, for the audit trail."""
        return (
            f"chart cell at ({self.cell.x_m:.2f}, {self.cell.y_m:.2f}) m, "
            f"r = {self.cell.radius_m:.2f} m, {self.cell.value_mGy:g} mGy, "
            f"bearing {self.cell.bearing_deg:.0f} deg vs requested "
            f"{self.bearing_deg:.0f} deg; scaled by "
            f"({self.cell.radius_m:.2f}/{self.distance_m:.2f})^2 to {self.value_mGy:.4g} mGy"
        )


def build_map(
    name: str,
    plane: Plane,
    x_coords: list[float],
    y_coords: list[float],
    values: list[list[float | None]],
    *,
    coordinate_unit: str = "in",
    value_unit: str = "mGy",
    per: str = "procedure",
    source: str = "",
) -> ScatterMap:
    """Build a scatter map from a published grid.

    Args:
        x_coords: Column offsets from the isocentre, one per column.
        y_coords: Row offsets from the isocentre, one per row.
        values: ``values[row][column]``; None marks a blank or "NA" cell, such
            as the masked region inside the gantry.
        coordinate_unit: Unit of the coordinates, usually inches.
        value_unit: Unit of the cell values.
        per: What each value is per.

    Returns:
        A :class:`ScatterMap` in metres and milligray.
    """
    if coordinate_unit not in COORDINATE_UNITS:
        raise IsodoseError(
            f"unknown coordinate unit {coordinate_unit!r}; known: {sorted(COORDINATE_UNITS)}"
        )
    if value_unit not in VALUE_UNITS:
        raise IsodoseError(f"unknown value unit {value_unit!r}; known: {sorted(VALUE_UNITS)}")
    if len(values) != len(y_coords):
        raise IsodoseError(
            f"{len(values)} value rows but {len(y_coords)} row coordinates"
        )

    length_scale = COORDINATE_UNITS[coordinate_unit]
    value_scale = VALUE_UNITS[value_unit]

    cells: list[Cell] = []
    for row_index, row in enumerate(values):
        if len(row) != len(x_coords):
            raise IsodoseError(
                f"row {row_index} has {len(row)} values but there are {len(x_coords)} columns"
            )
        for column_index, value in enumerate(row):
            if value is None:
                continue
            cells.append(
                Cell(
                    x_m=x_coords[column_index] * length_scale,
                    y_m=y_coords[row_index] * length_scale,
                    value_mGy=float(value) * value_scale,
                )
            )
    return ScatterMap(name=name, plane=plane, cells=cells, per=per, source=source)


def sample(
    scatter_map: ScatterMap,
    bearing_deg: float,
    distance_m: float,
    *,
    angle_tolerance_deg: float = DEFAULT_ANGLE_TOLERANCE_DEG,
) -> Sample:
    """Read the map on a bearing and scale it to a distance.

    Cells are matched on bearing, and the strongest of them sets the value --
    see the module docstring for why the envelope is preferred to the cell
    nearest in radius.

    Args:
        bearing_deg: Direction to the point, degrees from the map's +x axis.
        distance_m: True distance from the isocentre to the point.
        angle_tolerance_deg: How far off bearing a cell may be before the
            result is flagged as poorly matched.

    Returns:
        A :class:`Sample` carrying the value and how it was obtained.
    """
    if distance_m <= 0:
        raise IsodoseError(f"distance must be positive, got {distance_m}")

    def angular_error(cell: Cell) -> float:
        difference = (cell.bearing_deg - bearing_deg + 180.0) % 360.0 - 180.0
        return abs(difference)

    best_error = min(angular_error(cell) for cell in scatter_map.cells)
    # Everything within a degree of the closest bearing lies on the same ray
    # out of the isocentre and so describes the same direction.
    candidates = [c for c in scatter_map.cells if angular_error(c) <= best_error + 1.0]
    cell = max(candidates, key=lambda c: c.strength)

    notes: list[str] = []
    if best_error > angle_tolerance_deg:
        notes.append(
            f"nearest chart bearing is {best_error:.0f} deg away from the direction to this "
            f"point; the chart may not cover it"
        )

    weakest = min(c.strength for c in candidates)
    if weakest > 0 and cell.strength / weakest > 2.0:
        notes.append(
            f"cells on this bearing disagree by a factor of {cell.strength / weakest:.1f} "
            f"({len(candidates)} cells); the strongest was used, which is the conservative "
            "reading where part of the chart is shadowed by the pedestal or gantry"
        )

    ratio = distance_m / cell.radius_m
    if ratio > 3.0 or ratio < 1 / 3.0:
        notes.append(
            f"the chart value is being scaled by a factor of {ratio:.1f} in distance; "
            "check the chart covers this range"
        )

    return Sample(
        value_mGy=cell.strength / distance_m**2,
        distance_m=distance_m,
        cell=cell,
        bearing_deg=bearing_deg,
        angle_error_deg=best_error,
        extrapolation_ratio=ratio,
        strength=cell.strength,
        cells_considered=len(candidates),
        notes=tuple(notes),
    )


def parse_grid(text: str) -> tuple[list[float], list[float], list[list[float | None]]]:
    """Parse a pasted scatter grid into coordinates and values.

    The expected shape mirrors the printed chart: a header row of column
    offsets with a blank leading cell, then one row per row-offset beginning
    with that offset.  Cells may be separated by tabs, commas or runs of
    spaces.  Blank cells and "NA" become None.

    Returns:
        ``(x_coords, y_coords, values)``.
    """
    rows = [line for line in text.replace("\r", "").split("\n") if line.strip()]
    if len(rows) < 2:
        raise IsodoseError("a scatter grid needs a header row of offsets and at least one row")

    def split(line: str) -> list[str]:
        if "\t" in line:
            return [c.strip() for c in line.split("\t")]
        if "," in line:
            return [c.strip() for c in line.split(",")]
        return line.split()

    header = split(rows[0])
    # A leading blank corner cell is optional.
    try:
        x_coords = [float(c) for c in header]
    except ValueError:
        try:
            x_coords = [float(c) for c in header[1:]]
        except ValueError as exc:
            raise IsodoseError(
                f"could not read the header row of column offsets: {header}"
            ) from exc

    y_coords: list[float] = []
    values: list[list[float | None]] = []
    for line in rows[1:]:
        parts = split(line)
        if len(parts) < 2:
            continue
        try:
            y_coords.append(float(parts[0]))
        except ValueError as exc:
            raise IsodoseError(f"row does not start with a row offset: {parts[0]!r}") from exc
        row: list[float | None] = []
        for cell in parts[1:]:
            if cell == "" or cell.upper() in ("NA", "N/A", "-"):
                row.append(None)
            else:
                try:
                    row.append(float(cell))
                except ValueError:
                    row.append(None)
        # Pad or trim so ragged rows still line up with the header.
        row = (row + [None] * len(x_coords))[: len(x_coords)]
        values.append(row)

    return x_coords, y_coords, values
