"""CSV table loading with caching.

All published data lives in ``physics/data/*.csv`` rather than in Python
literals so that values can be audited, diffed, and extended without touching
code.  User-supplied tables (isotopes absent from TG-108, in-house material
data) are added by appending rows or by pointing ``set_data_dir`` at an
override directory.
"""

from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Any

_DEFAULT_DIR = Path(__file__).parent / "data"
_data_dir = _DEFAULT_DIR


def set_data_dir(path: str | Path) -> None:
    """Point the loader at an alternative data directory and clear the cache."""
    global _data_dir
    _data_dir = Path(path)
    load_table.cache_clear()


def data_dir() -> Path:
    """Return the directory currently used for table lookups."""
    return _data_dir


def _coerce(value: str) -> Any:
    """Convert a CSV cell to float where possible, else return the stripped string."""
    text = value.strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return text


@lru_cache(maxsize=None)
def load_table(name: str) -> tuple[dict[str, Any], ...]:
    """Load ``<name>.csv`` from the data directory as a tuple of row dicts.

    Numeric cells become floats; empty cells become ``None``.  The result is
    cached and immutable, so callers must not mutate the returned dicts.
    """
    path = _data_dir / f"{name}.csv"
    if not path.exists():
        raise FileNotFoundError(f"data table {name!r} not found at {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return tuple({k: _coerce(v) for k, v in row.items()} for row in csv.DictReader(handle))
