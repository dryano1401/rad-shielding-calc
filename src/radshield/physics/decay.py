"""Radioactive decay factors used by the TG-108 dose equations."""

from __future__ import annotations

import math


def decay_factor(half_life_h: float, elapsed_h: float) -> float:
    """Fractional activity remaining after ``elapsed_h``.

    This is TG-108's ``F_U`` when ``elapsed_h`` is the uptake time:
    ``exp(-0.693 * t / T_half)``.
    """
    if half_life_h <= 0:
        raise ValueError(f"half-life must be positive, got {half_life_h}")
    if elapsed_h < 0:
        raise ValueError(f"elapsed time must be non-negative, got {elapsed_h}")
    return math.exp(-math.log(2.0) * elapsed_h / half_life_h)


def dose_reduction_factor(half_life_h: float, duration_h: float) -> float:
    """TG-108 Eq. 1: ``R(t)``, the decay-averaged fraction of the initial dose rate.

    ``R(t) = 1.443 * (T_half / t) * (1 - exp(-0.693 * t / T_half))``

    Integrating the decaying dose rate over ``t`` and dividing by the
    undecayed product ``D_dot_0 * t`` gives this factor, which is always in
    (0, 1] and tends to 1 as ``t`` tends to 0.

    For F-18 this yields 0.91, 0.83 and 0.76 at 30, 60 and 90 minutes.
    """
    if half_life_h <= 0:
        raise ValueError(f"half-life must be positive, got {half_life_h}")
    if duration_h < 0:
        raise ValueError(f"duration must be non-negative, got {duration_h}")
    if duration_h == 0:
        return 1.0
    mean_life_ratio = half_life_h / (math.log(2.0) * duration_h)
    return mean_life_ratio * (1.0 - math.exp(-math.log(2.0) * duration_h / half_life_h))
