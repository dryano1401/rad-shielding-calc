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

The kappa values and the body factor are shipped in
``physics/data/ncrp147_ct_scatter.csv`` and are selected by body region.  They
can still be overridden per scanner.  Isodose maps remain vendor-specific and
must always be supplied by the caller.  Either way the transmission and
thickness path is identical to every other secondary barrier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..archer import ArcherParams, thickness as archer_thickness
from ..data_loader import load_table
from ..limits import DesignGoal
from . import tables

CTMethod = Literal["dlp", "isodose", "chart"]


class MissingScatterDataError(ValueError):
    """Raised when a CT calculation is attempted without usable scatter data."""


def scatter_defaults(body_region: str) -> tuple[float, float, str]:
    """Return ``(kappa_per_cm, region_factor, source)`` for a body region.

    The body figure carries NCRP 147's additional factor of 1.2; the head
    figure does not.  Keeping the factor separate from kappa means the audit
    trail shows both, rather than a single pre-multiplied constant a reviewer
    would have to reverse engineer.
    """
    region = body_region.strip().lower()
    for row in load_table("ncrp147_ct_scatter"):
        if str(row["body_region"]).lower() == region:
            return (
                float(row["kappa_per_cm"]),
                float(row["region_factor"]),
                str(row["source"]),
            )
    available = sorted(str(r["body_region"]) for r in load_table("ncrp147_ct_scatter"))
    raise MissingScatterDataError(
        f"unknown CT body region {body_region!r}; known: {available}"
    )


@dataclass(frozen=True)
class CTScatterModel:
    """Caller-supplied CT scatter characterisation.

    Attributes:
        method: ``"dlp"`` or ``"isodose"``.
        body_region: ``"head"`` or ``"body"``.  Selects the shipped kappa and
            the region factor when they are not given explicitly.
        kappa_per_cm: Scatter fraction per unit DLP, in cm^-1.  Defaults to the
            shipped value for ``body_region``: 9e-5 head, 3e-4 body.
        region_factor: Additional multiplier on the DLP form.  Defaults to 1.2
            for body and 1.0 for head, per NCRP 147.
        isodose_kerma_mGy_at_1m: For the isodose method, scattered air kerma at
            1 m for one reference procedure, in mGy.  Always caller-supplied,
            since isodose maps are scanner-specific.
        source: Citation, filled from the shipped table when defaults are used.
    """

    method: CTMethod
    body_region: str = "body"
    kappa_per_cm: float | None = None
    region_factor: float | None = None
    isodose_kerma_mGy_at_1m: float | None = None
    source: str = ""

    def __post_init__(self) -> None:
        if self.method == "dlp":
            default_kappa, default_factor, default_source = scatter_defaults(self.body_region)
            if self.kappa_per_cm is None:
                object.__setattr__(self, "kappa_per_cm", default_kappa)
            if self.region_factor is None:
                object.__setattr__(self, "region_factor", default_factor)
            if not self.source:
                object.__setattr__(self, "source", default_source)
            if self.kappa_per_cm <= 0:
                raise ValueError(f"kappa must be positive, got {self.kappa_per_cm}")
            if self.region_factor <= 0:
                raise ValueError(f"region factor must be positive, got {self.region_factor}")
        elif self.method == "chart":
            if not self.source:
                raise ValueError(
                    "CTScatterModel.source is required for the chart method so the audit "
                    "trail can cite which manufacturer document the chart came from"
                )
        else:
            if self.isodose_kerma_mGy_at_1m is None:
                raise MissingScatterDataError(
                    "the isodose method requires isodose_kerma_mGy_at_1m, taken from the "
                    "manufacturer's scatter isodose map for this scanner."
                )
            if not self.source:
                raise ValueError(
                    "CTScatterModel.source is required for the isodose method so the audit "
                    "trail can cite which scanner document the value came from"
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
        # K_sec at 1 m = kappa * region factor * DLP.  The factor is 1.2 for
        # body and 1.0 for head, so the head form reduces to kappa * DLP.
        kerma_at_1m = scatter.kappa_per_cm * scatter.region_factor * total_dlp
        terms = {
            "kappa (1/cm)": scatter.kappa_per_cm,
            f"{scatter.body_region} region factor": scatter.region_factor,
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


def evaluate_from_chart(
    inputs: CTBarrierInputs,
    goal: DesignGoal,
    kerma_per_procedure_mGy: float,
    chart_notes: tuple[str, ...] = (),
) -> CTBarrierResult:
    """Barrier requirement from a scatter chart already read at the point.

    :mod:`radshield.physics.isodose` handles the direction lookup and the
    inverse-square correction, because those depend on where the point sits
    relative to the isocentre.  What arrives here is the per-procedure air
    kerma *at the point*, so no further distance correction is applied.
    """
    if goal.quantity != "air_kerma":
        raise ValueError(f"NCRP 147 requires an air-kerma design goal, got {goal.quantity!r}")
    if kerma_per_procedure_mGy < 0:
        raise ValueError("chart kerma cannot be negative")

    kerma = kerma_per_procedure_mGy * inputs.procedures_per_week
    terms = {
        "chart kerma at the point (mGy per procedure)": kerma_per_procedure_mGy,
        "procedures per week": inputs.procedures_per_week,
        "distance d (m)": inputs.distance_m,
        "occupancy T": inputs.occupancy,
        "design goal P (mGy/week)": goal.value,
    }
    b = float("inf") if kerma <= 0 else goal.value / (inputs.occupancy * kerma)

    return CTBarrierResult(
        unshielded_weekly_kerma_mGy=kerma,
        required_transmission=b,
        inputs=inputs,
        goal=goal,
        terms=terms,
        notes=(
            f"scatter model: manufacturer chart, source: {inputs.scatter.source}",
            f"secondary transmission taken at {inputs.kvp:g} kVp (Table C.1)",
        )
        + chart_notes,
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
