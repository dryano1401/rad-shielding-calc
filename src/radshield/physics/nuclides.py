"""Radionuclide properties and per-nuclide barrier attenuation data.

TG-108 tabulates dose-rate constants and 511 keV transmission fits for
positron emitters.  Any other isotope -- Tc-99m, I-131, In-111, Ga-67 -- uses
an *identical* calculation path; only the constants differ.  So nuclides and
their Archer parameters are data, registered here, not special cases in the
solver.

Adding an isotope TG-108 does not cover:

    >>> register_nuclide(Nuclide("Tc-99m", half_life_min=360.6,
    ...                          gamma_eff=0.0195, source="in-house"))
    >>> register_archer("Tc-99m", ArcherParams(alpha=..., beta=..., gamma=...,
    ...                                        unit="cm", material="lead"))

after which every TG-108-style calculation works unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from .archer import ArcherParams
from .data_loader import load_table

# Ratio of the patient-self-attenuated dose-rate constant to the free-in-air
# effective dose equivalent constant, from TG-108's F-18 pair: 0.092 / 0.143.
# TG-108 states this only for F-18.  Applying it to other positron emitters is
# a defensible approximation because the emitted photon is the same 511 keV
# annihilation quantum and the attenuating body habitus is the same; applying
# it to a non-511 keV isotope is NOT defensible and raises unless the caller
# supplies an explicit constant.
_F18_PATIENT_ATTENUATION_RATIO = 0.092 / 0.143


class NuclideError(KeyError):
    """Raised when a nuclide or its attenuation data is not registered."""


@dataclass(frozen=True)
class Nuclide:
    """Physical constants for one radionuclide.

    Attributes:
        name: Identifier, e.g. ``"F-18"``.
        half_life_min: Physical half-life in minutes.
        gamma_eff: Effective dose equivalent rate constant, free in air, in
            uSv m^2 / (MBq h).
        gamma_patient: Dose-rate constant after patient self-attenuation, same
            units.  ``None`` means it is not tabulated; see
            :func:`patient_dose_rate_constant`.
        is_511_kev: True for positron emitters whose dominant emission is the
            511 keV annihilation photon, which controls whether the F-18
            patient-attenuation ratio may be borrowed.
        source: Citation, carried into audit output.
    """

    name: str
    half_life_min: float
    gamma_eff: float
    gamma_patient: float | None = None
    is_511_kev: bool = True
    source: str = ""

    @property
    def half_life_h(self) -> float:
        """Physical half-life in hours."""
        return self.half_life_min / 60.0


_registry: dict[str, Nuclide] = {}
_archer: dict[tuple[str, str], ArcherParams] = {}


def _load_builtin() -> None:
    """Populate the registry from the shipped TG-108 tables."""
    for row in load_table("tg108_nuclides"):
        name = str(row["nuclide"])
        _registry[name] = Nuclide(
            name=name,
            half_life_min=float(row["half_life_min"]),
            gamma_eff=float(row["gamma_eff_uSv_m2_per_MBq_h"]),
            gamma_patient=row["gamma_patient_uSv_m2_per_MBq_h"],
            is_511_kev=True,
            source=str(row["source"] or ""),
        )
    for row in load_table("tg108_archer_511kev"):
        material = str(row["material"])
        params = ArcherParams(
            alpha=float(row["alpha_per_cm"]),
            beta=float(row["beta_per_cm"]),
            gamma=float(row["gamma"]),
            unit="cm",
            material=material,
            source=str(row["source"] or ""),
        )
        # 511 keV attenuation is a property of the photon energy, not the
        # parent nuclide, so every positron emitter shares this fit.
        for name, nuc in _registry.items():
            if nuc.is_511_kev:
                _archer[(name, material)] = params


_load_builtin()


def get_nuclide(name: str) -> Nuclide:
    """Return the registered nuclide, raising ``NuclideError`` if absent."""
    try:
        return _registry[name]
    except KeyError:
        raise NuclideError(
            f"nuclide {name!r} is not registered; known: {sorted(_registry)}"
        ) from None


def available_nuclides() -> list[str]:
    """Return the names of all registered nuclides, sorted."""
    return sorted(_registry)


def register_nuclide(nuclide: Nuclide, *, overwrite: bool = False) -> None:
    """Register a nuclide not covered by TG-108.

    Raises:
        ValueError: If the name is already registered and ``overwrite`` is
            False -- guarding against a typo silently redefining F-18.
    """
    if nuclide.name in _registry and not overwrite:
        raise ValueError(f"nuclide {nuclide.name!r} already registered; pass overwrite=True")
    _registry[nuclide.name] = nuclide


def register_archer(nuclide: str, params: ArcherParams, *, overwrite: bool = False) -> None:
    """Register barrier transmission fit parameters for a nuclide and material.

    ``params.material`` names the material.  This is the hook for isotopes
    absent from TG-108: supply alpha, beta, gamma from your own fits or from
    another published source and the rest of the pipeline is unchanged.
    """
    if not params.material:
        raise ValueError("ArcherParams.material must be set when registering nuclide data")
    key = (nuclide, params.material)
    if key in _archer and not overwrite:
        raise ValueError(f"Archer parameters for {key} already registered; pass overwrite=True")
    _archer[key] = params


def get_archer(nuclide: str, material: str) -> ArcherParams:
    """Return the transmission fit for a nuclide/material pair."""
    get_nuclide(nuclide)  # Surface an unknown-nuclide error before an unknown-material one.
    try:
        return _archer[(nuclide, material)]
    except KeyError:
        known = sorted(m for n, m in _archer if n == nuclide)
        raise NuclideError(
            f"no transmission data for {nuclide!r} in {material!r}; "
            f"registered materials for this nuclide: {known}. "
            "Use register_archer() to supply alpha/beta/gamma."
        ) from None


def available_materials(nuclide: str) -> list[str]:
    """Return materials with registered transmission data for ``nuclide``."""
    return sorted(m for n, m in _archer if n == nuclide)


def patient_dose_rate_constant(nuclide: str) -> tuple[float, str]:
    """Return the patient-self-attenuated dose-rate constant and its provenance.

    Returns:
        ``(constant, provenance)`` where the constant is in
        uSv m^2 / (MBq h) and provenance describes where it came from, for the
        audit trail.

    Raises:
        NuclideError: If the nuclide is not a 511 keV emitter and no explicit
            attenuated constant is tabulated.  Silently scaling a 140 keV
            isotope by F-18's body-attenuation ratio would be wrong, so this
            fails loudly instead.
    """
    nuc = get_nuclide(nuclide)
    if nuc.gamma_patient is not None:
        return float(nuc.gamma_patient), f"tabulated ({nuc.source})"
    if not nuc.is_511_kev:
        raise NuclideError(
            f"{nuclide!r} has no tabulated patient-attenuated dose-rate constant and is not a "
            "511 keV emitter, so F-18's attenuation ratio cannot be borrowed. Supply "
            "Nuclide.gamma_patient explicitly."
        )
    value = nuc.gamma_eff * _F18_PATIENT_ATTENUATION_RATIO
    return value, (
        f"derived: gamma_eff {nuc.gamma_eff} x F-18 patient-attenuation ratio "
        f"{_F18_PATIENT_ATTENUATION_RATIO:.4f} (TG-108 0.092/0.143)"
    )
