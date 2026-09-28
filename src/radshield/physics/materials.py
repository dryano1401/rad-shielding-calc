"""Material naming shared by both methodologies.

The two source documents spell the same material differently: NCRP 147's
tables say "Gypsum Wallboard" and "Plate Glass", while TG-108's transmission
fits are registered as "gypsum" and "glass".  A barrier named by one
convention and looked up under the other has to resolve anyway, because the
consequence of it not resolving is a barrier silently dropped from a path --
which only ever understates the shielding, and looks identical to a control
that does nothing.

This maps every accepted spelling onto one lowercase key.  It deliberately
does *not* merge steel with iron: NCRP 147 fits steel and TG-108 fits iron,
they are different materials with different data, and conflating them would
attribute one's attenuation to the other.
"""

from __future__ import annotations

ALIASES: dict[str, str] = {
    "lead": "lead",
    "pb": "lead",
    "concrete": "concrete",
    "gypsum": "gypsum",
    "gypsum wallboard": "gypsum",
    "wallboard": "gypsum",
    "drywall": "gypsum",
    "steel": "steel",
    "iron": "iron",
    "glass": "glass",
    "plate glass": "glass",
    "wood": "wood",
}


def normalise(name: str) -> str:
    """Reduce a material name to its shared key.

    An unrecognised name is returned casefolded rather than rejected, so a
    material somebody registered under a name this module has never heard of
    still matches itself.
    """
    key = name.strip().casefold()
    return ALIASES.get(key, key)
