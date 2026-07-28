"""TG-108 nuclear-medicine barrier calculations.

Implements the uptake-room and imaging-room dose equations from Madsen et al.,
*AAPM Task Group 108: PET and PET/CT Shielding Requirements*, Med Phys 33(1),
2006, and solves them for required barrier thickness.

The dose equations (Eqs. 3 and 9) are:

    uptake:   D_week = G * N_w * A0 * t_U * R(t_U) / d^2
    imaging:  D_week = G * N_w * A0 * v * F_U * t_I * R(t_I) / d^2

where ``G`` is the patient-self-attenuated dose-rate constant (0.092 uSv m^2 /
MBq h for F-18), ``v`` is the voiding factor (0.85), and ``F_U`` is decay over
the uptake period.  The required transmission is then ``B = P / (T * D_week)``
-- note that occupancy scales the *limit*, not the reported dose, per the
footnote to TG-108 Table VII.

Scope note: TG-108 is a PET/PET-CT document.  The same equations are used here
for other isotopes via the nuclide registry, but results for non-511 keV
emitters should be labelled as the TG-108 *method extended to* that isotope,
not as TG-108 itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from . import nuclides
from .archer import thickness as archer_thickness
from .decay import decay_factor, dose_reduction_factor
from .limits import DesignGoal

SourceKind = Literal["uptake", "imaging"]

# TG-108 imaging-room calculation: patients typically void before imaging,
# removing ~15% of administered activity.
DEFAULT_VOID_FACTOR = 0.85


@dataclass(frozen=True)
class PatientSource:
    """A population of radioactive patients acting as a source.

    Attributes:
        kind: ``"uptake"`` (patient waiting post-injection) or ``"imaging"``
            (patient on the scanner, after uptake decay and voiding).
        nuclide: Registered nuclide name.
        administered_activity_MBq: A0, activity administered per patient.
        patients_per_week: N_w.
        uptake_time_h: t_U.  Used as the exposure duration for ``"uptake"``
            sources and as the pre-imaging decay interval for ``"imaging"``.
        imaging_time_h: t_I, exposure duration for ``"imaging"`` sources.
        void_factor: Fraction of activity remaining after voiding; imaging
            only.  1.0 disables the credit.
        scanner_attenuation: Multiplicative credit for gantry self-shielding;
            1.0 (default) takes no credit, which is the conservative choice.
            TG-108 suggests ~0.85 is realistic when credit is taken.
        label: Free text for audit output.
    """

    kind: SourceKind
    nuclide: str
    administered_activity_MBq: float
    patients_per_week: float
    uptake_time_h: float = 0.0
    imaging_time_h: float = 0.0
    void_factor: float = DEFAULT_VOID_FACTOR
    scanner_attenuation: float = 1.0
    label: str = ""

    def __post_init__(self) -> None:
        if self.administered_activity_MBq < 0:
            raise ValueError("administered activity must be non-negative")
        if self.patients_per_week < 0:
            raise ValueError("patients per week must be non-negative")
        if self.kind == "imaging" and self.imaging_time_h <= 0:
            raise ValueError("imaging sources require a positive imaging_time_h")
        if self.kind == "uptake" and self.uptake_time_h <= 0:
            raise ValueError("uptake sources require a positive uptake_time_h")
        if not 0 < self.void_factor <= 1:
            raise ValueError(f"void_factor must be in (0, 1], got {self.void_factor}")
        if not 0 < self.scanner_attenuation <= 1:
            raise ValueError(
                f"scanner_attenuation must be in (0, 1], got {self.scanner_attenuation}"
            )


@dataclass(frozen=True)
class DoseResult:
    """Unshielded weekly dose at a point, with every intermediate value.

    ``terms`` holds the named factors that multiply to give ``weekly_dose_uSv``
    (before the 1/d^2 divisor), so a report can show the full arithmetic rather
    than just the answer.
    """

    weekly_dose_uSv: float
    distance_m: float
    source_label: str
    terms: dict[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def audit_lines(self) -> list[str]:
        """Return human-readable lines describing the calculation."""
        lines = [f"Source: {self.source_label or '(unlabelled)'}"]
        lines += [f"  {name} = {value:g}" for name, value in self.terms.items()]
        lines.append(f"  distance d = {self.distance_m:g} m")
        lines.append(f"  => unshielded weekly dose = {self.weekly_dose_uSv:.4g} uSv")
        lines += [f"  note: {n}" for n in self.notes]
        return lines


def weekly_dose(source: PatientSource, distance_m: float) -> DoseResult:
    """Unshielded weekly dose in uSv at ``distance_m`` from ``source``.

    Implements TG-108 Eq. 3 (uptake) or Eq. 9 (imaging).
    """
    if distance_m <= 0:
        raise ValueError(f"distance must be positive, got {distance_m}")

    nuc = nuclides.get_nuclide(source.nuclide)
    gamma, gamma_provenance = nuclides.patient_dose_rate_constant(source.nuclide)

    terms: dict[str, float] = {
        "dose-rate constant G (uSv m2 / MBq h)": gamma,
        "patients per week N_w": source.patients_per_week,
        "administered activity A0 (MBq)": source.administered_activity_MBq,
    }
    notes = [f"G provenance: {gamma_provenance}", f"half-life = {nuc.half_life_min:g} min"]

    product = gamma * source.patients_per_week * source.administered_activity_MBq

    if source.kind == "uptake":
        duration = source.uptake_time_h
        r = dose_reduction_factor(nuc.half_life_h, duration)
        terms["uptake time t_U (h)"] = duration
        terms["decay reduction R(t_U)"] = r
        product *= duration * r
    else:
        f_u = decay_factor(nuc.half_life_h, source.uptake_time_h)
        r = dose_reduction_factor(nuc.half_life_h, source.imaging_time_h)
        terms["void factor"] = source.void_factor
        terms["uptake decay F_U"] = f_u
        terms["imaging time t_I (h)"] = source.imaging_time_h
        terms["decay reduction R(t_I)"] = r
        product *= source.void_factor * f_u * source.imaging_time_h * r

    if source.scanner_attenuation != 1.0:
        terms["scanner self-shielding"] = source.scanner_attenuation
        product *= source.scanner_attenuation
    else:
        notes.append("no scanner self-shielding credit taken (conservative)")

    return DoseResult(
        weekly_dose_uSv=product / (distance_m**2),
        distance_m=distance_m,
        source_label=source.label or f"{source.nuclide} {source.kind}",
        terms=terms,
        notes=tuple(notes),
    )


def required_transmission(total_weekly_dose_uSv: float, goal: DesignGoal, occupancy: float) -> float:
    """Return the transmission factor B needed to meet ``goal``.

    ``B = P / (T * D)``.  Per TG-108 Table VII, occupancy scales the design
    goal rather than the reported dose.  A value >= 1 means no barrier is
    required; it is returned unclamped so the caller can report "none needed".
    """
    if not 0 < occupancy <= 1:
        raise ValueError(f"occupancy factor must be in (0, 1], got {occupancy}")
    if goal.quantity != "effective_dose_equivalent":
        raise ValueError(
            f"TG-108 requires an effective-dose-equivalent goal, got {goal.quantity!r}"
        )
    if total_weekly_dose_uSv <= 0:
        return float("inf")
    return goal.value / (occupancy * total_weekly_dose_uSv)


@dataclass(frozen=True)
class BarrierResult:
    """Required barrier thickness for one point of interest."""

    total_weekly_dose_uSv: float
    required_transmission: float
    occupancy: float
    goal: DesignGoal
    thickness_by_material: dict[str, float]
    thickness_unit: dict[str, str]
    existing_credit: dict[str, float] = field(default_factory=dict)
    per_source: tuple[DoseResult, ...] = ()

    @property
    def shielding_required(self) -> bool:
        """True when the unshielded dose exceeds the occupancy-scaled goal."""
        return self.required_transmission < 1.0


def solve_barrier(
    sources: list[tuple[PatientSource, float]],
    goal: DesignGoal,
    occupancy: float,
    materials: list[str],
    *,
    nuclide_for_attenuation: str | None = None,
    existing_barriers: dict[str, float] | None = None,
) -> BarrierResult:
    """Solve for barrier thickness at a point exposed to several sources.

    Every source incident on the point contributes; the doses are summed and a
    single transmission factor is derived from the total, per TG-108 Table VII
    (where uptake-room and tomograph-room doses add before B is computed).

    Args:
        sources: ``(source, distance_m)`` pairs, all incident on this point.
        goal: Weekly design goal.
        occupancy: Occupancy factor T for the point.
        materials: Material names to report thickness for.
        nuclide_for_attenuation: Nuclide whose transmission data to use.
            Defaults to the first source's nuclide; must be given explicitly
            when sources mix nuclides with different photon energies.
        existing_barriers: Thickness already present in the structure, keyed by
            material and expressed in that material's tabulated unit.  Credited
            against the requirement.

    Returns:
        A :class:`BarrierResult` carrying the full audit trail.
    """
    if not sources:
        raise ValueError("at least one source is required")

    per_source = tuple(weekly_dose(src, dist) for src, dist in sources)
    total = sum(r.weekly_dose_uSv for r in per_source)

    nuclide_names = {src.nuclide for src, _ in sources}
    if nuclide_for_attenuation is None:
        if len(nuclide_names) > 1:
            raise ValueError(
                f"sources mix nuclides {sorted(nuclide_names)}; pass nuclide_for_attenuation "
                "explicitly to choose the transmission data"
            )
        nuclide_for_attenuation = next(iter(nuclide_names))

    b = required_transmission(total, goal, occupancy)
    existing = existing_barriers or {}

    thicknesses: dict[str, float] = {}
    units: dict[str, str] = {}
    credits: dict[str, float] = {}
    for material in materials:
        params = nuclides.get_archer(nuclide_for_attenuation, material)
        units[material] = params.unit
        gross = archer_thickness(params, b) if b < 1.0 else 0.0
        credit = float(existing.get(material, 0.0))
        credits[material] = credit
        thicknesses[material] = max(gross - credit, 0.0)

    return BarrierResult(
        total_weekly_dose_uSv=total,
        required_transmission=b,
        occupancy=occupancy,
        goal=goal,
        thickness_by_material=thicknesses,
        thickness_unit=units,
        existing_credit=credits,
        per_source=per_source,
    )


def equivalent_thickness(
    from_material: str,
    from_thickness: float,
    to_material: str,
    nuclide: str = "F-18",
) -> float:
    """Convert a thickness of one material to the equivalent thickness of another.

    Equivalence is defined by equal transmission at the nuclide's photon
    energy.  This is what lets an existing 10 cm concrete slab be credited as
    ~0.65 cm of lead, as in TG-108 Example 4.
    """
    from .archer import transmission as archer_transmission

    src = nuclides.get_archer(nuclide, from_material)
    dst = nuclides.get_archer(nuclide, to_material)
    b = archer_transmission(src, from_thickness)
    return archer_thickness(dst, b)
