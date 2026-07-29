"""Manufacturer scatter charts: parsing, bearing lookup and inverse-square scaling.

The fixture is a real vendor plan view, transcribed from the printed chart,
including the region masked by the gantry outline and the pedestal column that
reads an order of magnitude below its neighbours.
"""

from __future__ import annotations

import pytest

from radshield.engine.evaluate import evaluate_point
from radshield.model.geometry import chart_direction
from radshield.model.project import (
    Calibration,
    Floor,
    PointOfInterest,
    Project,
    ScatterMapData,
    SourcePoint,
)
from radshield.model.store import load, save
from radshield.physics import isodose

N = None
IN = 0.0254   # the charts are dimensioned in inches; 19.7 in is 0.50038 m, not 0.5
PLAN_X = [-78.7, -59.1, -39.4, -19.7, 0, 19.7, 39.4, 59.1, 78.7]
PLAN_Y = [-118.1, -98.4, -78.7, -59.1, -39.4, -19.7, 0,
          19.7, 39.4, 59.1, 78.7, 98.4, 118.1, 137.8, 157.5, 177.1]
PLAN_VALUES = [
    [0.001, 0.003, 0.010, 0.020, 0.021, 0.020, 0.010, 0.003, 0.001],
    [0.001, 0.002, 0.009, 0.029, 0.030, 0.028, 0.008, 0.002, 0.001],
    [0.001, 0.001, 0.005, 0.041, 0.049, 0.041, 0.005, 0.001, 0.001],
    [0.001, 0.001, N, N, N, N, N, 0.001, 0.001],
    [0.001, 0.001, N, N, N, N, N, 0.001, 0.001],
    [0.001, 0.002, N, N, N, N, N, 0.001, 0.001],
    [0.004, 0.005, N, N, N, N, N, 0.002, 0.002],
    [0.008, 0.018, 0.066, 0.370, 0.757, 0.381, 0.065, 0.013, 0.006],
    [0.021, 0.053, 0.096, 0.149, 0.195, 0.157, 0.098, 0.053, 0.017],
    [0.032, 0.047, 0.062, 0.076, 0.087, 0.078, 0.060, 0.045, 0.032],
    [0.028, 0.034, 0.040, 0.045, 0.047, 0.045, 0.040, 0.033, 0.027],
    [0.023, 0.025, 0.028, 0.029, 0.031, 0.029, 0.028, 0.024, 0.021],
    [0.017, 0.019, 0.020, 0.020, N, 0.020, 0.020, 0.018, 0.016],
    [0.013, 0.014, 0.015, 0.015, 0.002, 0.014, 0.014, 0.014, 0.013],
    [0.010, 0.011, 0.011, 0.011, 0.002, 0.011, 0.011, 0.011, 0.010],
    [0.008, 0.009, 0.009, 0.009, 0.001, 0.009, 0.009, 0.009, 0.008],
]


def plan_map() -> isodose.ScatterMap:
    return isodose.build_map(
        "Vendor plan view", "plan", PLAN_X, PLAN_Y, PLAN_VALUES,
        coordinate_unit="in", value_unit="mGy", per="procedure",
        source="manufacturer scatter chart",
    )


# --- parsing -----------------------------------------------------------------

def test_parse_tab_separated_grid_with_na_cells():
    text = (
        "\t-19.7\t0\t19.7\n"
        "19.7\t0.370\t0.757\t0.381\n"
        "39.4\t0.149\tNA\t0.157\n"
    )
    x, y, values = isodose.parse_grid(text)
    assert x == [-19.7, 0, 19.7]
    assert y == [19.7, 39.4]
    assert values[0] == [0.370, 0.757, 0.381]
    assert values[1][1] is None


def test_parse_accepts_commas_and_spaces():
    for text in ("' '\n-19.7,0\n19.7,0.37,0.76\n".replace("' '", ""),
                 "  -19.7 0\n19.7 0.37 0.76\n"):
        x, y, values = isodose.parse_grid(text)
        assert x == [-19.7, 0]
        assert values[0] == [0.37, 0.76]


def test_parse_rejects_unusable_input():
    with pytest.raises(isodose.IsodoseError, match="header row"):
        isodose.parse_grid("only one line")
    with pytest.raises(isodose.IsodoseError, match="header row of column offsets"):
        isodose.parse_grid("alpha\tbeta\n1\t2\n")


def test_build_map_converts_units_and_drops_blank_cells():
    scatter_map = plan_map()
    # 9 x 16 grid less the 20 masked cells and the two pedestal zeros kept as values.
    assert len(scatter_map.cells) == 123
    low, high = scatter_map.radius_range_m
    assert low == pytest.approx(0.5, abs=0.01)      # 19.7 in
    assert high == pytest.approx(4.92, abs=0.02)    # corner of the grid


def test_build_map_rejects_a_ragged_grid():
    with pytest.raises(isodose.IsodoseError, match="values but there are"):
        isodose.build_map("bad", "plan", [0, 1], [0], [[1.0, 2.0, 3.0]])
    with pytest.raises(isodose.IsodoseError, match="value rows but"):
        isodose.build_map("bad", "plan", [0], [0, 1], [[1.0]])


def test_build_map_rejects_an_empty_chart():
    with pytest.raises(isodose.IsodoseError, match="no usable cells"):
        isodose.build_map("empty", "plan", [0, 1], [0], [[None, 0.0]])


# --- the physical premise ----------------------------------------------------

def test_kerma_times_radius_squared_is_constant_along_a_bearing():
    """The premise of the whole method, checked against the vendor's own numbers.

    Along the table axis the chart gives 0.757 mGy at 0.5 m and 0.031 mGy at
    2.5 m.  If scatter falls as 1/d^2 then K*r^2 is constant; on this chart it
    holds to within 3% over a five-fold change in distance.
    """
    scatter_map = plan_map()
    on_axis = sorted(
        (c for c in scatter_map.cells if abs(c.x_m) < 1e-9 and 0 < c.y_m <= 2.55),
        key=lambda c: c.y_m,
    )
    strengths = [c.strength for c in on_axis]
    assert len(strengths) == 5
    assert max(strengths) / min(strengths) < 1.05
    assert strengths[0] == pytest.approx(0.757 * (19.7 * IN) ** 2, rel=1e-9)


# --- sampling ----------------------------------------------------------------

def test_sampling_at_the_strongest_cell_returns_its_chart_value():
    """At the radius of the governing cell, its published value comes straight back."""
    scatter_map = plan_map()
    reading = isodose.sample(scatter_map, 90.0, 59.1 * IN)
    assert reading.value_mGy == pytest.approx(0.087, rel=1e-9)
    assert reading.cell.radius_m == pytest.approx(59.1 * IN)


def test_envelope_slightly_over_reads_the_weaker_cells_on_a_bearing():
    """The cost of taking the envelope, stated rather than hidden.

    At 39.4 in the chart says 0.195 mGy, but the governing cell on that
    bearing is the one at 59.1 in, so the reading comes back marginally
    higher. On this chart the difference is under 1%.
    """
    scatter_map = plan_map()
    reading = isodose.sample(scatter_map, 90.0, 39.4 * IN)
    assert reading.value_mGy > 0.195
    assert reading.value_mGy / 0.195 < 1.01


def test_inverse_square_scaling_between_chart_points():
    scatter_map = plan_map()
    near = isodose.sample(scatter_map, 90.0, 2.0)
    far = isodose.sample(scatter_map, 90.0, 4.0)
    assert far.value_mGy == pytest.approx(near.value_mGy / 4.0, rel=1e-9)


def test_distance_sweep_is_smooth_despite_the_pedestal_shadow():
    """The pedestal column reads ~10x low; the value must not jump as d crosses it.

    Selecting the cell nearest in radius would hand back the shadowed 0.002
    value beyond 3 m, dropping the answer tenfold for a few centimetres of
    extra distance, and in the unsafe direction.
    """
    scatter_map = plan_map()
    values = [isodose.sample(scatter_map, 90.0, d).value_mGy
              for d in (2.4, 2.6, 3.0, 3.4, 3.6, 4.0)]
    for previous, current in zip(values, values[1:]):
        assert current < previous                       # falls monotonically
        assert previous / current < 1.6                 # no cliff


def test_shadowed_bearing_is_reported():
    scatter_map = plan_map()
    reading = isodose.sample(scatter_map, 90.0, 3.0)
    assert any("disagree by a factor" in note for note in reading.notes)


def test_bearing_selects_the_right_side_of_the_chart():
    """Scatter is far stronger down the table than out through the gantry bore."""
    scatter_map = plan_map()
    down_table = isodose.sample(scatter_map, 90.0, 4.0).value_mGy
    through_bore = isodose.sample(scatter_map, 0.0, 4.0).value_mGy
    assert down_table > 10 * through_bore


def test_large_extrapolation_and_off_bearing_reads_are_flagged():
    scatter_map = plan_map()
    assert any("scaled by a factor" in n for n in isodose.sample(scatter_map, 90.0, 30.0).notes)
    assert isodose.sample(scatter_map, 90.0, 4.0).angle_error_deg == pytest.approx(0.0)


def test_sample_rejects_a_zero_distance():
    with pytest.raises(isodose.IsodoseError, match="distance must be positive"):
        isodose.sample(plan_map(), 0.0, 0.0)


# --- orientation on the plan -------------------------------------------------

def chart_project() -> Project:
    """One calibrated floor at 1 PDF unit = 0.1 m, with a chart-driven CT."""
    project = Project(materials=["lead"])
    project.floors.append(
        Floor(id="f1", name="L1", pdf_name="a.pdf", elevation_m=0.0,
              calibration=Calibration(p1=(0, 0), p2=(100, 0), known_distance=10, unit="m"),
              alignment=(0.0, 0.0))
    )
    project.floors.append(
        Floor(id="f2", name="L2", pdf_name="b.pdf", elevation_m=4.0,
              calibration=Calibration(p1=(0, 0), p2=(100, 0), known_distance=10, unit="m"),
              alignment=(0.0, 0.0))
    )
    project.scatter_maps.append(
        ScatterMapData(id="m1", name="Vendor plan view", plane="plan",
                       coordinate_unit="in", value_unit="mGy", per="procedure",
                       source="manufacturer chart",
                       x_coords=PLAN_X, y_coords=PLAN_Y, values=PLAN_VALUES)
    )
    project.sources.append(
        SourcePoint(id="ct1", floor_id="f1", x=0, y=0, label="CT", method="ncrp147_ct",
                    height_above_floor_m=1.0, rotation_deg=0.0,
                    params={"scatter_method": "chart", "plan_map_id": "m1",
                            "procedures_per_week": 100, "kvp": 125,
                            "scatter_source": "manufacturer chart"})
    )
    return project


def north_point(project: Project, metres: float = 4.0) -> PointOfInterest:
    """A point due north of the isocentre. PDF y grows downward, so north is -y."""
    poi = PointOfInterest(
        id="p1", floor_id="f1", x=0, y=-metres * 10, label="Office",
        auto_height=False, height_above_floor_m=1.0, occupancy=1.0,
        offset_applied=True, linked_source_ids=["ct1"],
    )
    project.pois.append(poi)
    return poi


def test_rotation_turns_the_chart_on_the_plan():
    project = chart_project()
    poi = north_point(project)
    source = project.source("ct1")
    bearings = {}
    for rotation in (0, 90, 180, 270):
        source.rotation_deg = rotation
        bearings[rotation] = chart_direction(project, source, poi).bearing_deg
    assert bearings[0] == pytest.approx(90.0)
    assert bearings[90] == pytest.approx(0.0)
    assert bearings[180] == pytest.approx(-90.0)


def test_rotating_the_scanner_changes_the_dose_at_a_fixed_point():
    """Turning the scanner so the table points at the wall raises the dose there."""
    project = chart_project()
    poi = north_point(project)
    source = project.source("ct1")

    source.rotation_deg = 0.0                     # table axis points north, at the point
    facing = evaluate_point(project, poi).methods[0].total
    source.rotation_deg = 90.0                    # bore points north instead
    side_on = evaluate_point(project, poi).methods[0].total

    assert facing > 10 * side_on


def test_chart_result_matches_a_hand_calculation():
    """0.087 mGy at 59.1 in, scaled to 4 m, times 100 procedures a week."""
    project = chart_project()
    poi = north_point(project, metres=4.0)
    result = evaluate_point(project, poi)

    strength = 0.087 * (59.1 * IN) ** 2   # governing cell on the table-axis bearing
    expected = strength / 4.0**2 * 100
    assert result.methods[0].total == pytest.approx(expected, rel=1e-9)
    assert result.contributions[0].terms["chart kerma at the point (mGy per procedure)"] == (
        pytest.approx(strength / 16.0, rel=1e-9)
    )


def test_audit_trail_names_the_chart_and_the_cell_used():
    project = chart_project()
    poi = north_point(project)
    notes = evaluate_point(project, poi).contributions[0].notes
    assert any("Vendor plan view" in n for n in notes)
    assert any("chart cell at" in n and "scaled by" in n for n in notes)


def test_missing_chart_is_reported_not_crashed():
    project = chart_project()
    project.source("ct1").params["plan_map_id"] = ""
    poi = north_point(project)
    result = evaluate_point(project, poi)
    assert result.errors and "no chart is assigned" in result.errors[0]


def test_plan_chart_used_across_floors_says_so():
    project = chart_project()
    poi = PointOfInterest(
        id="p1", floor_id="f2", x=0, y=-40, auto_height=True, occupancy=1.0,
        offset_applied=True, linked_source_ids=["ct1"],
    )
    project.pois.append(poi)
    notes = evaluate_point(project, poi).contributions[0].notes
    assert any("no elevation chart assigned" in n for n in notes)


def test_elevation_chart_is_preferred_across_floors():
    project = chart_project()
    project.scatter_maps.append(
        ScatterMapData(id="m2", name="Vendor elevation view", plane="elevation",
                       coordinate_unit="in", value_unit="mGy",
                       x_coords=[-59.1, 0, 59.1, 118.1],
                       y_coords=[39.4, 19.7, 0, -19.7],
                       values=[[0.013, 0.034, 0.041, 0.021],
                               [0.041, 0.361, 0.047, 0.021],
                               [0.049, 0.757, 0.047, 0.002],
                               [0.039, 0.306, 0.018, 0.001]])
    )
    project.source("ct1").params["elevation_map_id"] = "m2"
    poi = PointOfInterest(
        id="p1", floor_id="f2", x=0, y=-40, auto_height=True, occupancy=1.0,
        offset_applied=True, linked_source_ids=["ct1"],
    )
    project.pois.append(poi)
    notes = evaluate_point(project, poi).contributions[0].notes
    assert any("elevation view" in n for n in notes)
    assert not any("no elevation chart" in n for n in notes)


def test_charts_survive_a_save_and_reload(tmp_path):
    project = chart_project()
    project.source("ct1").rotation_deg = 37.5
    path = save(project, tmp_path / "p.rsproj",
                {"a.pdf": b"%PDF-1.4 fake", "b.pdf": b"%PDF-1.4 fake"})
    reloaded, _ = load(path)
    stored = reloaded.scatter_map("m1")
    assert stored.name == "Vendor plan view"
    assert stored.values[7][4] == 0.757
    assert stored.values[3][2] is None
    assert reloaded.source("ct1").rotation_deg == 37.5


def test_chart_is_read_at_the_distance_the_rest_of_the_calculation_uses():
    """The standoff and any override must reach the inverse-square correction.

    Sampling at the raw point-to-point distance while reporting the adjusted
    one would leave the audit trail quoting a distance the arithmetic never
    used.
    """
    project = chart_project()
    poi = north_point(project, metres=4.0)

    poi.offset_applied = True
    plain = evaluate_point(project, poi)
    assert plain.contributions[0].distance_m == pytest.approx(4.0)

    # The placed point marks the barrier, so 0.3 m is added.
    poi.offset_applied = False
    with_standoff = evaluate_point(project, poi)
    assert with_standoff.contributions[0].distance_m == pytest.approx(4.3)
    assert with_standoff.methods[0].total == pytest.approx(
        plain.methods[0].total * (4.0 / 4.3) ** 2, rel=1e-9
    )

    # An entered distance must move the chart reading too.
    poi.offset_applied = True
    poi.distance_overrides = {"ct1": 8.0}
    overridden = evaluate_point(project, poi)
    assert overridden.methods[0].total == pytest.approx(
        plain.methods[0].total / 4.0, rel=1e-9
    )
    assert any("chart read at 8.00 m" in n for n in overridden.contributions[0].notes)
