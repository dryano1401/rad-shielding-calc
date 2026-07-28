"""Shielding design goals.

The two methodologies express their design goal in different quantities:
TG-108 works in effective dose equivalent (uSv per week), NCRP 147 in air
kerma (mGy per week).  Numerically the weekly values coincide (0.02 mGy =
20 uSv), but they are not the same quantity and are kept separate so audit
output states the correct one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AreaClass = Literal["controlled", "uncontrolled"]

# TG-108: 1 mSv/y to the public implies 20 uSv/week; controlled areas use the
# 5 mSv/y ALARA target rather than the 50 mSv/y regulatory limit.
TG108_WEEKLY_LIMIT_USV: dict[AreaClass, float] = {
    "uncontrolled": 20.0,
    "controlled": 100.0,
}

# NCRP 147 Section 3: P = 0.1 mGy/week controlled (5 mGy/y),
# 0.02 mGy/week uncontrolled (1 mGy/y).
NCRP147_WEEKLY_LIMIT_MGY: dict[AreaClass, float] = {
    "uncontrolled": 0.02,
    "controlled": 0.1,
}

# Weeks per year assumed when converting an annual limit to a weekly one.
WEEKS_PER_YEAR = 50.0


@dataclass(frozen=True)
class DesignGoal:
    """A weekly shielding design goal.

    Attributes:
        value: Weekly limit, in uSv for TG-108 or mGy for NCRP 147.
        quantity: ``"effective_dose_equivalent"`` or ``"air_kerma"``.
        area_class: Controlled or uncontrolled.
        basis: Free text describing where the value came from.
    """

    value: float
    quantity: str
    area_class: AreaClass
    basis: str = ""

    def __post_init__(self) -> None:
        if self.value <= 0:
            raise ValueError(f"design goal must be positive, got {self.value}")


def tg108_goal(area_class: AreaClass) -> DesignGoal:
    """Return the default TG-108 weekly design goal in uSv."""
    return DesignGoal(
        value=TG108_WEEKLY_LIMIT_USV[area_class],
        quantity="effective_dose_equivalent",
        area_class=area_class,
        basis=f"TG-108 default for {area_class} areas",
    )


def ncrp147_goal(area_class: AreaClass) -> DesignGoal:
    """Return the default NCRP 147 weekly design goal in mGy air kerma."""
    return DesignGoal(
        value=NCRP147_WEEKLY_LIMIT_MGY[area_class],
        quantity="air_kerma",
        area_class=area_class,
        basis=f"NCRP 147 Section 3 default for {area_class} areas",
    )
