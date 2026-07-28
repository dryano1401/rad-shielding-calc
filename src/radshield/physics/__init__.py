"""Shielding physics: NCRP 147 (x-ray, fluoroscopy, CT) and TG-108 (PET/nuclear medicine).

Both methodologies share the Archer three-parameter transmission model, so
:mod:`radshield.physics.archer` serves both and adding an isotope or material
is a data change rather than a code change.
"""

from . import archer, decay, limits, nuclides, tg108
from .archer import ArcherParams
from .limits import DesignGoal, ncrp147_goal, tg108_goal
from .nuclides import Nuclide

__all__ = [
    "ArcherParams",
    "DesignGoal",
    "Nuclide",
    "archer",
    "decay",
    "limits",
    "ncrp147_goal",
    "nuclides",
    "tg108",
    "tg108_goal",
]
