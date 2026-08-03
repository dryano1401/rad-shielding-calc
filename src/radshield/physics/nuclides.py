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

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .archer import ArcherError, ArcherParams
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

# Pristine snapshot of the shipped TG-108 tables, captured before any
# user-editable overlay is applied.  This is what "reset to default" restores
# and what distinguishes a user edit from the original value.
_BUILTIN_NUCLIDES: dict[str, Nuclide] = dict(_registry)
_BUILTIN_ARCHER: dict[tuple[str, str], ArcherParams] = dict(_archer)


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


# --- Editable overlay -------------------------------------------------------
#
# Everything above this line is read-only, shipped-with-the-package data. The
# functions below let the running application add isotopes TG-108 does not
# cover, or correct a shipped value, and have that edit survive a restart --
# without ever touching the packaged CSVs (which stay the auditable, diffable
# record of what TG-108 itself says). Edits are stored as a JSON overlay,
# applied on top of the builtin tables at import time, keyed by nuclide name.
# Because the overlay is applied after ``_load_builtin`` but the pristine
# values were already snapshotted into ``_BUILTIN_*`` above, a user edit can
# always be told apart from a shipped default and reset back to it.


def _custom_store_path() -> Path:
    """Location of the user-editable overlay file.

    Defaults to ``~/.radshield/custom_nuclides.json``; set ``RADSHIELD_HOME``
    to relocate it (tests use this to avoid touching a real home directory).
    """
    base = os.environ.get("RADSHIELD_HOME")
    return (Path(base) if base else Path.home() / ".radshield") / "custom_nuclides.json"


def _nuclide_to_dict(name: str) -> dict:
    nuc = _registry[name]
    return {
        "name": nuc.name,
        "half_life_min": nuc.half_life_min,
        "gamma_eff": nuc.gamma_eff,
        "gamma_patient": nuc.gamma_patient,
        "is_511_kev": nuc.is_511_kev,
        "source": nuc.source,
        "archer": {
            material: {
                "alpha": params.alpha,
                "beta": params.beta,
                "gamma": params.gamma,
                "unit": params.unit,
                "source": params.source,
            }
            for (n, material), params in _archer.items()
            if n == name
        },
    }


def _is_customized(name: str) -> bool:
    """True if ``name``'s current values differ from the shipped defaults."""
    if _registry.get(name) != _BUILTIN_NUCLIDES.get(name):
        return True
    materials = {m for n, m in _archer if n == name}
    builtin_materials = {m for n, m in _BUILTIN_ARCHER if n == name}
    if materials != builtin_materials:
        return True
    return any(_archer[(name, m)] != _BUILTIN_ARCHER.get((name, m)) for m in materials)


def load_custom_overlay(path: Path | None = None) -> None:
    """Apply a persisted overlay on top of the builtin tables.

    Silently does nothing if the file is absent, empty, or unreadable -- a
    corrupt overlay should not prevent the application from starting with
    the shipped defaults.
    """
    target = path or _custom_store_path()
    if not target.exists():
        return
    try:
        payload = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        return
    for entry in payload.get("nuclides", []):
        try:
            _apply_record(entry)
        except (KeyError, TypeError, ValueError, ArcherError):
            continue  # skip a malformed entry rather than fail application startup


def _save_custom_overlay(path: Path | None = None) -> None:
    target = path or _custom_store_path()
    customized = [_nuclide_to_dict(name) for name in sorted(_registry) if _is_customized(name)]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"nuclides": customized}, indent=2))


def _apply_record(entry: dict) -> None:
    name = str(entry["name"])
    half_life_min = float(entry["half_life_min"])
    gamma_eff = float(entry["gamma_eff"])
    if half_life_min <= 0:
        raise ValueError("half_life_min must be positive")
    if gamma_eff <= 0:
        raise ValueError("gamma_eff must be positive")
    gamma_patient = entry.get("gamma_patient")
    nuc = Nuclide(
        name=name,
        half_life_min=half_life_min,
        gamma_eff=gamma_eff,
        gamma_patient=float(gamma_patient) if gamma_patient is not None else None,
        is_511_kev=bool(entry.get("is_511_kev", False)),
        source=str(entry.get("source", "")),
    )
    register_nuclide(nuc, overwrite=True)
    for key in [k for k in _archer if k[0] == name]:
        del _archer[key]
    for material, params in entry.get("archer", {}).items():
        register_archer(
            name,
            ArcherParams(
                alpha=float(params["alpha"]),
                beta=float(params["beta"]),
                gamma=float(params["gamma"]),
                unit=params.get("unit", "cm"),
                material=material,
                source=str(params.get("source", "")),
            ),
            overwrite=True,
        )


@dataclass(frozen=True)
class NuclideRecord:
    """A full isotope record for the editor UI: constants plus every material's fit.

    Attributes:
        is_builtin: True if this name ships with the package (TG-108 Table II).
        is_customized: True if the current values differ from the shipped
            default -- irrelevant (always False) for a name that is not builtin.
    """

    name: str
    half_life_min: float
    gamma_eff: float
    gamma_patient: float | None
    is_511_kev: bool
    source: str
    archer: dict[str, ArcherParams]
    is_builtin: bool
    is_customized: bool


def list_records() -> list[NuclideRecord]:
    """Every registered isotope, builtin or custom, for the editor UI."""
    records = []
    for name in sorted(_registry):
        nuc = _registry[name]
        archer = {m: p for (n, m), p in _archer.items() if n == name}
        records.append(
            NuclideRecord(
                name=name,
                half_life_min=nuc.half_life_min,
                gamma_eff=nuc.gamma_eff,
                gamma_patient=nuc.gamma_patient,
                is_511_kev=nuc.is_511_kev,
                source=nuc.source,
                archer=archer,
                is_builtin=name in _BUILTIN_NUCLIDES,
                is_customized=_is_customized(name),
            )
        )
    return records


def default_511_archer() -> dict[str, ArcherParams]:
    """The shipped 511 keV Table V fit, keyed by material.

    Every positron emitter registered from ``tg108_archer_511kev.csv`` shares
    an identical fit, so F-18's is representative. This is what a new
    isotope's Archer fields default to in the editor -- a reasonable starting
    point that the user overwrites with isotope-specific data when they have
    it, per the 511 keV annihilation photon being the common case this
    application was built around.
    """
    return {m: p for (n, m), p in _BUILTIN_ARCHER.items() if n == "F-18"}


def upsert_record(
    name: str,
    *,
    half_life_min: float,
    gamma_eff: float,
    gamma_patient: float | None,
    is_511_kev: bool,
    source: str,
    archer_by_material: dict[str, tuple[float, float, float, str, str]],
) -> NuclideRecord:
    """Add or edit an isotope, persisting the change to the overlay file.

    Args:
        archer_by_material: material name -> ``(alpha, beta, gamma, unit, source)``.
            A material omitted here has its transmission data removed.

    Raises:
        ValueError: If a physical constant is non-positive.
        ArcherError: If a material's alpha or gamma is non-positive.
    """
    if half_life_min <= 0:
        raise ValueError(f"half_life_min must be positive, got {half_life_min}")
    if gamma_eff <= 0:
        raise ValueError(f"gamma_eff must be positive, got {gamma_eff}")
    if gamma_patient is not None and gamma_patient <= 0:
        raise ValueError(f"gamma_patient must be positive, got {gamma_patient}")

    nuc = Nuclide(
        name=name,
        half_life_min=half_life_min,
        gamma_eff=gamma_eff,
        gamma_patient=gamma_patient,
        is_511_kev=is_511_kev,
        source=source,
    )
    register_nuclide(nuc, overwrite=True)
    for key in [k for k in _archer if k[0] == name]:
        del _archer[key]
    for material, (alpha, beta, gamma, unit, material_source) in archer_by_material.items():
        register_archer(
            name,
            ArcherParams(
                alpha=alpha, beta=beta, gamma=gamma,
                unit=unit, material=material, source=material_source,
            ),
            overwrite=True,
        )
    _save_custom_overlay()
    return next(r for r in list_records() if r.name == name)


def delete_or_reset_record(name: str) -> NuclideRecord | None:
    """Remove a custom isotope, or reset a builtin one back to its shipped default.

    Returns:
        The restored :class:`NuclideRecord` if ``name`` is builtin (now back
        to shipped values), or ``None`` if it was a purely custom isotope that
        has now been removed entirely.

    Raises:
        NuclideError: If ``name`` is not currently registered.
    """
    if name not in _registry:
        raise NuclideError(f"nuclide {name!r} is not registered; known: {sorted(_registry)}")

    for key in [k for k in _archer if k[0] == name]:
        del _archer[key]

    if name in _BUILTIN_NUCLIDES:
        _registry[name] = _BUILTIN_NUCLIDES[name]
        for (n, m), p in _BUILTIN_ARCHER.items():
            if n == name:
                _archer[(n, m)] = p
        _save_custom_overlay()
        return next(r for r in list_records() if r.name == name)

    del _registry[name]
    _save_custom_overlay()
    return None


load_custom_overlay()
