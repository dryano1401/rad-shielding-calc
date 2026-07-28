"""NCRP 147 primary and secondary barrier calculations.

Primary barrier (NCRP 147 Eq. 4.2):

    B_p = P * d_P^2 / (K1^P * U * N * T)

Secondary barrier (Eq. 4.5):

    B_sec = P * d_sec^2 / (K1^sec * N * T)

where K1 is the unshielded air kerma per patient at 1 m (mGy), N is patients
per week, U the use factor, T the occupancy factor and P the weekly design
goal in mGy.  Thickness follows from the Archer inversion using the
workload-distribution-weighted fits (Tables B.1 and C.1), which is the
preferred route because those fits are already integrated over the clinical
spectrum.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..archer import ArcherParams, thickness as archer_thickness
from ..limits import DesignGoal
from . import tables


@dataclass(frozen=True)
class XrayBarrierInputs:
    """Inputs for one NCRP 147 barrier calculation.

    Attributes:
        workload: Workload distribution name from Table 4.2.
        distance_m: d, source-to-point-of-protection distance in metres.  For
            NCRP 147 the point of protection is 0.3 m beyond the distal
            barrier surface; the geometry layer applies that offset, not this
            function.
        occupancy: T, occupancy factor for the protected area.
        patients_per_week: N.  Defaults to the Table 4.2 surveyed value for
            the workload distribution when omitted.
        use_factor: U, primary beam use factor.  Primary barriers only.
        barrier_type: ``"primary"`` or ``"secondary"``.
        scatter_geometry: For secondary barriers, ``"side"`` or
            ``"forward_back"``.
        label: Free text for audit output.
    """

    workload: str
    distance_m: float
    occupancy: float
    patients_per_week: float | None = None
    use_factor: float = 1.0
    barrier_type: str = "secondary"
    scatter_geometry: str = "side"
    label: str = ""

    def __post_init__(self) -> None:
        if self.distance_m <= 0:
            raise ValueError(f"distance must be positive, got {self.distance_m}")
        if not 0 < self.occupancy <= 1:
            raise ValueError(f"occupancy must be in (0, 1], got {self.occupancy}")
        if self.barrier_type not in ("primary", "secondary"):
            raise ValueError(f"barrier_type must be 'primary' or 'secondary', got {self.barrier_type!r}")
        if not 0 < self.use_factor <= 1:
            raise ValueError(f"use factor must be in (0, 1], got {self.use_factor}")


@dataclass(frozen=True)
class XrayBarrierResult:
    """Result of one NCRP 147 barrier calculation, with intermediates."""

    unshielded_weekly_kerma_mGy: float
    required_transmission: float
    inputs: XrayBarrierInputs
    goal: DesignGoal
    terms: dict[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def shielding_required(self) -> bool:
        """True when the unshielded kerma exceeds the occupancy-scaled goal."""
        return self.required_transmission < 1.0

    def audit_lines(self) -> list[str]:
        """Return human-readable lines describing the calculation."""
        lines = [f"Source: {self.inputs.label or self.inputs.workload} ({self.inputs.barrier_type})"]
        lines += [f"  {name} = {value:g}" for name, value in self.terms.items()]
        lines.append(f"  => unshielded weekly air kerma = {self.unshielded_weekly_kerma_mGy:.4g} mGy")
        lines.append(f"  => required transmission B = {self.required_transmission:.4g}")
        lines += [f"  note: {n}" for n in self.notes]
        return lines


def unshielded_kerma(inputs: XrayBarrierInputs) -> tuple[float, dict[str, float], list[str]]:
    """Return unshielded weekly air kerma (mGy) at the point, plus intermediates."""
    totals = tables.workload_totals(inputs.workload)
    n = inputs.patients_per_week if inputs.patients_per_week is not None else totals.patients_per_week
    notes: list[str] = []
    if inputs.patients_per_week is None:
        notes.append(
            f"N defaulted to the Table 4.2 surveyed value ({totals.patients_per_week:g} patients/week)"
        )

    if inputs.barrier_type == "primary":
        k1 = tables.primary_air_kerma(inputs.workload)
        u = inputs.use_factor
        terms = {
            "K1^P (mGy/patient at 1 m)": k1,
            "use factor U": u,
            "patients per week N": n,
            "distance d (m)": inputs.distance_m,
        }
        kerma = k1 * u * n / (inputs.distance_m**2)
    else:
        k1 = tables.secondary_air_kerma(inputs.workload, geometry=inputs.scatter_geometry)
        terms = {
            "K1^sec (mGy/patient at 1 m)": k1,
            "patients per week N": n,
            "distance d (m)": inputs.distance_m,
        }
        notes.append(f"secondary kerma geometry: {inputs.scatter_geometry}")
        kerma = k1 * n / (inputs.distance_m**2)

    return kerma, terms, notes


def evaluate(inputs: XrayBarrierInputs, goal: DesignGoal) -> XrayBarrierResult:
    """Compute unshielded kerma and required transmission for one barrier."""
    if goal.quantity != "air_kerma":
        raise ValueError(f"NCRP 147 requires an air-kerma design goal, got {goal.quantity!r}")

    kerma, terms, notes = unshielded_kerma(inputs)
    b = float("inf") if kerma <= 0 else goal.value / (inputs.occupancy * kerma)
    terms["occupancy T"] = inputs.occupancy
    terms["design goal P (mGy/week)"] = goal.value

    return XrayBarrierResult(
        unshielded_weekly_kerma_mGy=kerma,
        required_transmission=b,
        inputs=inputs,
        goal=goal,
        terms=terms,
        notes=tuple(notes),
    )


def barrier_params(inputs: XrayBarrierInputs, material: str) -> ArcherParams:
    """Return the Archer fit appropriate to this barrier type and workload."""
    if inputs.barrier_type == "primary":
        return tables.primary_archer_by_workload(inputs.workload, material)
    return tables.secondary_archer(inputs.workload, material)


def required_thickness(
    results: list[XrayBarrierResult],
    material: str,
    *,
    existing_thickness_mm: float = 0.0,
) -> float:
    """Thickness in mm needed at a point exposed to several x-ray sources.

    Doses from all incident sources are summed before solving, consistent with
    the TG-108 treatment.  Because each source may carry different Archer
    parameters (different workload distributions), the summed requirement is
    solved per source and the governing (largest) thickness is returned -- the
    conservative reading when a single barrier must satisfy every source.

    Args:
        results: Evaluated barriers, all incident on the same point.
        material: Material to report thickness in.
        existing_thickness_mm: Credit for shielding already in the structure.
    """
    if not results:
        raise ValueError("at least one evaluated barrier is required")

    goals = {r.goal.value for r in results}
    if len(goals) > 1:
        raise ValueError(f"all sources at a point must share one design goal, got {sorted(goals)}")
    occupancies = {r.inputs.occupancy for r in results}
    if len(occupancies) > 1:
        raise ValueError(
            f"all sources at a point must share one occupancy factor, got {sorted(occupancies)}"
        )

    total_kerma = sum(r.unshielded_weekly_kerma_mGy for r in results)
    goal = results[0].goal
    occupancy = results[0].inputs.occupancy
    b_total = float("inf") if total_kerma <= 0 else goal.value / (occupancy * total_kerma)

    if b_total >= 1.0:
        return 0.0

    thicknesses = [
        archer_thickness(barrier_params(r.inputs, material), b_total) for r in results
    ]
    return max(max(thicknesses) - existing_thickness_mm, 0.0)
