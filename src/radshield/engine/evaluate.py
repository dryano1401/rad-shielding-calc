"""Bind the project model to the physics engines and aggregate per point.

Every source linked to a point contributes.  Doses are summed *within a
methodology* before the transmission factor is derived, matching TG-108
Table VII.  TG-108 and NCRP 147 results are not summed with each other: one is
effective dose equivalent and the other air kerma, and their design goals are
different quantities.  Where a point sees both, each is solved separately and
the governing (thicker) requirement is reported alongside both components.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..model.geometry import Crossing, Distance, distance, format_length, path_barriers
from ..model.project import PointOfInterest, Project, SourcePoint
from ..physics import nuclides, tg108
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


@dataclass
class MethodResult:
    """Solved requirement for one methodology at one point."""

    method: str
    quantity: str
    total: float
    required_transmission: float
    thickness_mm: dict[str, float] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)


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


def _tg108_source(source: SourcePoint) -> tg108.PatientSource:
    """Build a physics source from the stored parameters."""
    p = source.params
    return tg108.PatientSource(
        kind=p.get("kind", "uptake"),
        nuclide=p.get("nuclide", "F-18"),
        administered_activity_MBq=float(p.get("administered_activity_MBq", 0.0)),
        patients_per_week=float(p.get("patients_per_week", 0.0)),
        uptake_time_h=float(p.get("uptake_time_h", 0.0)),
        imaging_time_h=float(p.get("imaging_time_h", 0.0)),
        void_factor=float(p.get("void_factor", tg108.DEFAULT_VOID_FACTOR)),
        scanner_attenuation=float(p.get("scanner_attenuation", 1.0)),
        label=source.label or source.id,
    )


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
        dlp_per_procedure_mGy_cm=_optional_float(p.get("dlp_per_procedure_mGy_cm")),
        kvp=float(p.get("kvp", 125)),
        label=source.label or source.id,
    )


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
) -> tuple[MethodResult, list[SourceContribution]]:
    """Attenuate each TG-108 source by its own path, sum, then solve."""
    goal = tg108_goal(poi.area_class)

    nuclide_names = {src.params.get("nuclide", "F-18") for src, _ in pairs}
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
    for src, dist in pairs:
        dose = tg108.weekly_dose(_tg108_source(src), dist.metres)
        crossings, geometry_warnings = path_barriers(
            project, src, poi, apply_obliquity=project.apply_obliquity
        )
        b_path, equivalent_mm, details, barrier_warnings = _path_attenuation(
            crossings, params_for
        )
        attenuated = dose.weekly_dose_uSv * b_path
        total += attenuated

        notes = list(dose.notes) + dist.notes + geometry_warnings + barrier_warnings
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
                unshielded_value=dose.weekly_dose_uSv,
                terms=dose.terms,
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
        ),
        contributions,
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
        ),
        contributions,
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
                apply_ncrp_standoff=(
                    source.method != "tg108" and not poi.offset_applied
                ),
                override_m=poi.distance_overrides.get(source_id),
            )
        except Exception as exc:  # geometry problems are reported, not raised
            result.errors.append(f"{source.label or source.id}: {exc}")
            continue
        result.warnings.extend(dist.warnings)
        group = "tg108" if source.method == "tg108" else "ncrp147"
        by_method[group].append((source, dist))

    for group, pairs in by_method.items():
        if not pairs:
            continue
        try:
            solver = _solve_tg108 if group == "tg108" else _solve_ncrp147
            method_result, contributions = solver(project, poi, pairs, project.materials)
        except Exception as exc:
            result.errors.append(f"{group}: {exc}")
            continue
        result.methods.append(method_result)
        result.contributions.extend(contributions)
        for material, mm in method_result.thickness_mm.items():
            result.governing_thickness_mm[material] = max(
                result.governing_thickness_mm.get(material, 0.0), mm
            )

    if len(result.methods) > 1:
        result.warnings.append(
            "this point sees both TG-108 and NCRP 147 sources; the two are solved separately "
            "because they use different dose quantities, and the thicker requirement governs"
        )

    return result


def evaluate_project(project: Project) -> list[PointResult]:
    """Evaluate every point of interest in the project."""
    return [evaluate_point(project, poi) for poi in project.pois]


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
                    apply_ncrp_standoff=(source.method != "tg108" and not poi.offset_applied),
                )
                used = distance(
                    project,
                    source,
                    poi,
                    apply_ncrp_standoff=(source.method != "tg108" and not poi.offset_applied),
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
                "occupancy": res.occupancy,
                "area_class": res.area_class,
                "required_transmission": (
                    "" if method.required_transmission == float("inf")
                    else round(method.required_transmission, 5)
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
