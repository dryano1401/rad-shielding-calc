"""Archer transmission model, checked against both reports' tabulated data."""

from __future__ import annotations

import csv
import math

import pytest

from radshield.physics import nuclides
from radshield.physics.archer import (
    ArcherError,
    ArcherParams,
    equilibrium_hvl,
    equilibrium_tvl,
    thickness,
    transmission,
)
from radshield.physics.data_loader import data_dir

TG108_LEAD = ArcherParams(1.543, -0.4408, 2.136, "cm", "lead")
TG108_CONCRETE = ArcherParams(0.1539, -0.1161, 2.0752, "cm", "concrete")
TG108_IRON = ArcherParams(0.5704, -0.3063, 0.6326, "cm", "iron")


def _table_iv_rows():
    with (data_dir() / "tg108_transmission_511kev.csv").open() as handle:
        return list(csv.DictReader(handle))


def test_transmission_at_zero_thickness_is_unity():
    assert transmission(TG108_LEAD, 0.0) == 1.0
    assert transmission(TG108_CONCRETE, 0.0) == 1.0


def test_tg108_table_iv_lead():
    """Table V parameters must reproduce the Table IV lead column.

    Lead thickness is printed in mm while the fit parameters are in cm^-1 --
    the conversion below is exactly the trap this test exists to catch.
    """
    for row in _table_iv_rows():
        thickness_mm = float(row["thickness"])
        expected = float(row["lead_mm_transmission"])
        got = transmission(TG108_LEAD, thickness_mm / 10.0)
        # The table is printed to four decimal places, so entries like 0.0005
        # carry only one significant figure; the absolute floor accounts for it.
        assert got == pytest.approx(expected, rel=0.02, abs=5e-5), f"{thickness_mm} mm Pb"


def test_tg108_table_iv_concrete():
    for row in _table_iv_rows():
        thickness_cm = float(row["thickness"])
        expected = float(row["concrete_cm_transmission"])
        got = transmission(TG108_CONCRETE, thickness_cm)
        assert got == pytest.approx(expected, rel=0.02, abs=5e-5), f"{thickness_cm} cm concrete"


def test_tg108_table_iv_iron():
    for row in _table_iv_rows():
        if not row["iron_cm_transmission"]:
            continue
        thickness_cm = float(row["thickness"])
        expected = float(row["iron_cm_transmission"])
        got = transmission(TG108_IRON, thickness_cm)
        assert got == pytest.approx(expected, rel=0.05, abs=5e-5), f"{thickness_cm} cm iron"


@pytest.mark.parametrize("params", [TG108_LEAD, TG108_CONCRETE, TG108_IRON])
@pytest.mark.parametrize("b", [0.9, 0.5, 0.2, 0.05, 0.01, 0.001])
def test_forward_inverse_round_trip(params, b):
    """x(B) and B(x) must be exact inverses."""
    x = thickness(params, b)
    assert transmission(params, x) == pytest.approx(b, rel=1e-9)


def test_transmission_is_monotonically_decreasing():
    previous = 1.0
    for x in [0.1 * i for i in range(1, 40)]:
        current = transmission(TG108_LEAD, x)
        assert current < previous
        previous = current


def test_no_barrier_needed_when_transmission_at_or_above_one():
    assert thickness(TG108_LEAD, 1.0) == 0.0
    assert thickness(TG108_LEAD, 2.5) == 0.0


def test_invalid_inputs_raise():
    with pytest.raises(ArcherError):
        transmission(TG108_LEAD, -1.0)
    with pytest.raises(ArcherError):
        thickness(TG108_LEAD, 0.0)
    with pytest.raises(ArcherError):
        ArcherParams(alpha=-1.0, beta=0.1, gamma=1.0, unit="cm")
    with pytest.raises(ArcherError):
        ArcherParams(alpha=1.0, beta=0.1, gamma=0.0, unit="cm")


def test_equilibrium_layers():
    """Asymptotic HVL and TVL follow from alpha alone."""
    assert equilibrium_hvl(TG108_LEAD) == pytest.approx(math.log(2) / 1.543)
    assert equilibrium_tvl(TG108_LEAD) == pytest.approx(math.log(10) / 1.543)


def test_ncrp147_lead_100kvp_matches_published_hvl():
    """NCRP 147 lists ~0.27 mm as the lead HVL for 100 kVp primary radiation."""
    from radshield.physics.ncrp147 import tables

    params = tables.primary_archer_by_kvp(100, "lead")
    assert params.unit == "mm"
    assert equilibrium_hvl(params) == pytest.approx(0.277, abs=0.01)
    # Spot checks along the published transmission curve.
    assert transmission(params, 0.25) == pytest.approx(0.11, rel=0.15)
    assert transmission(params, 1.0) == pytest.approx(0.0074, rel=0.2)


def test_units_differ_between_methodologies():
    """TG-108 fits are per cm, NCRP 147 fits per mm; mixing them is a 10x error."""
    from radshield.physics.ncrp147 import tables

    assert nuclides.get_archer("F-18", "lead").unit == "cm"
    assert tables.primary_archer_by_kvp(100, "lead").unit == "mm"
