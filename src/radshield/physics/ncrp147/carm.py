"""C-arm fluoroscopy secondary barriers, worked from KAP.

A C-arm's image receptor is the primary-beam stop, so structural barriers see
scatter and tube-housing leakage only and there is no primary term to
calculate (NCRP 147 Appendix C, opening paragraph, which names image
intensifier operation as exactly this case).  What separates this from the
room workloads in :mod:`radshield.physics.ncrp147.barriers` is the workload
input: a C-arm's output is metered as air-kerma--area product, not as
patients through a Table 4.2 distribution, so the barrier is worked from KAP
directly.

Scatter
-------
NCRP 147 Equation C.2 gives the unshielded scattered air kerma at distance
``dS`` as

    K_S = K_P(1 m) * a1 * 1e-6 / dS^2 * F / dF^2

with ``a1`` the scatter fraction per cm2 of primary beam area at 1 m.  Since
the air-kerma--area product is invariant with distance,

    KAP = K_P(1 m) * F / dF^2

so substituting leaves

    K_S = KAP * a1 * 1e-6 / dS^2

and the field area cancels out of the scatter term entirely.  It reappears
only in the leakage estimate, which needs K_P(1 m) on its own.

``a1`` itself is the polynomial printed inside Figure C.1 -- see
:func:`scatter_fraction`.  It is a function of both kVp *and* scattering
angle, and the angular swing is the larger of the two: at 100 kVp it runs
from 4.5 at 69 degrees to 7.0 at 140 degrees.

Leakage
-------
Equations C.6 to C.8.  Leakage is pinned to the regulatory cap of 100 mR/h
(0.876 mGy/h) at 1 m at the leakage technique factors, and scaled to clinical
operation by kVp^2, by the housing transmission, and by the workload:

    K_L(1 m) = (K_lim / 60) * kVp^2 * B_housing(kVp) * W
               / (kVpmax^2 * B_housing(kVpmax) * Imax)

The workload W in mA min is not metered by a C-arm either, but it follows
from KAP through Figure B.1's air kerma per unit workload:

    W = K_P(1 m) / K_W(kVp)

which closes the loop and leaves the whole barrier a function of KAP, kVp and
geometry.  The result is a leakage-to-primary ratio of 4.5e-4 at 150 kVp
falling to 2.1e-4 at 100 kVp and effectively zero below that, matching the
report's remark that below 100 kVp "the leakage radiation contribution
through the tube housing is negligible".

Why the two components are attenuated separately
------------------------------------------------
Table C.1's fits are for the *combined* secondary transmission, and so bake
in a particular scatter-to-leakage mix -- the one produced by 90 degree
scatter at the Table 4.7 beam sizes with 150 kVp / 3.3 mA leakage.  A C-arm's
mix is set by its own KAP, housing and geometry instead, so that fit does not
describe it.

NCRP 147 Section C.4 is written for precisely this case: sum the separately
attenuated contributions and iterate for the thickness that brings the total
to P/T.  That is what :func:`thickness_for_transmission` does, using the
report's own component transmissions -- Equation C.3 attenuates scatter with
the primary fit ("assumed identical to that of the primary beam"), and
Equation C.8 attenuates leakage as ``exp(-ln2 * x / x_half)`` with ``x_half``
the half-value layer at high attenuation, which for the Archer form is
``ln2 / alpha``.  Note that this leakage transmission is deliberately the
bare exponential without the Archer asymptote's prefactor, so it sits above
the primary curve at every thickness; that is the report's model, not an
omission.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ...physics.archer import ArcherParams, transmission
from ..limits import DesignGoal
from . import tables

# Figure C.1, W anode / Al filter.  Plotted over these ranges; the fit is not
# extrapolated beyond them.
SCATTER_KVP_RANGE = (50.0, 150.0)
SCATTER_ANGLE_RANGE = (20.0, 140.0)

# Figure B.1, W anode / Al filtered radiography tube.
WORKLOAD_KVP_RANGE = (50.0, 150.0)

# Equation C.6 leakage technique factors and the regulatory cap they apply at:
# 100 mR/h at 1 m, which is 0.876 mGy/h.
LEAKAGE_LIMIT_MGY_PER_H = 0.876
LEAKAGE_KVP_MAX = 150.0
LEAKAGE_MA_MAX = 3.3
# The lead-lined housing thickness the report derives for those factors.
HOUSING_LEAD_MM = 2.32

# NCRP 147 tabulates the unshielded secondary air kerma at both 90 degrees
# ("side-scatter") and 135 degrees ("forward- and backscatter") in Table 4.7.
# A C-arm gantry rotates, so the angle to any given barrier is not fixed;
# 135 degrees is the more conservative of the two the report tabulates.
DEFAULT_SCATTER_ANGLE_DEG = 135.0


class ScatterFractionError(ValueError):
    """Raised when the scatter fraction fit is asked for a point it does not cover."""


class WorkloadFitError(ValueError):
    """Raised when the air-kerma-per-workload fit is asked for a kVp it does not cover."""


def scatter_fraction(kvp: float, angle_deg: float) -> float:
    """Scatter fraction per cm2 of primary beam area at 1 m.

    The polynomial printed inside NCRP 147 Figure C.1, for tungsten anode /
    aluminium filtered beams:

        a1 = 1.6e-2 (kVp - 125) + 8.43 - 1.11e-1 t + 9.83e-4 t^2 - 1.74e-6 t^3

    with ``t`` the scattering angle in degrees.  The graph plots ``a1``, which
    the caption instructs be multiplied by 1e-6; that factor is applied here,
    so the value returned is the one Equation C.2 consumes directly.

    Refused rather than extrapolated outside the plotted ranges, as the Archer
    fits are.
    """
    low, high = SCATTER_KVP_RANGE
    if not low <= kvp <= high:
        raise ScatterFractionError(
            f"no Figure C.1 scatter fraction for {kvp:g} kVp; the tungsten-anode curves "
            f"cover {low:g}-{high:g} kVp. Enter one for this source rather than extrapolating."
        )
    a_low, a_high = SCATTER_ANGLE_RANGE
    if not a_low <= angle_deg <= a_high:
        raise ScatterFractionError(
            f"no Figure C.1 scatter fraction at {angle_deg:g} degrees; the curves are "
            f"plotted over {a_low:g}-{a_high:g} degrees."
        )
    t = angle_deg
    a1 = (
        1.6e-2 * (kvp - 125.0)
        + 8.43
        - 1.11e-1 * t
        + 9.83e-4 * t**2
        - 1.74e-6 * t**3
    )
    return a1 * 1.0e-6


def air_kerma_per_workload(kvp: float) -> float:
    """Primary beam air kerma at 1 m per unit workload, mGy per mA min.

    The polynomial printed inside NCRP 147 Figure B.1 for the tungsten anode /
    aluminium filtered radiography tube of Archer et al. (1994).
    """
    low, high = WORKLOAD_KVP_RANGE
    if not low <= kvp <= high:
        raise WorkloadFitError(
            f"no Figure B.1 air kerma per workload for {kvp:g} kVp; "
            f"the fit covers {low:g}-{high:g} kVp."
        )
    return 1.222 - 5.664e-2 * kvp + 1.227e-3 * kvp**2 - 3.136e-6 * kvp**3


def housing_transmission(kvp: float) -> float:
    """B_housing(kVp): primary transmission through the 2.32 mm lead housing."""
    return transmission(tables.primary_archer_by_kvp(kvp, "lead"), HOUSING_LEAD_MM)


def leakage_fraction_of_primary(kvp: float) -> float:
    """K_L(1 m) / K_P(1 m) implied by Equations C.6 to C.8.

    The kVp^2 of Equation C.7 and the kVp^2-like growth of Figure B.1 largely
    cancel, leaving the housing transmission as the dominant term -- which is
    why this collapses by orders of magnitude below 100 kVp.
    """
    return (
        (LEAKAGE_LIMIT_MGY_PER_H / 60.0)
        * kvp**2
        * housing_transmission(kvp)
        / (
            LEAKAGE_KVP_MAX**2
            * housing_transmission(LEAKAGE_KVP_MAX)
            * LEAKAGE_MA_MAX
            * air_kerma_per_workload(kvp)
        )
    )


@dataclass(frozen=True)
class CArmInputs:
    """Inputs for one C-arm secondary barrier.

    Attributes:
        kvp: Maximum or design operating potential.
        kap_week_mGy_cm2: Weekly air-kerma--area product, mGy cm2.
        scatter_distance_m: dS, patient/scatter centre to the point of
            protection.  The placed source point is the scatter centre, as a
            CT source's placed point is its isocentre.
        leakage_distance_m: dL, focal spot to the point of protection.
        occupancy: T for the protected area.
        scatter_angle_deg: Scattering angle from the primary beam axis to the
            protected area.
        scatter_fraction: Overrides Figure C.1 when the equipment's own
            scatter data is known.  Per cm2 at 1 m, the units of Equation C.2.
        leakage_fraction: Overrides the Equation C.6-C.8 leakage model with a
            flat fraction of the equivalent primary air kerma at 1 m, for
            vendor figures quoted that way.  Ignored when
            ``leakage_at_1m_uGy_week`` is given.
        leakage_at_1m_uGy_week: Measured weekly leakage air kerma at 1 m,
            which overrides both of the above.
        field_area_cm2: Representative field area, for the leakage estimate
            only -- it cancels out of the scatter term.
        field_distance_m: Distance the field area is quoted at, usually SID.
        scatter_multiplier: Directional or project-specific adjustment applied
            to the scatter term alone.
    """

    kvp: float
    kap_week_mGy_cm2: float
    scatter_distance_m: float
    leakage_distance_m: float
    occupancy: float
    scatter_angle_deg: float = DEFAULT_SCATTER_ANGLE_DEG
    scatter_fraction: float | None = None
    leakage_fraction: float | None = None
    leakage_at_1m_uGy_week: float | None = None
    field_area_cm2: float = 900.0
    field_distance_m: float = 1.0
    scatter_multiplier: float = 1.0
    label: str = ""

    def __post_init__(self) -> None:
        if self.scatter_distance_m <= 0 or self.leakage_distance_m <= 0:
            raise ValueError("scatter and leakage distances must be positive")
        if not 0 < self.occupancy <= 1:
            raise ValueError(f"occupancy must be in (0, 1], got {self.occupancy}")
        if self.kap_week_mGy_cm2 < 0:
            raise ValueError("weekly KAP cannot be negative")
        if self.field_area_cm2 <= 0 or self.field_distance_m <= 0:
            raise ValueError("field area and field distance must be positive")
        if self.leakage_fraction is not None and self.leakage_fraction < 0:
            raise ValueError("leakage fraction cannot be negative")
        if self.leakage_at_1m_uGy_week is not None and self.leakage_at_1m_uGy_week < 0:
            raise ValueError("measured leakage at 1 m cannot be negative")
        if self.scatter_multiplier < 0:
            raise ValueError("scatter multiplier cannot be negative")


@dataclass(frozen=True)
class CArmResult:
    """Result of one C-arm secondary barrier, with its intermediates."""

    unshielded_weekly_kerma_mGy: float
    required_transmission: float
    inputs: CArmInputs
    goal: DesignGoal
    scatter_mGy: float = 0.0
    leakage_mGy: float = 0.0
    terms: dict[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def shielding_required(self) -> bool:
        return self.required_transmission < 1.0


def unshielded_kerma(inputs: CArmInputs) -> tuple[float, float, dict[str, float], list[str]]:
    """Weekly scatter and leakage air kerma (mGy) at the point of protection."""
    notes: list[str] = []

    if inputs.scatter_fraction is not None:
        a1 = inputs.scatter_fraction
        notes.append(f"scatter fraction {a1:.4g} per cm2 at 1 m entered for this source")
    else:
        a1 = scatter_fraction(inputs.kvp, inputs.scatter_angle_deg)
        notes.append(
            f"scatter fraction {a1:.4g} per cm2 at 1 m from NCRP 147 Figure C.1 "
            f"at {inputs.kvp:g} kVp, {inputs.scatter_angle_deg:g} degrees"
        )

    kap_mGy_cm2 = inputs.kap_week_mGy_cm2

    # Equation C.2 with KAP substituted for K_P(1 m) * F / dF^2.
    scatter = kap_mGy_cm2 * a1 * inputs.scatter_multiplier / (inputs.scatter_distance_m**2)
    if inputs.scatter_multiplier != 1.0:
        notes.append(f"scatter scaled by {inputs.scatter_multiplier:g} for this barrier")

    # Primary air kerma at 1 m, needed only for the leakage estimate.
    primary_1m = kap_mGy_cm2 * (inputs.field_distance_m**2) / inputs.field_area_cm2

    if inputs.leakage_at_1m_uGy_week is not None:
        leakage_1m = inputs.leakage_at_1m_uGy_week / 1000.0
        notes.append(
            f"leakage taken as the measured {inputs.leakage_at_1m_uGy_week:g} uGy/week at 1 m"
        )
    elif inputs.leakage_fraction is not None:
        leakage_1m = primary_1m * inputs.leakage_fraction
        notes.append(
            f"leakage entered as {inputs.leakage_fraction:.3g} of the "
            f"{primary_1m:.4g} mGy/week primary air kerma at 1 m"
        )
    else:
        ratio = leakage_fraction_of_primary(inputs.kvp)
        leakage_1m = primary_1m * ratio
        workload = primary_1m / air_kerma_per_workload(inputs.kvp)
        notes.append(
            f"leakage from NCRP 147 Eq. C.6-C.8: {workload:.4g} mA min/week implied by "
            f"KAP, giving {ratio:.3g} of the {primary_1m:.4g} mGy/week primary at 1 m"
        )

    leakage = leakage_1m / (inputs.leakage_distance_m**2)

    terms = {
        "weekly KAP (mGy cm2)": inputs.kap_week_mGy_cm2,
        "scatter fraction a1 (per cm2 at 1 m)": a1,
        "scattering angle (deg)": inputs.scatter_angle_deg,
        "dS (m)": inputs.scatter_distance_m,
        "dL (m)": inputs.leakage_distance_m,
        "primary air kerma at 1 m (mGy/week)": primary_1m,
        "scatter (mGy/week)": scatter,
        "leakage (mGy/week)": leakage,
    }
    return scatter, leakage, terms, notes


def evaluate(inputs: CArmInputs, goal: DesignGoal) -> CArmResult:
    """Total secondary kerma and the transmission needed to meet ``goal``."""
    if goal.quantity != "air_kerma":
        raise ValueError(f"NCRP 147 requires an air-kerma design goal, got {goal.quantity!r}")

    scatter, leakage, terms, notes = unshielded_kerma(inputs)
    total = scatter + leakage
    b = float("inf") if total <= 0 else goal.value / (inputs.occupancy * total)
    terms["occupancy T"] = inputs.occupancy
    terms["design goal P (mGy/week)"] = goal.value
    notes.append(
        "primary term is zero: the image receptor is the primary-beam stop, "
        "so this barrier sees scatter and leakage only"
    )
    return CArmResult(
        unshielded_weekly_kerma_mGy=total,
        required_transmission=b,
        inputs=inputs,
        goal=goal,
        scatter_mGy=scatter,
        leakage_mGy=leakage,
        terms=terms,
        notes=tuple(notes),
    )


def scatter_transmission(inputs: CArmInputs, material: str, thickness_mm: float) -> float:
    """Equation C.3: scatter is attenuated as the primary beam is."""
    return transmission(tables.primary_archer_by_kvp(inputs.kvp, material), thickness_mm)


def leakage_transmission(inputs: CArmInputs, material: str, thickness_mm: float) -> float:
    """Equation C.8: exp(-ln2 x / x_half) at the high-attenuation half-value layer.

    For the Archer form the high-attenuation HVL is ``ln2 / alpha``, so this
    reduces to ``exp(-alpha x)`` -- the asymptote's slope without its
    prefactor, which is what the report specifies.
    """
    alpha = tables.primary_archer_by_kvp(inputs.kvp, material).alpha
    return math.exp(-alpha * thickness_mm)


def transmitted_fraction(inputs: CArmInputs, material: str, thickness_mm: float) -> float:
    """Fraction of the unshielded secondary kerma passing ``thickness_mm``.

    The two components are attenuated by their own transmissions and summed,
    per Equation C.16 restricted to the secondary terms.
    """
    scatter, leakage, _, _ = unshielded_kerma(inputs)
    total = scatter + leakage
    if total <= 0:
        return 1.0
    return (
        scatter * scatter_transmission(inputs, material, thickness_mm)
        + leakage * leakage_transmission(inputs, material, thickness_mm)
    ) / total


def thickness_for_transmission(
    inputs: CArmInputs, material: str, b: float, *, tolerance: float = 1e-9
) -> float:
    """Thickness in mm whose summed component transmission is ``b``.

    NCRP 147 Section C.4: no closed form exists once the components carry
    different transmissions, so the thickness is found by iteration.  Bisection
    is used rather than the report's exponential interpolation because the
    bracket is cheap to establish and the result is then exact to the
    tolerance rather than to the shape assumption.
    """
    if not math.isfinite(b) or b >= 1.0:
        return 0.0
    if b <= 0.0:
        raise ValueError("required transmission must be positive")

    low = 0.0
    high = 1.0
    for _ in range(200):
        if transmitted_fraction(inputs, material, high) <= b:
            break
        high *= 2.0
    else:  # pragma: no cover - only reachable for a non-attenuating material
        raise ValueError(f"no thickness of {material} reaches transmission {b:g}")

    while high - low > tolerance:
        mid = 0.5 * (low + high)
        if transmitted_fraction(inputs, material, mid) > b:
            low = mid
        else:
            high = mid
    return high


def barrier_params(inputs: CArmInputs, material: str) -> ArcherParams:
    """A single secondary fit for this source's kVp, for walls along the path.

    Barriers the ray merely crosses on its way to the point are attenuated
    with one transmission curve rather than component-wise, so Table C.1's
    combined secondary fit at this kVp is used for them.  The barrier being
    solved for is handled component-wise instead, by
    :func:`thickness_for_transmission`.
    """
    return tables.secondary_archer(str(inputs.kvp), material, by_kvp=True)
