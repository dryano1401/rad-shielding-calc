"""Lookup layer over the extracted NCRP 147 reference tables.

Data provenance: transcribed from NCRP Report No. 147 (2015 printing) by text
extraction, then converted to CSV.  Known gaps in the extraction are declared
in :data:`KNOWN_GAPS`.

A kVp lookup (Table A.1 primary, Table C.1 secondary) that falls between two
tabulated values is linearly interpolated between them -- alpha, beta and
gamma independently -- with the substitution disclosed in
``ArcherParams.source``, the same way a physicist reading the printed table
by hand would use the nearer entries either side.  A kVp outside the
tabulated range is not extrapolated: that has no support in the data to
interpolate from, so it still raises rather than guessing.  A non-kVp lookup
(workload-keyed Table B.1, or Table C.1's workload rows) has no ordering to
interpolate along and is still exact-match only.

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
    "CT isodose maps are scanner-specific and are not shipped; the isodose method requires "
    "caller-supplied values. The DLP method ships kappa and the body factor "
    "(see ncrp147_ct_scatter.csv).",
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


def _interpolate_archer(
    candidates: list[tuple[float, dict]],
    kvp: float,
    material: str,
    table_label: str,
    gap_note: str = "",
) -> ArcherParams:
    """Archer fit at ``kvp``, interpolating between the two rows bracketing it.

    An exact match returns that row untouched -- no interpolation note, since
    none was needed. Otherwise alpha, beta and gamma are each linearly
    interpolated between the nearest tabulated kVp below and above, and the
    substitution is disclosed in the returned params' ``source``. A ``kvp``
    outside the tabulated range is not extrapolated, since a linear fit has
    no support out there to guess from -- raises instead, same as a missing
    exact match always has.
    """
    candidates = sorted(candidates, key=lambda c: c[0])
    for row_kvp, row in candidates:
        if row_kvp == kvp:
            return _archer_from_row(row, material, f"{table_label}, {kvp:g} kVp")

    lower = next((c for c in reversed(candidates) if c[0] < kvp), None)
    upper = next((c for c in candidates if c[0] > kvp), None)
    if lower is None or upper is None:
        available = [c[0] for c in candidates]
        note = f" Known extraction gaps: {gap_note}" if gap_note else ""
        raise TableLookupError(
            f"{kvp:g} kVp is outside the tabulated range for {material!r} in {table_label} "
            f"({min(available):g}-{max(available):g} kVp); available exact values: "
            f"{available}.{note}"
        )

    lo_kvp, lo_row = lower
    hi_kvp, hi_row = upper
    frac = (kvp - lo_kvp) / (hi_kvp - lo_kvp)

    def interp(field: str) -> float:
        return float(lo_row[field]) + frac * (float(hi_row[field]) - float(lo_row[field]))

    return ArcherParams(
        alpha=interp("alpha_per_mm"),
        beta=interp("beta_per_mm"),
        gamma=interp("gamma"),
        unit="mm",
        material=material,
        source=f"{table_label}, interpolated between {lo_kvp:g} and {hi_kvp:g} kVp",
    )


def primary_archer_by_kvp(kvp: float, material: str) -> ArcherParams:
    """Table A.1: primary broad-beam fit parameters, interpolated by kVp."""
    mat = canonical_material(material)
    candidates = [
        (float(row["kvp"]), row)
        for row in load_table("ncrp147_primary_archer_kvp")
        if row["material"] == mat
    ]
    if not candidates:
        raise TableLookupError(f"no Table A.1 primary fit captured for {mat} at any kVp")
    return _interpolate_archer(
        candidates, float(kvp), mat, "NCRP 147 Table A.1", gap_note=KNOWN_GAPS[0]
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
            when ``by_kvp`` is True -- interpolated between the two
            tabulated kVp rows bracketing it when there is no exact match.
        material: Material name.
        by_kvp: Select the single-kVp rows rather than the workload rows.
    """
    mat = canonical_material(material)
    key_type = "kvp" if by_kvp else "workload"
    rows = [
        row
        for row in load_table("ncrp147_secondary_archer")
        if row["material"] == mat and row["key_type"] == key_type
    ]
    if by_kvp:
        candidates = [(float(row["key"]), row) for row in rows]
        if not candidates:
            raise TableLookupError(f"no Table C.1 secondary fit captured for {mat} at any kVp")
        return _interpolate_archer(
            candidates, float(key), mat, "NCRP 147 Table C.1", gap_note=KNOWN_GAPS[2]
        )
    for row in rows:
        if str(row["key"]) == key:
            return _archer_from_row(row, mat, f"NCRP 147 Table C.1, {key}")
    available = sorted(str(row["key"]) for row in rows)
    raise TableLookupError(
        f"no Table C.1 secondary fit for {mat} / {key!r} (workload); available: {available}. "
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
