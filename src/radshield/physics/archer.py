"""The Archer three-parameter broad-beam transmission model.

Both methodologies this package implements use the same functional form:

    B(x) = [ (1 + beta/alpha) * exp(alpha*gamma*x) - beta/alpha ] ** (-1/gamma)

inverted for thickness as

    x(B) = (1 / (alpha*gamma)) * ln[ (B**-gamma + beta/alpha) / (1 + beta/alpha) ]

NCRP 147 (Eq. A.2/A.3) tabulates alpha and beta in mm^-1, so x is in mm.
TG-108 (Table V) tabulates them in cm^-1, so x is in cm.  Nothing here knows
or cares which -- the caller supplies parameters and reads the answer back in
whatever length unit the parameters were tabulated in.  ``ArcherParams``
carries that unit as metadata so the layers above cannot mix them up.

The 10x error this prevents is real and easy to hit: TG-108 Table IV prints
lead thickness in *mm* while its Table V parameters are in *cm*^-1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

LengthUnit = Literal["mm", "cm"]


class ArcherError(ValueError):
    """Raised when a transmission or thickness request is outside the model's domain."""


@dataclass(frozen=True)
class ArcherParams:
    """Fit parameters for one (material, radiation quality) pair.

    Attributes:
        alpha: First fit parameter, in ``1/unit``.
        beta: Second fit parameter, in ``1/unit``.  May be negative (TG-108's
            511 keV fits all have beta < 0).
        gamma: Third fit parameter, dimensionless.
        unit: Length unit the parameters are tabulated in; thicknesses passed
            to and returned from this model are in this unit.
        material: Material name, for error messages and audit output.
        source: Citation for the values, carried through to the audit trail.
    """

    alpha: float
    beta: float
    gamma: float
    unit: LengthUnit
    material: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        if self.alpha <= 0:
            raise ArcherError(f"alpha must be positive, got {self.alpha} for {self.material!r}")
        if self.gamma <= 0:
            raise ArcherError(f"gamma must be positive, got {self.gamma} for {self.material!r}")


def transmission(params: ArcherParams, thickness: float) -> float:
    """Return the broad-beam transmission factor B through ``thickness``.

    Args:
        params: Fit parameters; ``thickness`` must be in ``params.unit``.
        thickness: Barrier thickness, >= 0.

    Returns:
        Transmission factor in (0, 1].  B(0) is exactly 1.
    """
    if thickness < 0:
        raise ArcherError(f"thickness must be non-negative, got {thickness}")
    if thickness == 0:
        return 1.0

    ratio = params.beta / params.alpha
    inner = (1.0 + ratio) * math.exp(params.alpha * params.gamma * thickness) - ratio
    if inner <= 0:
        # Only reachable for pathological fits; the tabulated sets do not hit it.
        raise ArcherError(
            f"Archer model undefined at x={thickness} {params.unit} for {params.material!r}"
        )
    return inner ** (-1.0 / params.gamma)


def thickness(params: ArcherParams, b: float) -> float:
    """Return the barrier thickness required to achieve transmission ``b``.

    Args:
        params: Fit parameters; the result is in ``params.unit``.
        b: Required transmission factor.  Values >= 1 mean no barrier is
            needed and return 0.0.

    Returns:
        Required thickness in ``params.unit``, >= 0.
    """
    if b <= 0:
        raise ArcherError(f"transmission must be positive, got {b}")
    if b >= 1.0:
        return 0.0

    ratio = params.beta / params.alpha
    numerator = b ** (-params.gamma) + ratio
    denominator = 1.0 + ratio
    if numerator <= 0 or denominator <= 0:
        raise ArcherError(
            f"Archer inversion undefined at B={b} for {params.material!r}; "
            "the required transmission lies outside the fit's valid range"
        )
    x = math.log(numerator / denominator) / (params.alpha * params.gamma)
    # Guard against tiny negative values from floating-point noise near B=1.
    return max(x, 0.0)


def equilibrium_hvl(params: ArcherParams) -> float:
    """Half-value layer in the thick-barrier limit, ln(2)/alpha, in ``params.unit``."""
    return math.log(2.0) / params.alpha


def equilibrium_tvl(params: ArcherParams) -> float:
    """Tenth-value layer in the thick-barrier limit, ln(10)/alpha, in ``params.unit``."""
    return math.log(10.0) / params.alpha
