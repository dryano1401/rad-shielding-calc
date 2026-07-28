"""NCRP 147 CT secondary barrier calculations.

CT differs from radiographic and fluoroscopic rooms: there is no primary
barrier to consider (the beam is intercepted by the gantry), and the scattered
radiation is characterised per unit dose-length product rather than per
mA-min.  NCRP 147 offers two routes:

``dlp``
    ``K_sec = kappa * DLP_total / d^2``, where ``kappa`` is a scatter fraction
    per unit DLP with separate head and body values.

``isodose``
    Manufacturer-supplied isodose contours, normalised to a reference
    procedure and scaled by the weekly procedure count.

The kappa values and isodose maps were **not** part of the extracted table
set, and they are scanner- and vendor-specific in the isodose case.  Rather
than ship guessed constants into a calculation that ends up in a physics
report, both routes require the caller to supply the scatter data explicitly.
Once supplied, the transmission and thickness path is identical to every other
secondary barrier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..archer import ArcherParams, thickness as archer_thickness
from ..limits import DesignGoal
from . import tables

CTMethod = Literal["dlp", "isodose"]


class MissingScatterDataError(ValueError):
    """Raised when a CT calculation is attempted without caller-supplied scatter data."""


@dataclass(frozen=True)
class CTScatterModel:
    """Caller-supplied CT scatter characterisation.

    Attributes:
        method: ``"dlp"`` or ``"isodose"``.
        kappa_mGy_per_mGy_cm: For the DLP method, scattered air kerma at 1 m
            per unit DLP, in mGy per (mGy cm).  Head and body differ; supply
            the value matching ``body_region``.
        isodose_kerma_mGy_at_1m: For the isodose method, scattered air kerma at
            1 m for one reference procedure, in mGy.
        body_region: ``"head"`` or ``"body"``, recorded for the audit trail.
        source: Citation for the value, e.g. the vendor document or the NCRP
            147 section it came from.
    """

    method: CTMethod
    kappa_mGy_per_mGy_cm: float | None = None
    isodose_kerma_mGy_at_1m: float | None = None
    body_region: str = "body"
    source: str = ""

    def __post_init__(self) -> None:
        if self.method == "dlp" and self.kappa_mGy_per_mGy_cm is None:
            raise MissingScatterDataError(
                "the DLP method requires kappa_mGy_per_mGy_cm. NCRP 147 Section 5 tabulates "
                "separate head and body values; they were not part of the extracted table set, "
                "so supply the value from the report or from vendor data."
            )
        if self.method == "isodose" and self.isodose_kerma_mGy_at_1m is None:
            raise MissingScatterDataError(
                "the isodose method requires isodose_kerma_mGy_at_1m, taken from the "
                "manufacturer's scatter isodose map for this scanner."
            )
        if not self.source:
            raise ValueError(
                "CTScatterModel.source is required so the audit trail can cite where the "
                "scatter data came from"
            )


@dataclass(frozen=True)
class CTBarrierInputs:
    """Inputs for a CT secondary barrier calculation.

    Attributes:
        scatter: Caller-supplied scatter characterisation.
        distance_m: Source-to-point-of-protection distance.
        occupancy: Occupancy factor T.
        procedures_per_week: Weekly procedure count.
        dlp_per_procedure_mGy_cm: Mean DLP per procedure; DLP method only.
        kvp: Tube potential, used to select the secondary Archer fit.  CT
            operates at 120-140 kVp; Table C.1 provides 125 and 150 kVp rows.
        label: Free text for audit output.
    """

    scatter: CTScatterModel
    distance_m: float
    occupancy: float
    procedures_per_week: float
    dlp_per_procedure_mGy_cm: float | None = None
    kvp: float = 125.0
    label: str = ""

    def __post_init__(self) -> None:
        if self.distance_m <= 0:
            raise ValueError(f"distance must be positive, got {self.distance_m}")
        if not 0 < self.occupancy <= 1:
            raise ValueError(f"occupancy must be in (0, 1], got {self.occupancy}")
        if self.procedures_per_week < 0:
            raise ValueError("procedures per week must be non-negative")
        if self.scatter.method == "dlp" and self.dlp_per_procedure_mGy_cm is None:
            raise MissingScatterDataError(
                "dlp_per_procedure_mGy_cm is required when using the DLP method"
            )


@dataclass(frozen=True)
class CTBarrierResult:
    """Result of a CT secondary barrier calculation."""

    unshielded_weekly_kerma_mGy: float
    required_transmission: float
    inputs: CTBarrierInputs
    goal: DesignGoal
    terms: dict[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def shielding_required(self) -> bool:
        """True when the unshielded kerma exceeds the occupancy-scaled goal."""
        return self.required_transmission < 1.0


def evaluate(inputs: CTBarrierInputs, goal: DesignGoal) -> CTBarrierResult:
    """Compute unshielded weekly kerma and required transmission for a CT room."""
    if goal.quantity != "air_kerma":
        raise ValueError(f"NCRP 147 requires an air-kerma design goal, got {goal.quantity!r}")

    scatter = inputs.scatter
    if scatter.method == "dlp":
        total_dlp = inputs.dlp_per_procedure_mGy_cm * inputs.procedures_per_week
        kerma_at_1m = scatter.kappa_mGy_per_mGy_cm * total_dlp
        terms = {
            "kappa (mGy per mGy cm at 1 m)": scatter.kappa_mGy_per_mGy_cm,
            "DLP per procedure (mGy cm)": inputs.dlp_per_procedure_mGy_cm,
            "procedures per week": inputs.procedures_per_week,
            "total weekly DLP (mGy cm)": total_dlp,
        }
    else:
        kerma_at_1m = scatter.isodose_kerma_mGy_at_1m * inputs.procedures_per_week
        terms = {
            "isodose kerma per procedure at 1 m (mGy)": scatter.isodose_kerma_mGy_at_1m,
            "procedures per week": inputs.procedures_per_week,
        }

    kerma = kerma_at_1m / (inputs.distance_m**2)
    terms["distance d (m)"] = inputs.distance_m
    terms["occupancy T"] = inputs.occupancy
    terms["design goal P (mGy/week)"] = goal.value

    b = float("inf") if kerma <= 0 else goal.value / (inputs.occupancy * kerma)

    return CTBarrierResult(
        unshielded_weekly_kerma_mGy=kerma,
        required_transmission=b,
        inputs=inputs,
        goal=goal,
        terms=terms,
        notes=(
            f"scatter model: {scatter.method}, {scatter.body_region}, source: {scatter.source}",
            f"secondary transmission taken at {inputs.kvp:g} kVp (Table C.1)",
        ),
    )


def barrier_params(inputs: CTBarrierInputs, material: str) -> ArcherParams:
    """Return the secondary Archer fit for the CT tube potential."""
    return tables.secondary_archer(str(inputs.kvp), material, by_kvp=True)


def required_thickness(
    result: CTBarrierResult, material: str, *, existing_thickness_mm: float = 0.0
) -> float:
    """Thickness in mm required for a CT secondary barrier."""
    if not result.shielding_required:
        return 0.0
    params = barrier_params(result.inputs, material)
    return max(archer_thickness(params, result.required_transmission) - existing_thickness_mm, 0.0)
