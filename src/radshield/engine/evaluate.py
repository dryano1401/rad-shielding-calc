"""Bind the project model to the physics engines and aggregate per point.

Every source linked to a point contributes.  Doses are summed *within a
methodology* before the transmission factor is derived, matching TG-108
Table VII.  Where a point sees both TG-108 and NCRP 147 sources, each is
still solved on its own for the audit trail, but the two are also summed:
1 uGy air kerma and 1 uSv effective dose equivalent coincide for the photon
energies both methodologies model here (radiation weighting factor 1), so
a wall thick enough for one alone is not necessarily thick enough for both
together -- two sources each individually under the weekly goal can still
add up to more than it.  ``_solve_combined`` finds the thickness that meets
the summed dose by bisection, since the two attenuate at different rates
(different photon energies) and there is no closed form the way there is
for one methodology alone; that combined figure is what governs, not the
larger of the two independent requirements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..model.geometry import (
    Crossing,
    Distance,
    chart_direction,
    distance,
    format_length,
    path_barriers,
)
from ..model.project import PointOfInterest, Project, SourcePoint
from ..physics import isodose, nuclides, tg108
from ..physics.archer import equivalent_thickness as archer_equivalent
from ..physics.archer import thickness as archer_thickness
from ..physics.archer import transmission as archer_transmission
from ..physics.limits import ncrp147_goal, tg108_goal
from ..physics.ncrp147 import barriers as ncrp_barriers
from ..physics.ncrp147 import ct as ncrp_ct
from ..physics.ncrp147 import tables as ncrp_tables

# Materials each methodology can currently attenuate, by tabulated data.
TG108_MATERIALS = {"lead", "concrete", "iron"}
NCRP147_MATERIALS = {"lead", "concrete", "gypsum", "steel", "glass", "wood"}

_MM_PER_UNIT = {"mm": 1.0, "cm": 10.0}


@dataclass
class SourceContribution:
    """One source's contribution to the dose at a point."""

    source_id: str
    label: str
    method: str
    distance_m: float
    quantity: str
    value: float
    terms: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    geometric_distance_m: float | None = None
    unshielded_value: float = 0.0
    barriers: list[dict[str, Any]] = field(default_factory=list)
    path_transmission: float = 1.0
    path_equivalent_mm: float = 0.0
    components: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MethodResult:
    """Solved requirement for one methodology at one point.

    ``total`` is the summed dose as things currently stand -- attenuated by
    whatever walls the floor plan already shows crossing each source's path,
    but not by the ``thickness_mm`` this same result proposes adding.
    ``unshielded_total`` is the same sum with no attenuation at all, the sum
    of every contribution's own ``unshielded_value``; the ratio of the two is
    what the existing walls are already buying.
    """

    method: str
    quantity: str
    total: float
    required_transmission: float
    thickness_mm: dict[str, float] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    unshielded_total: float = 0.0


@dataclass
class PointResult:
    """Everything computed for a single point of interest."""

    poi_id: str
    label: str
    floor_name: str
    occupancy: float
    area_class: str
    contributions: list[SourceContribution] = field(default_factory=list)
    methods: list[MethodResult] = field(default_factory=list)
    governing_thickness_mm: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def shielding_required(self) -> bool:
        """True when any methodology demands a non-zero barrier."""
        return any(v > 0 for v in self.governing_thickness_mm.values())


def _existing_credit_mm(poi: PointOfInterest, material: str, unit: str) -> float:
    """Existing shielding credit for ``material``, converted to millimetres."""
    if poi.existing_material != material or poi.existing_thickness <= 0:
        return 0.0
    return poi.existing_thickness * _MM_PER_UNIT[unit]


def _tg108_component(entry: dict[str, Any], fallback_label: str) -> tg108.PatientSource:
    """Build one isotope's physics source from its stored parameters."""
    nuclide = entry.get("nuclide", "F-18")
    return tg108.PatientSource(
        kind=entry.get("kind", "uptake"),
        nuclide=nuclide,
        administered_activity_MBq=float(entry.get("administered_activity_MBq", 0.0) or 0.0),
        patients_per_week=float(entry.get("patients_per_week", 0.0) or 0.0),
        uptake_time_h=float(entry.get("uptake_time_h", 0.0) or 0.0),
        imaging_time_h=float(entry.get("imaging_time_h", 0.0) or 0.0),
        void_factor=float(entry.get("void_factor", tg108.DEFAULT_VOID_FACTOR) or 1.0),
        scanner_attenuation=float(entry.get("scanner_attenuation", 1.0) or 1.0),
        label=entry.get("label") or f"{nuclide} ({fallback_label})",
    )


def _tg108_components(source: SourcePoint) -> list[tg108.PatientSource]:
    """Every isotope a source region runs, as physics sources.

    A source carrying a ``components`` list runs several tracers from one
    place, each with its own activity, patient load and timings, and so its
    own decay factors.  A source without one is a single isotope described by
    the parameters directly, which is how projects were written before mixes
    were supported.
    """
    entries = source.params.get("components")
    if entries:
        return [_tg108_component(entry, source.label or source.id) for entry in entries]
    return [_tg108_component(source.params, source.label or source.id)]


def _ncrp_inputs(source: SourcePoint, dist: float, occupancy: float) -> ncrp_barriers.XrayBarrierInputs:
    """Build NCRP 147 barrier inputs from the stored parameters."""
    p = source.params
    patients = p.get("patients_per_week")
    return ncrp_barriers.XrayBarrierInputs(
        workload=p.get("workload", "Rad Room (all barriers)"),
        distance_m=dist,
        occupancy=occupancy,
        patients_per_week=float(patients) if patients not in (None, "") else None,
        use_factor=float(p.get("use_factor", 1.0)),
        barrier_type=p.get("barrier_type", "secondary"),
        scatter_geometry=p.get("scatter_geometry", "side"),
        label=source.label or source.id,
    )


def _ct_inputs(source: SourcePoint, dist: float, occupancy: float) -> ncrp_ct.CTBarrierInputs:
    """Build CT barrier inputs from the stored parameters."""
    p = source.params
    scatter = ncrp_ct.CTScatterModel(
        method=p.get("scatter_method", "dlp"),
        body_region=p.get("body_region", "body"),
        kappa_per_cm=_optional_float(p.get("kappa_per_cm")),
        region_factor=_optional_float(p.get("region_factor")),
        isodose_kerma_mGy_at_1m=_optional_float(p.get("isodose_kerma_mGy_at_1m")),
        source=p.get("scatter_source", ""),
    )
    return ncrp_ct.CTBarrierInputs(
        scatter=scatter,
        distance_m=dist,
        occupancy=occupancy,
        procedures_per_week=float(p.get("procedures_per_week", 0.0)),
        dlp_per_procedure_mGy_cm=_optional_float(p.get("dlp_per_procedure_mGy_cm"))
        if scatter.method == "dlp"
        else None,
        kvp=float(p.get("kvp", 125)),
        label=source.label or source.id,
    )


def _scatter_map(project: Project, map_id: str) -> isodose.ScatterMap:
    """Build a usable scatter map from the project's stored grid."""
    stored = project.scatter_map(map_id)
    return isodose.build_map(
        name=stored.name,
        plane=stored.plane,
        x_coords=stored.x_coords,
        y_coords=stored.y_coords,
        values=stored.values,
        coordinate_unit=stored.coordinate_unit,
        value_unit=stored.value_unit,
        per=stored.per,
        source=stored.source,
        flip_x=stored.flip_x,
        flip_y=stored.flip_y,
    )


def _read_chart(
    project: Project, source: SourcePoint, poi: PointOfInterest, distance_m: float
) -> tuple[float, tuple[str, ...]]:
    """Sample the source's scatter chart at a point.

    The chart is laid over the plan with its origin on the isocentre, so where
    it reaches the point it is simply read there: the published value already
    accounts for the distance to that spot.  Inverse square is reserved for
    points the chart does not cover.

    An elevation chart is preferred when the point is on another floor, since
    that is the view describing what leaves the gantry vertically.

    ``distance_m`` is the distance the rest of the calculation is using,
    including the NCRP standoff and any entered override.  Where it differs
    from the raw point-to-point distance, the read position is moved outward
    to match, so one distance governs throughout.

    Returns:
        ``(kerma per unit of workload at the point in mGy, notes)``.
    """
    params = source.params
    plan_id = params.get("plan_map_id") or ""
    elevation_id = params.get("elevation_map_id") or ""

    cross_floor = source.floor_id != poi.floor_id
    use_elevation = cross_floor and bool(elevation_id)
    map_id = elevation_id if use_elevation else plan_id
    if not map_id:
        raise ValueError(
            "this CT source uses the manufacturer chart method but no chart is assigned"
        )
    plane = "elevation" if use_elevation else "plan"

    scatter_map = _scatter_map(project, map_id)
    direction = chart_direction(project, source, poi, plane=plane)

    # Scale the chart-frame position so its radius matches the distance the
    # calculation is using; without this a standoff or an override would move
    # the point but not the place the chart is read.
    x_m, y_m = direction.x_m, direction.y_m
    adjusted = abs(distance_m - direction.distance_m) > 1e-9
    if adjusted and direction.distance_m > 0:
        stretch = distance_m / direction.distance_m
        x_m, y_m = x_m * stretch, y_m * stretch

    reading = isodose.sample_at(scatter_map, x_m, y_m)

    notes = (
        f"{scatter_map.name} ({plane} view), {direction.note}",
        reading.describe(),
    ) + reading.notes
    if adjusted:
        notes += (
            f"chart read at {distance_m:.2f} m, the distance used throughout this "
            f"calculation, rather than the {direction.distance_m:.2f} m straight from "
            "the placed points",
        )
    if not reading.is_extrapolated:
        notes += (
            "the chart covers this point, so its published value is used as it stands "
            "with no inverse-square correction",
        )
    if cross_floor and not elevation_id:
        notes += (
            "no elevation chart assigned, so the plan chart was used for a point on "
            "another floor; the vertical separation is not represented",
        )
    return reading.value_mGy, notes


def _chart_basis(project: Project, source: SourcePoint) -> str:
    """What the source's chart quotes its values per."""
    for key in ("plan_map_id", "elevation_map_id"):
        map_id = source.params.get(key)
        if map_id:
            try:
                return project.scatter_map(map_id).per
            except KeyError:
                continue
    return "procedure"


def _optional_float(value: Any) -> float | None:
    """Coerce a form value to float, treating blanks as absent."""
    if value in (None, ""):
        return None
    return float(value)


def _native_thickness(thickness_mm: float, unit: str) -> float:
    """Convert millimetres to the unit an Archer parameter set is tabulated in."""
    return thickness_mm if unit == "mm" else thickness_mm / 10.0


# Reference material a barrier stack is reduced to before the fit is applied.
# Lead is tabulated by both methodologies, so it always resolves.
REFERENCE_MATERIAL = "lead"


def _path_attenuation(
    crossings: list[Crossing], params_for: Any
) -> tuple[float, float, list[dict[str, Any]], list[str]]:
    """Reduce a stack of barriers to one reference thickness and its transmission.

    Each barrier is converted to the equivalent thickness of the reference
    material, the equivalents are summed, and the fit is applied once.  This
    is preferred over multiplying the barriers' individual transmissions,
    which ignores beam hardening between layers and would overstate the
    protection achieved.

    Barriers whose material the active methodology cannot attenuate are
    dropped with a warning rather than guessed at; dropping a barrier
    understates shielding, so the result stays conservative.

    Returns:
        ``(transmission, equivalent_mm, details, warnings)``.
    """
    warnings: list[str] = []
    details: list[dict[str, Any]] = []
    if not crossings:
        return 1.0, 0.0, details, warnings

    try:
        reference = params_for(REFERENCE_MATERIAL)
    except Exception as exc:
        return 1.0, 0.0, details, [f"no reference transmission data available: {exc}"]

    total_equivalent = 0.0
    for crossing in crossings:
        try:
            params = params_for(crossing.material)
        except Exception as exc:
            warnings.append(
                f"{crossing.label}: no transmission data for {crossing.material!r} under this "
                f"methodology, so the barrier was ignored (conservative). {exc}"
            )
            continue
        native = _native_thickness(crossing.effective_thickness_mm, params.unit)
        equivalent = archer_equivalent(params, native, reference)
        total_equivalent += equivalent
        details.append(
            {
                "label": crossing.label,
                "material": crossing.material,
                "thickness_mm": round(crossing.thickness_mm, 2),
                "effective_thickness_mm": round(crossing.effective_thickness_mm, 2),
                "angle_deg": round(crossing.angle_deg, 1),
                "oblique": crossing.is_oblique,
                "equivalent_mm": round(
                    equivalent * (1.0 if reference.unit == "mm" else 10.0), 3
                ),
                "equivalent_material": REFERENCE_MATERIAL,
                "wall_id": crossing.wall_id,
                "floor_name": crossing.floor_name,
            }
        )

    if total_equivalent <= 0:
        return 1.0, 0.0, details, warnings

    transmission = archer_transmission(reference, total_equivalent)
    equivalent_mm = total_equivalent * (1.0 if reference.unit == "mm" else 10.0)
    return transmission, equivalent_mm, details, warnings


def _solve_tg108(
    project: Project,
    poi: PointOfInterest,
    pairs: list[tuple[SourcePoint, Distance]],
    materials: list[str],
) -> tuple[MethodResult, list[SourceContribution], str]:
    """Attenuate each TG-108 source by its own path, sum, then solve.

    Returns the nuclide used for the group's shared 511 keV Archer fit as a
    third element, alongside the result and contributions, so a point also
    seeing NCRP 147 sources can reuse the same fit when solving the combined
    dose rather than re-deriving it.
    """
    goal = tg108_goal(poi.area_class)

    # Every isotope in play emits the same 511 keV annihilation photon, so one
    # transmission fit serves the lot; F-18 stands in when a mix is present.
    nuclide_names = {
        component.nuclide for src, _ in pairs for component in _tg108_components(src)
    }
    attenuation_nuclide = "F-18" if len(nuclide_names) > 1 else next(iter(nuclide_names))
    params_for = lambda material: nuclides.get_archer(attenuation_nuclide, material)

    usable = [m for m in materials if m in TG108_MATERIALS]
    unavailable = {
        m: "no 511 keV transmission data registered for this material"
        for m in materials
        if m not in TG108_MATERIALS
    }

    contributions: list[SourceContribution] = []
    total = 0.0
    unshielded_total = 0.0
    for src, dist in pairs:
        components = _tg108_components(src)
        combined = tg108.combined_weekly_dose(components, dist.metres)
        unshielded_total += combined.weekly_dose_uSv
        crossings, geometry_warnings = path_barriers(
            project, src, poi, apply_obliquity=project.apply_obliquity
        )
        b_path, equivalent_mm, details, barrier_warnings = _path_attenuation(
            crossings, params_for
        )
        attenuated = combined.weekly_dose_uSv * b_path
        total += attenuated

        per_nuclide = [
            {
                "label": dose.source_label,
                "nuclide": component.nuclide,
                "kind": component.kind,
                "unshielded_uSv": dose.weekly_dose_uSv,
                "value_uSv": dose.weekly_dose_uSv * b_path,
                "terms": dose.terms,
                "notes": list(dose.notes),
            }
            for component, dose in zip(components, combined.per_nuclide)
        ]

        single = len(combined.per_nuclide) == 1
        dose = combined.per_nuclide[0]
        notes = list(dose.notes) if single else [
            f"{len(per_nuclide)} isotopes from this region, each decayed on its own "
            "half-life and timings, then summed"
        ]
        notes += dist.notes + geometry_warnings + barrier_warnings
        if details:
            notes.append(
                f"path crosses {len(details)} barrier(s), "
                f"{equivalent_mm:.2f} mm lead equivalent, transmission {b_path:.4g}"
            )
        contributions.append(
            SourceContribution(
                source_id=src.id,
                label=src.label or src.id,
                method="tg108",
                distance_m=dist.metres,
                quantity="uSv/week",
                value=attenuated,
                unshielded_value=combined.weekly_dose_uSv,
                terms=dose.terms if single else {
                    d["label"]: round(d["unshielded_uSv"], 4) for d in per_nuclide
                },
                components=per_nuclide,
                notes=notes,
                geometric_distance_m=dist.geometric_m,
                barriers=details,
                path_transmission=b_path,
                path_equivalent_mm=equivalent_mm,
            )
        )

    b_required = tg108.required_transmission(total, goal, poi.occupancy)

    thickness_mm: dict[str, float] = {}
    for material in usable:
        params = params_for(material)
        gross = archer_thickness(params, b_required) if b_required < 1.0 else 0.0
        gross_mm = gross * (1.0 if params.unit == "mm" else 10.0)
        thickness_mm[material] = max(gross_mm - _existing_credit_mm(poi, material, params.unit), 0.0)

    return (
        MethodResult(
            method="tg108",
            quantity="effective dose equivalent (uSv/week)",
            total=total,
            required_transmission=b_required,
            thickness_mm=thickness_mm,
            unavailable=unavailable,
            unshielded_total=unshielded_total,
        ),
        contributions,
        attenuation_nuclide,
    )


def _solve_ncrp147(
    project: Project,
    poi: PointOfInterest,
    pairs: list[tuple[SourcePoint, Distance]],
    materials: list[str],
) -> tuple[MethodResult, list[SourceContribution]]:
    """Attenuate each NCRP 147 source by its own path, sum, then solve."""
    goal = ncrp147_goal(poi.area_class)
    evaluated: list[tuple[Any, bool]] = []
    contributions: list[SourceContribution] = []

    for src, dist in pairs:
        is_ct = src.method == "ncrp147_ct"
        if is_ct:
            inputs = _ct_inputs(src, dist.metres, poi.occupancy)
            if inputs.scatter.method == "chart":
                kerma, chart_notes = _read_chart(project, src, poi, dist.metres)
                basis = _chart_basis(project, src)
                units = isodose.weekly_multiplier(
                    basis,
                    float(src.params.get("procedures_per_week", 0.0) or 0.0),
                    float(src.params.get("mas_per_week", 0.0) or 0.0),
                )
                res = ncrp_ct.evaluate_from_chart(
                    inputs, goal, kerma, chart_notes, workload_per_week=units, basis=basis
                )
            else:
                res = ncrp_ct.evaluate(inputs, goal)
            params_for = lambda material, i=inputs: ncrp_ct.barrier_params(i, material)
        else:
            inputs = _ncrp_inputs(src, dist.metres, poi.occupancy)
            res = ncrp_barriers.evaluate(inputs, goal)
            params_for = lambda material, i=inputs: ncrp_barriers.barrier_params(i, material)
        evaluated.append((res, is_ct))

        crossings, geometry_warnings = path_barriers(
            project, src, poi, apply_obliquity=project.apply_obliquity
        )
        b_path, equivalent_mm, details, barrier_warnings = _path_attenuation(
            crossings, params_for
        )
        attenuated = res.unshielded_weekly_kerma_mGy * b_path

        notes = list(res.notes) + dist.notes + geometry_warnings + barrier_warnings
        if details:
            notes.append(
                f"path crosses {len(details)} barrier(s), "
                f"{equivalent_mm:.2f} mm lead equivalent, transmission {b_path:.4g}"
            )
        contributions.append(
            SourceContribution(
                source_id=src.id,
                label=src.label or src.id,
                method=src.method,
                distance_m=dist.metres,
                quantity="mGy/week",
                value=attenuated,
                unshielded_value=res.unshielded_weekly_kerma_mGy,
                terms=res.terms,
                notes=notes,
                geometric_distance_m=dist.geometric_m,
                barriers=details,
                path_transmission=b_path,
                path_equivalent_mm=equivalent_mm,
            )
        )

    total = sum(c.value for c in contributions)
    unshielded_total = sum(c.unshielded_value for c in contributions)
    b_required = float("inf") if total <= 0 else goal.value / (poi.occupancy * total)

    thickness_mm: dict[str, float] = {}
    unavailable: dict[str, str] = {}
    for material in materials:
        if material not in NCRP147_MATERIALS:
            unavailable[material] = "not an NCRP 147 tabulated material"
            continue
        if b_required >= 1.0:
            thickness_mm[material] = 0.0
            continue
        try:
            candidates = [
                _ct_thickness_for(res, material, b_required)
                if is_ct
                else _thickness_for(res, material, b_required)
                for res, is_ct in evaluated
            ]
        except ncrp_tables.TableLookupError as exc:
            unavailable[material] = str(exc)
            continue
        credit = _existing_credit_mm(poi, material, "mm")
        thickness_mm[material] = max(max(candidates, default=0.0) - credit, 0.0)

    return (
        MethodResult(
            method="ncrp147",
            quantity="air kerma (mGy/week)",
            total=total,
            required_transmission=b_required,
            thickness_mm=thickness_mm,
            unavailable=unavailable,
            unshielded_total=unshielded_total,
        ),
        contributions,
    )


def _params_for_ncrp_source(source: SourcePoint, material: str) -> Any:
    """One NCRP 147 source's own Archer fit for ``material``.

    Distance and occupancy don't affect which fit applies -- only workload,
    barrier type and (for CT) kVp do -- so this rebuilds just enough of the
    source's inputs to look it up, the same dummy-distance approach
    ``reference_dose()`` already uses for its 1 m sanity figure.
    """
    if source.method == "ncrp147_ct":
        return ncrp_ct.barrier_params(_ct_inputs(source, 1.0, 1.0), material)
    return ncrp_barriers.barrier_params(_ncrp_inputs(source, 1.0, 1.0), material)


def _combined_dose_uSv_at(
    thickness_mm: float,
    material: str,
    project: Project,
    tg108_total_uSv: float,
    tg108_params: Any | None,
    ncrp147_contributions: list[SourceContribution],
) -> float | None:
    """Total TG-108 + NCRP 147 dose (uSv) through ``thickness_mm`` of ``material``.

    None means at least one contributing source has no transmission data for
    this material -- refusing to guess rather than silently ignoring part of
    the real dose, the same choice ``_path_attenuation`` makes for a barrier
    of unrecognised material.
    """
    total = 0.0
    if tg108_total_uSv > 0:
        if tg108_params is None:
            return None
        native = _native_thickness(thickness_mm, tg108_params.unit)
        total += tg108_total_uSv * archer_transmission(tg108_params, native)
    for contribution in ncrp147_contributions:
        if contribution.value <= 0:
            continue
        try:
            params = _params_for_ncrp_source(project.source(contribution.source_id), material)
        except Exception:
            return None
        native = _native_thickness(thickness_mm, params.unit)
        total += contribution.value * 1000.0 * archer_transmission(params, native)
    return total


def _solve_combined_thickness(
    material: str,
    goal_uSv: float,
    project: Project,
    tg108_total_uSv: float,
    tg108_params: Any | None,
    ncrp147_contributions: list[SourceContribution],
) -> float | None:
    """Thickness of ``material`` (mm) bringing the combined dose to ``goal_uSv``.

    TG-108 and NCRP 147 attenuate at different rates through the same
    material -- different photon energies -- so there is no closed form the
    way there is for one methodology alone. Bisected instead, which is safe
    because transmission is monotonically decreasing in thickness for both.

    Returns:
        The thickness in mm, ``inf`` if the goal can't be met with this
        material, or ``None`` if a contributing source lacks the data to
        evaluate it at all.
    """
    at = lambda t: _combined_dose_uSv_at(
        t, material, project, tg108_total_uSv, tg108_params, ncrp147_contributions
    )
    baseline = at(0.0)
    if baseline is None:
        return None
    if baseline <= goal_uSv:
        return 0.0

    lo, hi = 0.0, 10.0
    while True:
        value = at(hi)
        if value is None:
            return None
        if value <= goal_uSv:
            break
        hi *= 2
        if hi > 1e5:
            return float("inf")
    for _ in range(60):
        mid = (lo + hi) / 2.0
        value = at(mid)
        if value is None:
            return None
        if value > goal_uSv:
            lo = mid
        else:
            hi = mid
    return hi


def _solve_combined(
    project: Project,
    poi: PointOfInterest,
    tg108_result: MethodResult,
    tg108_nuclide: str,
    ncrp147_result: MethodResult,
    ncrp147_contributions: list[SourceContribution],
    materials: list[str],
) -> MethodResult:
    """Solve one barrier thickness against the *summed* TG-108 + NCRP 147 dose.

    1 uGy air kerma and 1 uSv effective dose equivalent coincide for the
    photon energies both methodologies model here (radiation weighting
    factor 1 -- see physics/limits.py), so a point seeing both source types
    needs one wall thick enough for their combined dose. Solving each
    methodology alone and taking the larger requirement, as before, can
    understate that: two sources each individually under the weekly goal can
    still add up to more than it.
    """
    goal_uSv = tg108_goal(poi.area_class).value
    combined_goal_uSv = goal_uSv / poi.occupancy

    thickness_mm: dict[str, float] = {}
    unavailable: dict[str, str] = {}
    for material in materials:
        tg108_params: Any | None = None
        if material in TG108_MATERIALS:
            try:
                tg108_params = nuclides.get_archer(tg108_nuclide, material)
            except Exception:
                tg108_params = None

        gross = _solve_combined_thickness(
            material, combined_goal_uSv, project,
            tg108_result.total, tg108_params, ncrp147_contributions,
        )
        if gross is None:
            unavailable[material] = (
                "at least one source has no transmission data for this material, so the "
                "combined TG-108 + NCRP 147 requirement can't be verified"
            )
        elif gross == float("inf"):
            unavailable[material] = "the combined dose cannot be brought under the goal with this material"
        else:
            thickness_mm[material] = max(gross - _existing_credit_mm(poi, material, "mm"), 0.0)

    combined_total_uSv = tg108_result.total + ncrp147_result.total * 1000.0
    combined_unshielded_uSv = (
        tg108_result.unshielded_total + ncrp147_result.unshielded_total * 1000.0
    )
    return MethodResult(
        method="combined",
        quantity="TG-108 + NCRP 147 combined (uSv/week; 1 uGy = 1 uSv)",
        total=combined_total_uSv,
        required_transmission=(
            float("inf") if combined_total_uSv <= 0
            else goal_uSv / (poi.occupancy * combined_total_uSv)
        ),
        thickness_mm=thickness_mm,
        unavailable=unavailable,
        unshielded_total=combined_unshielded_uSv,
    )


def _thickness_for(result: Any, material: str, b: float) -> float:
    """Thickness in mm for one evaluated x-ray barrier at transmission ``b``."""
    from ..physics.archer import thickness as archer_thickness

    return archer_thickness(ncrp_barriers.barrier_params(result.inputs, material), b)


def _ct_thickness_for(result: ncrp_ct.CTBarrierResult, material: str, b: float) -> float:
    """Thickness in mm for one evaluated CT barrier at transmission ``b``."""
    from ..physics.archer import thickness as archer_thickness

    return archer_thickness(ncrp_ct.barrier_params(result.inputs, material), b)


def evaluate_point(project: Project, poi: PointOfInterest) -> PointResult:
    """Evaluate one point of interest against every source linked to it."""
    floor = project.floor(poi.floor_id)
    result = PointResult(
        poi_id=poi.id,
        label=poi.label or poi.id,
        floor_name=floor.name,
        occupancy=poi.occupancy,
        area_class=poi.area_class,
    )

    if not poi.linked_source_ids:
        result.errors.append("no sources linked to this point")
        return result

    by_method: dict[str, list[tuple[SourcePoint, Distance]]] = {"tg108": [], "ncrp147": []}
    for source_id in poi.linked_source_ids:
        try:
            source = project.source(source_id)
        except KeyError:
            result.errors.append(f"linked source {source_id} no longer exists")
            continue
        try:
            dist = distance(
                project,
                source,
                poi,
                # TG-108 has no standoff convention of its own; its default
                # source-to-wall/floor distances are drawn directly from NCRP
                # guidance, so the same 0.3 m point-of-protection standoff
                # applies to it too, unless this point's offset is already
                # applied.
                apply_ncrp_standoff=not poi.offset_applied,
                override_m=poi.distance_overrides.get(source_id),
            )
        except Exception as exc:  # geometry problems are reported, not raised
            result.errors.append(f"{source.label or source.id}: {exc}")
            continue
        result.warnings.extend(dist.warnings)
        group = "tg108" if source.method == "tg108" else "ncrp147"
        by_method[group].append((source, dist))

    tg108_result: MethodResult | None = None
    tg108_nuclide: str | None = None
    ncrp147_result: MethodResult | None = None
    ncrp147_contributions: list[SourceContribution] = []

    for group, pairs in by_method.items():
        if not pairs:
            continue
        try:
            if group == "tg108":
                tg108_result, contributions, tg108_nuclide = _solve_tg108(
                    project, poi, pairs, project.materials
                )
                method_result = tg108_result
            else:
                ncrp147_result, contributions = _solve_ncrp147(
                    project, poi, pairs, project.materials
                )
                ncrp147_contributions = contributions
                method_result = ncrp147_result
        except Exception as exc:
            result.errors.append(f"{group}: {exc}")
            continue
        result.methods.append(method_result)
        result.contributions.extend(contributions)
        for material, mm in method_result.thickness_mm.items():
            result.governing_thickness_mm[material] = max(
                result.governing_thickness_mm.get(material, 0.0), mm
            )

    if tg108_result is not None and ncrp147_result is not None:
        combined = _solve_combined(
            project, poi, tg108_result, tg108_nuclide, ncrp147_result,
            ncrp147_contributions, project.materials,
        )
        result.methods.append(combined)
        # The combined row is what governs, not the larger of the two totals
        # taken independently -- two sources each under the goal alone can
        # still add up to more than it.
        result.governing_thickness_mm = dict(combined.thickness_mm)
        result.warnings.append(
            "this point sees both TG-108 and NCRP 147 sources; 1 uGy and 1 uSv coincide for "
            "the photon energies both model, so their doses are summed -- see the combined "
            "row, which is what governs rather than either methodology's own total alone"
        )

    return result


def evaluate_project(project: Project) -> list[PointResult]:
    """Evaluate every point of interest in the project."""
    return [evaluate_point(project, poi) for poi in project.pois]


def reference_dose(project: Project, source: SourcePoint) -> dict[str, Any]:
    """Unshielded weekly dose one metre from a source, for sanity checking.

    A single number that depends only on the source's own parameters, not on
    where anything was placed, so a mistyped activity or patient load shows up
    as an implausible figure straight away rather than surfacing later as a
    barrier thickness nobody expected.

    Returns:
        ``{"value", "unit", "note"}``, plus ``"components"`` for an isotope
        mix, or ``{"error"}`` where the source is not yet fully specified.
    """
    try:
        if source.method == "tg108":
            components = _tg108_components(source)
            combined = tg108.combined_weekly_dose(components, 1.0)
            result: dict[str, Any] = {
                "value": combined.weekly_dose_uSv,
                "unit": "uSv/week",
                "note": "unshielded, at 1 m",
            }
            if len(components) > 1:
                result["components"] = [
                    {"label": dose.source_label, "value": dose.weekly_dose_uSv}
                    for dose in combined.per_nuclide
                ]
            return result

        goal = ncrp147_goal("uncontrolled")
        if source.method == "ncrp147_ct":
            inputs = _ct_inputs(source, 1.0, 1.0)
            if inputs.scatter.method == "chart":
                basis = _chart_basis(project, source)
                units = isodose.weekly_multiplier(
                    basis,
                    float(source.params.get("procedures_per_week", 0.0) or 0.0),
                    float(source.params.get("mas_per_week", 0.0) or 0.0),
                )
                map_id = source.params.get("plan_map_id") or source.params.get(
                    "elevation_map_id"
                )
                if not map_id:
                    return {"error": "no scatter chart assigned"}
                scatter_map = _scatter_map(project, map_id)
                # The chart is directional, so quote its strongest bearing:
                # a peak rather than a single figure that would mislead.
                peak = max(cell.strength for cell in scatter_map.cells)
                return {
                    "value": peak * units,
                    "unit": "mGy/week",
                    "note": f"unshielded, peak bearing at 1 m, per {basis}",
                }
            return {
                "value": ncrp_ct.evaluate(inputs, goal).unshielded_weekly_kerma_mGy,
                "unit": "mGy/week",
                "note": "unshielded, at 1 m",
            }

        kerma, _, _ = ncrp_barriers.unshielded_kerma(_ncrp_inputs(source, 1.0, 1.0))
        return {"value": kerma, "unit": "mGy/week", "note": "unshielded, at 1 m"}
    except Exception as exc:
        return {"error": str(exc)}


def describe_distances(project: Project) -> list[dict[str, Any]]:
    """Per point, the distance to each linked source, without running the physics.

    Lets the interface display and edit distances before a calculation is run,
    and reports geometry failures per link rather than aborting the lot.
    """
    unit = project.display_unit
    report: list[dict[str, Any]] = []
    for poi in project.pois:
        links: list[dict[str, Any]] = []
        for source_id in poi.linked_source_ids:
            entry: dict[str, Any] = {"source_id": source_id, "override_m": poi.distance_overrides.get(source_id)}
            try:
                source = project.source(source_id)
            except KeyError:
                entry["error"] = "source no longer exists"
                links.append(entry)
                continue
            entry["label"] = source.label or source.id
            try:
                geometric = distance(
                    project,
                    source,
                    poi,
                    apply_ncrp_standoff=not poi.offset_applied,
                )
                used = distance(
                    project,
                    source,
                    poi,
                    apply_ncrp_standoff=not poi.offset_applied,
                    override_m=poi.distance_overrides.get(source_id),
                )
            except Exception as exc:
                entry["error"] = str(exc)
                links.append(entry)
                continue
            entry.update(
                geometric_m=geometric.metres,
                distance_m=used.metres,
                horizontal_m=used.horizontal_m,
                vertical_m=used.vertical_m,
                same_floor=used.same_floor,
                display=format_length(used.metres, unit),
                geometric_display=format_length(geometric.metres, unit),
                warnings=used.warnings,
                notes=used.notes,
            )
            links.append(entry)
        report.append({"poi_id": poi.id, "label": poi.label or poi.id, "links": links})
    return report


def describe_barriers(project: Project) -> list[dict[str, Any]]:
    """Per point, the barriers on the path from each linked source.

    Purely geometric, so the interface can show which walls are in the way
    before any calculation runs.  Equivalent thicknesses are not computed here
    because they depend on the methodology evaluating the path.
    """
    report: list[dict[str, Any]] = []
    for poi in project.pois:
        links: list[dict[str, Any]] = []
        for source_id in poi.linked_source_ids:
            entry: dict[str, Any] = {"source_id": source_id}
            try:
                source = project.source(source_id)
            except KeyError:
                entry["error"] = "source no longer exists"
                links.append(entry)
                continue
            entry["label"] = source.label or source.id
            crossings, warnings = path_barriers(
                project, source, poi, apply_obliquity=project.apply_obliquity
            )
            entry["warnings"] = warnings
            entry["barriers"] = [
                {
                    "label": c.label,
                    "material": c.material,
                    "thickness_mm": round(c.thickness_mm, 2),
                    "effective_thickness_mm": round(c.effective_thickness_mm, 2),
                    "angle_deg": round(c.angle_deg, 1),
                    "oblique": c.is_oblique,
                    "wall_id": c.wall_id,
                    "floor_name": c.floor_name,
                    "drawn": c.wall_id is not None,
                }
                for c in crossings
            ]
            links.append(entry)
        report.append({"poi_id": poi.id, "label": poi.label or poi.id, "links": links})
    return report


def results_to_rows(results: list[PointResult], materials: list[str]) -> list[dict[str, Any]]:
    """Flatten results into CSV-ready rows, one per source contribution.

    A summary row per point carries the totals and required thickness; the
    contribution rows above it show how the total was reached, so the export
    is an audit trail rather than a bare answer.
    """
    rows: list[dict[str, Any]] = []
    for res in results:
        for contribution in res.contributions:
            rows.append(
                {
                    "point": res.label,
                    "floor": res.floor_name,
                    "row_type": "source",
                    "source": contribution.label,
                    "method": contribution.method,
                    "distance_m": round(contribution.distance_m, 3),
                    "geometric_distance_m": (
                        "" if contribution.geometric_distance_m is None
                        else round(contribution.geometric_distance_m, 3)
                    ),
                    "quantity": contribution.quantity,
                    "value": round(contribution.value, 4),
                    "occupancy": res.occupancy,
                    "area_class": res.area_class,
                    "unshielded_value": round(contribution.unshielded_value, 4),
                    "path_transmission": round(contribution.path_transmission, 5),
                    "path_lead_equivalent_mm": round(contribution.path_equivalent_mm, 3),
                    "isotopes": " + ".join(
                        f"{c['label']} {c['unshielded_uSv']:.3g}" for c in contribution.components
                    ) if len(contribution.components) > 1 else "",
                    "barriers": " + ".join(
                        f"{b['label']} {b['effective_thickness_mm']:g} mm {b['material']}"
                        for b in contribution.barriers
                    ),
                }
            )
        for method in res.methods:
            row = {
                "point": res.label,
                "floor": res.floor_name,
                "row_type": "total",
                "source": "(all sources)",
                "method": method.method,
                "distance_m": "",
                "quantity": method.quantity,
                "value": round(method.total, 4),
                "unshielded_value": round(method.unshielded_total, 4),
                "occupancy": res.occupancy,
                "area_class": res.area_class,
                "required_transmission": (
                    "" if method.required_transmission == float("inf")
                    else round(method.required_transmission, 5)
                ),
                "pct_of_goal": (
                    "" if method.required_transmission == float("inf")
                    else round(100.0 / method.required_transmission, 2)
                ),
            }
            for material in materials:
                row[f"{material}_mm"] = round(method.thickness_mm.get(material, 0.0), 3)
            rows.append(row)
        if res.warnings or res.errors:
            rows.append(
                {
                    "point": res.label,
                    "floor": res.floor_name,
                    "row_type": "notes",
                    "source": "",
                    "method": "",
                    "quantity": "",
                    "value": "",
                    "notes": " | ".join(res.errors + res.warnings),
                }
            )
    return rows
