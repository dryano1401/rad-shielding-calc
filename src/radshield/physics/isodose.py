"""Manufacturer scatter isodose maps.

Vendors publish scatter as a grid of air kerma values on a plane through the
isocentre -- a plan view looking down, and an elevation view looking from the
side.  This module turns such a grid into a value at an arbitrary point.

The chart is a map of the room laid over the plan with its origin on the
isocentre.  So the primary operation is simply to **read it where the point
of interest sits** -- the published value at that spot already accounts for
both the direction and the distance, and scaling it again by inverse square
would count the distance twice.

Reading inside the chart is a bilinear interpolation between the four
surrounding cells, falling back to the nearest cell where the chart is masked
(the gantry footprint, the pedestal) and no complete set of four exists.

Inverse square is used **only** where the chart genuinely does not reach: a
point beyond the printed grid, or a point on another storey with no elevation
chart available.  For those, each cell is normalised to

    S = K * r^2         [mGy m^2 per unit of workload]

the scatter strength on that bearing, which is independent of distance, and
the value follows as ``K = S / d^2``.  On real charts ``S`` is impressively
steady: along the table axis of the chart used to develop this it holds to
within 5% from 0.5 m out to 2.5 m, which is what makes the extrapolation
trustworthy where it is needed.

When extrapolating, the **largest** ``S`` on the bearing is used rather than
the nearest in radius.  Charts contain shadowed cells -- the pedestal column
reads an order of magnitude below its neighbours -- and picking by radius
makes the answer jump by that order of magnitude as the point moves a little
further out, in the unsafe direction.  A wall is wide and such shadows are
narrow, so the unshadowed envelope is both conservative and what a physicist
reading the printed chart would take.
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

# What a chart value is quoted per, and how a weekly total is formed from it.
# Charts published per mAs or per 100 mAs are scaled by the weekly workload
# instead of a procedure count.
WORKLOAD_BASIS: dict[str, str] = {
    "procedure": "procedures per week",
    "scan": "scans per week",
    "mAs": "mAs per week",
    "100 mAs": "mAs per week / 100",
}


def weekly_multiplier(per: str, procedures_per_week: float, mas_per_week: float) -> float:
    """How many chart-units of workload occur in a week.

    A chart quoted per procedure is multiplied by the procedure count; one
    quoted per mAs is multiplied by the weekly workload, and per 100 mAs by a
    hundredth of it.
    """
    if per not in WORKLOAD_BASIS:
        raise IsodoseError(f"unknown chart basis {per!r}; known: {sorted(WORKLOAD_BASIS)}")
    if per == "mAs":
        return mas_per_week
    if per == "100 mAs":
        return mas_per_week / 100.0
    return procedures_per_week

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
    xs: list[float] = field(default_factory=list)
    ys: list[float] = field(default_factory=list)
    grid: dict[tuple[int, int], float] = field(default_factory=dict)

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

    @property
    def extent(self) -> tuple[float, float, float, float]:
        """``(min_x, max_x, min_y, max_y)`` of the printed grid, in metres."""
        return min(self.xs), max(self.xs), min(self.ys), max(self.ys)

    def covers(self, x_m: float, y_m: float) -> bool:
        """True when a position falls inside the printed grid."""
        min_x, max_x, min_y, max_y = self.extent
        return min_x <= x_m <= max_x and min_y <= y_m <= max_y


@dataclass(frozen=True)
class Sample:
    """The result of reading a scatter map on one bearing."""

    value_mGy: float
    distance_m: float
    method: str = "read"
    x_m: float = 0.0
    y_m: float = 0.0
    cell: Cell | None = None
    bearing_deg: float = 0.0
    angle_error_deg: float = 0.0
    extrapolation_ratio: float = 1.0
    strength: float = 0.0
    cells_considered: int = 1
    notes: tuple[str, ...] = ()

    @property
    def is_extrapolated(self) -> bool:
        """True when inverse square was used because the chart did not reach."""
        return self.method == "extrapolated"

    def describe(self) -> str:
        """One line explaining the read, for the audit trail."""
        position = f"({self.x_m:+.2f}, {self.y_m:+.2f}) m from the isocentre"
        if self.method == "interpolated":
            return (
                f"chart read at {position} by interpolation between the surrounding "
                f"cells: {self.value_mGy:.4g} mGy"
            )
        if self.method == "nearest":
            assert self.cell is not None
            return (
                f"chart read at {position} from the nearest cell "
                f"({self.cell.x_m:+.2f}, {self.cell.y_m:+.2f}) m, "
                f"{self.cell.value_mGy:g} mGy"
            )
        assert self.cell is not None
        return (
            f"{position} lies beyond the chart, so the cell at "
            f"({self.cell.x_m:+.2f}, {self.cell.y_m:+.2f}) m on the same bearing "
            f"({self.cell.value_mGy:g} mGy at r = {self.cell.radius_m:.2f} m) was scaled by "
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
    flip_x: bool = False,
    flip_y: bool = False,
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
        flip_x: Mirror the column axis. A vendor's own printed left/right or
            front/back convention does not always match the source's rotation
            arrow, and there is no way to know which from the grid alone --
            this corrects it without touching the grid as pasted.
        flip_y: Mirror the row axis.

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

    length_scale = COORDINATE_UNITS[coordinate_unit] * (-1.0 if flip_x else 1.0)
    height_scale = COORDINATE_UNITS[coordinate_unit] * (-1.0 if flip_y else 1.0)
    value_scale = VALUE_UNITS[value_unit]

    cells: list[Cell] = []
    grid: dict[tuple[int, int], float] = {}
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
                    y_m=y_coords[row_index] * height_scale,
                    value_mGy=float(value) * value_scale,
                )
            )
            grid[(column_index, row_index)] = float(value) * value_scale

    return ScatterMap(
        name=name,
        plane=plane,
        cells=cells,
        per=per,
        source=source,
        xs=[c * length_scale for c in x_coords],
        ys=[c * height_scale for c in y_coords],
        grid=grid,
    )


def sample_at(
    scatter_map: ScatterMap,
    x_m: float,
    y_m: float,
    *,
    angle_tolerance_deg: float = DEFAULT_ANGLE_TOLERANCE_DEG,
) -> Sample:
    """Read the chart at a position expressed in the chart's own axes.

    Inside the printed grid the chart value is returned as published, with no
    distance correction: the chart already accounts for the distance to that
    spot, and scaling it again would count the distance twice.  Beyond the
    grid, the value is extrapolated along the bearing by inverse square.

    Args:
        x_m: Offset from the isocentre along the chart's x axis.
        y_m: Offset along the chart's y axis.

    Returns:
        A :class:`Sample` recording the value and how it was obtained.
    """
    distance = math.hypot(x_m, y_m)
    if distance <= 0:
        raise IsodoseError("cannot read a scatter chart at the isocentre itself")

    if scatter_map.covers(x_m, y_m):
        interpolated = _interpolate(scatter_map, x_m, y_m)
        if interpolated is not None:
            return Sample(
                value_mGy=interpolated,
                distance_m=distance,
                method="interpolated",
                x_m=x_m,
                y_m=y_m,
                bearing_deg=math.degrees(math.atan2(y_m, x_m)),
            )
        nearest = min(
            scatter_map.cells,
            key=lambda c: (c.x_m - x_m) ** 2 + (c.y_m - y_m) ** 2,
        )
        return Sample(
            value_mGy=nearest.value_mGy,
            distance_m=distance,
            method="nearest",
            x_m=x_m,
            y_m=y_m,
            cell=nearest,
            bearing_deg=math.degrees(math.atan2(y_m, x_m)),
            notes=(
                "the chart is masked around this position, so the nearest printed cell "
                "was used without interpolation",
            ),
        )

    reading = extrapolate(
        scatter_map,
        math.degrees(math.atan2(y_m, x_m)),
        distance,
        angle_tolerance_deg=angle_tolerance_deg,
    )
    return Sample(
        value_mGy=reading.value_mGy,
        distance_m=distance,
        method="extrapolated",
        x_m=x_m,
        y_m=y_m,
        cell=reading.cell,
        bearing_deg=reading.bearing_deg,
        angle_error_deg=reading.angle_error_deg,
        extrapolation_ratio=reading.extrapolation_ratio,
        strength=reading.strength,
        cells_considered=reading.cells_considered,
        notes=reading.notes,
    )


def _interpolate(scatter_map: ScatterMap, x_m: float, y_m: float) -> float | None:
    """Bilinear value at a position, or None where the four cells are not all printed."""
    xs, ys = scatter_map.xs, scatter_map.ys
    if len(xs) < 2 or len(ys) < 2:
        return None

    def bracket(values: list[float], target: float) -> tuple[int, int, float] | None:
        ordered = sorted(range(len(values)), key=lambda i: values[i])
        for low, high in zip(ordered, ordered[1:]):
            if values[low] <= target <= values[high]:
                span = values[high] - values[low]
                fraction = 0.0 if span == 0 else (target - values[low]) / span
                return low, high, fraction
        return None

    column = bracket(xs, x_m)
    row = bracket(ys, y_m)
    if column is None or row is None:
        return None

    left, right, fx = column
    bottom, top, fy = row
    try:
        corners = (
            scatter_map.grid[(left, bottom)],
            scatter_map.grid[(right, bottom)],
            scatter_map.grid[(left, top)],
            scatter_map.grid[(right, top)],
        )
    except KeyError:
        return None

    lower = corners[0] * (1 - fx) + corners[1] * fx
    upper = corners[2] * (1 - fx) + corners[3] * fx
    return lower * (1 - fy) + upper * fy


def extrapolate(
    scatter_map: ScatterMap,
    bearing_deg: float,
    distance_m: float,
    *,
    angle_tolerance_deg: float = DEFAULT_ANGLE_TOLERANCE_DEG,
) -> Sample:
    """Project the chart out to a distance it does not cover, along a bearing.

    Used where the chart genuinely cannot answer: a point past the edge of the
    printed grid, or on another storey with no elevation chart.  The strongest
    cell on the bearing sets the scatter strength -- see the module docstring
    for why the envelope beats the nearest cell.
    """
    if distance_m <= 0:
        raise IsodoseError(f"distance must be positive, got {distance_m}")

    def angular_error(cell: Cell) -> float:
        difference = (cell.bearing_deg - bearing_deg + 180.0) % 360.0 - 180.0
        return abs(difference)

    best_error = min(angular_error(cell) for cell in scatter_map.cells)
    candidates = [c for c in scatter_map.cells if angular_error(c) <= best_error + 1.0]
    cell = max(candidates, key=lambda c: c.strength)

    notes: list[str] = []
    if best_error > angle_tolerance_deg:
        notes.append(
            f"nearest chart bearing is {best_error:.0f} deg away from the direction to this "
            "point; the chart may not cover it"
        )

    weakest = min(c.strength for c in candidates)
    if weakest > 0 and cell.strength / weakest > 2.0:
        notes.append(
            f"cells on this bearing disagree by a factor of {cell.strength / weakest:.1f} "
            f"({len(candidates)} cells); the strongest was used, which is the conservative "
            "reading where part of the chart is shadowed by the pedestal or gantry"
        )

    ratio = distance_m / cell.radius_m
    if ratio > 3.0:
        notes.append(
            f"the chart value is being projected {ratio:.1f} times further out than the "
            "cell it came from"
        )

    return Sample(
        value_mGy=cell.strength / distance_m**2,
        distance_m=distance_m,
        method="extrapolated",
        x_m=distance_m * math.cos(math.radians(bearing_deg)),
        y_m=distance_m * math.sin(math.radians(bearing_deg)),
        cell=cell,
        bearing_deg=bearing_deg,
        angle_error_deg=best_error,
        extrapolation_ratio=ratio,
        strength=cell.strength,
        cells_considered=len(candidates),
        notes=tuple(notes),
    )


# A pasted chart's negative offsets sometimes carry a true minus sign or a
# dash (copied out of a PDF or typed by hand) instead of an ASCII hyphen.
_MINUS_VARIANTS = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-",
    "–": "-", "—": "-", "−": "-",
})


def _to_float(text: str) -> float:
    return float(text.translate(_MINUS_VARIANTS))


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
        x_coords = [_to_float(c) for c in header]
    except ValueError:
        try:
            x_coords = [_to_float(c) for c in header[1:]]
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
            y_coords.append(_to_float(parts[0]))
        except ValueError as exc:
            raise IsodoseError(f"row does not start with a row offset: {parts[0]!r}") from exc
        row: list[float | None] = []
        for cell in parts[1:]:
            if cell == "" or cell.upper() in ("NA", "N/A", "-"):
                row.append(None)
            else:
                try:
                    row.append(_to_float(cell))
                except ValueError:
                    row.append(None)
        # Pad or trim so ragged rows still line up with the header.
        row = (row + [None] * len(x_coords))[: len(x_coords)]
        values.append(row)

    return x_coords, y_coords, values
