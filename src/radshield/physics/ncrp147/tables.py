"""Lookup layer over the extracted NCRP 147 reference tables.

Data provenance: transcribed from NCRP Report No. 147 (2015 printing) by text
extraction, then converted to CSV.  Known gaps in the extraction are declared
in :data:`KNOWN_GAPS` and raised as informative errors at lookup time rather
than silently returning a neighbouring value.

Archer parameters here are in mm^-1, so thicknesses are in **mm** -- unlike
TG-108's cm^-1 parameters.  ``ArcherParams.unit`` carries this.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..archer import ArcherParams
from ..data_loader import load_table

# Canonical material keys mapped to the spellings used in the source tables.
_MATERIAL_ALIASES = {
    "lead": "Lead",
    "concrete": "Concrete",
    "gypsum": "Gypsum Wallboard",
    "gypsum wallboard": "Gypsum Wallboard",
    "steel": "Steel",
    "glass": "Plate Glass",
    "plate glass": "Plate Glass",
    "wood": "Wood",
}

KNOWN_GAPS = (
    "Table A.1: the 40 and 45 kVp rows were captured for concrete only; other "
    "materials jump 35 -> 50 kVp.",
    "Table B.1: the Peripheral Angiography row is missing for steel, plate glass and wood.",
    "Table C.1: for steel, plate glass and wood only the 30, 50, 70, 125 and 150 kVp rows "
    "were captured -- the 100 kVp row and all workload-distribution rows are absent.",
    "Table B.1 occupancy factors were not part of the extraction; the shipped "
    "ncrp147_occupancy.csv is seeded from the published table and marked NEEDS_VERIFICATION.",
    "Section 5 CT scatter data (isodose maps / DLP scatter fractions) was not extracted; "
    "CT calculations require caller-supplied scatter parameters.",
)


class TableLookupError(KeyError):
    """Raised when a requested table entry is absent from the extraction."""


def canonical_material(name: str) -> str:
    """Map a material name to the spelling used in the source tables."""
    key = name.strip().lower()
    try:
        return _MATERIAL_ALIASES[key]
    except KeyError:
        raise TableLookupError(
            f"unknown material {name!r}; known: {sorted(set(_MATERIAL_ALIASES))}"
        ) from None


def _archer_from_row(row: dict, material: str, source: str) -> ArcherParams:
    return ArcherParams(
        alpha=float(row["alpha_per_mm"]),
        beta=float(row["beta_per_mm"]),
        gamma=float(row["gamma"]),
        unit="mm",
        material=material,
        source=source,
    )


def primary_archer_by_kvp(kvp: float, material: str) -> ArcherParams:
    """Table A.1: primary broad-beam fit parameters at a single kVp."""
    mat = canonical_material(material)
    for row in load_table("ncrp147_primary_archer_kvp"):
        if row["material"] == mat and float(row["kvp"]) == float(kvp):
            return _archer_from_row(row, mat, f"NCRP 147 Table A.1, {kvp:g} kVp")
    available = sorted(
        float(r["kvp"]) for r in load_table("ncrp147_primary_archer_kvp") if r["material"] == mat
    )
    raise TableLookupError(
        f"no Table A.1 primary fit for {mat} at {kvp:g} kVp; available: {available}. "
        f"Known extraction gaps: {KNOWN_GAPS[0]}"
    )


def primary_archer_by_workload(workload: str, material: str) -> ArcherParams:
    """Table B.1: primary fit parameters pre-integrated over a workload distribution."""
    mat = canonical_material(material)
    for row in load_table("ncrp147_primary_archer_workload"):
        if row["material"] == mat and row["workload"] == workload:
            return _archer_from_row(row, mat, f"NCRP 147 Table B.1, {workload}")
    available = sorted(
        str(r["workload"])
        for r in load_table("ncrp147_primary_archer_workload")
        if r["material"] == mat
    )
    raise TableLookupError(
        f"no Table B.1 primary fit for {mat} / {workload!r}; available: {available}. "
        f"Known extraction gaps: {KNOWN_GAPS[1]}"
    )


def secondary_archer(key: str, material: str, *, by_kvp: bool = False) -> ArcherParams:
    """Table C.1: secondary (leakage + scatter) fit parameters.

    Args:
        key: Either a workload distribution name, or a kVp value as a string
            when ``by_kvp`` is True.
        material: Material name.
        by_kvp: Select the single-kVp rows rather than the workload rows.
    """
    mat = canonical_material(material)
    key_type = "kvp" if by_kvp else "workload"
    for row in load_table("ncrp147_secondary_archer"):
        if row["material"] == mat and row["key_type"] == key_type:
            row_key = row["key"]
            matches = (
                float(row_key) == float(key) if by_kvp else str(row_key) == key
            )
            if matches:
                label = f"{key} kVp" if by_kvp else str(key)
                return _archer_from_row(row, mat, f"NCRP 147 Table C.1, {label}")
    available = sorted(
        str(r["key"])
        for r in load_table("ncrp147_secondary_archer")
        if r["material"] == mat and r["key_type"] == key_type
    )
    raise TableLookupError(
        f"no Table C.1 secondary fit for {mat} / {key!r} ({key_type}); available: {available}. "
        f"Known extraction gaps: {KNOWN_GAPS[2]}"
    )


@dataclass(frozen=True)
class WorkloadTotals:
    """Table 4.2 totals for one workload distribution."""

    workload: str
    wnorm_ma_min_per_patient: float
    patients_per_week: float


def workload_totals(workload: str) -> WorkloadTotals:
    """Table 4.2: total normalised workload and surveyed patient load."""
    for row in load_table("ncrp147_workload_totals"):
        if row["workload"] == workload:
            return WorkloadTotals(
                workload=workload,
                wnorm_ma_min_per_patient=float(row["wnorm_ma_min_per_patient"]),
                patients_per_week=float(row["patients_per_week"]),
            )
    available = sorted(str(r["workload"]) for r in load_table("ncrp147_workload_totals"))
    raise TableLookupError(f"unknown workload distribution {workload!r}; available: {available}")


def available_workloads() -> list[str]:
    """Return the workload distribution names present in Table 4.2."""
    return sorted(str(r["workload"]) for r in load_table("ncrp147_workload_totals"))


def primary_air_kerma(workload: str) -> float:
    """Table 4.5: unshielded primary air kerma per patient at 1 m, K1^P, in mGy."""
    for row in load_table("ncrp147_k1p"):
        if row["workload"] == workload:
            return float(row["k1p_mgy_per_patient"])
    available = sorted(str(r["workload"]) for r in load_table("ncrp147_k1p"))
    raise TableLookupError(
        f"no Table 4.5 primary air kerma for {workload!r}; available: {available}. "
        "Table 4.5 covers primary-beam rooms only."
    )


def secondary_air_kerma(workload: str, *, geometry: str = "side") -> float:
    """Table 4.7: unshielded secondary air kerma per patient at 1 m, K1^sec, in mGy.

    Args:
        workload: Workload distribution name.
        geometry: ``"side"`` for leakage + side-scatter, or ``"forward_back"``
            for leakage + forward/backscatter (the larger, more conservative
            value).
    """
    column = {
        "side": "k1sec_side_mgy_per_patient",
        "forward_back": "k1sec_fwd_back_mgy_per_patient",
    }.get(geometry)
    if column is None:
        raise ValueError(f"geometry must be 'side' or 'forward_back', got {geometry!r}")
    for row in load_table("ncrp147_k1sec"):
        if row["workload"] == workload:
            return float(row[column])
    available = sorted(str(r["workload"]) for r in load_table("ncrp147_k1sec"))
    raise TableLookupError(f"no Table 4.7 secondary kerma for {workload!r}; available: {available}")


def use_factor(barrier: str) -> float:
    """Table 4.4: primary beam use factor U for a general radiographic room."""
    for row in load_table("ncrp147_use_factors"):
        if str(row["barrier"]).lower() == barrier.strip().lower():
            return float(row["use_factor"])
    available = sorted(str(r["barrier"]) for r in load_table("ncrp147_use_factors"))
    raise TableLookupError(f"unknown barrier {barrier!r}; available: {available}")


def occupancy_factors() -> list[tuple[float, str, bool]]:
    """Return ``(factor, description, verified)`` for the occupancy table.

    The shipped values are seeded from NCRP 147 Table B.1 but were not part of
    the source extraction, so ``verified`` is False for all of them until
    checked against the report.
    """
    return [
        (
            float(row["occupancy_factor"]),
            str(row["area_description"]),
            str(row["verified"]).upper() != "NEEDS_VERIFICATION",
        )
        for row in load_table("ncrp147_occupancy")
    ]
