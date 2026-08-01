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


def test_parse_accepts_a_true_minus_sign_and_an_em_dash_mask():
    """Pasted from a PDF or typed by hand, negative offsets sometimes carry
    a real minus sign (U+2212) rather than a hyphen, and masked cells an em
    dash rather than "NA" -- both showed up in a real vendor chart."""
    text = (
        "\t−78.7\t0\t78.7\n"
        "−19.7\t0.021\t—\t0.021\n"
        "19.7\t0.008\t0.757\t0.006\n"
    )
    x, y, values = isodose.parse_grid(text)
    assert x == [-78.7, 0, 78.7]
    assert y == [-19.7, 19.7]
    assert values[0][1] is None
    assert values[1] == [0.008, 0.757, 0.006]


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


def test_flip_mirrors_the_axis_without_touching_the_values():
    """A vendor's own left/right or front/back convention doesn't always
    match the source's rotation arrow; flip_x/flip_y correct for that by
    swapping which side of the isocentre a cell's offset sits on, not what
    it reads."""
    plain = plan_map()
    flipped = isodose.build_map(
        "Vendor plan view", "plan", PLAN_X, PLAN_Y, PLAN_VALUES,
        coordinate_unit="in", value_unit="mGy", per="procedure", flip_x=True, flip_y=True,
    )
    by_offset = {(round(c.x_m, 4), round(c.y_m, 4)): c.value_mGy for c in plain.cells}
    for cell in flipped.cells:
        assert cell.value_mGy == by_offset[(round(-cell.x_m, 4), round(-cell.y_m, 4))]
    assert flipped.extent == (-plain.extent[1], -plain.extent[0], -plain.extent[3], -plain.extent[2])


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


# --- reading the chart --------------------------------------------------------

def test_reading_a_grid_point_returns_its_published_value():
    """The chart is laid over the plan, so at a printed cell it reads exactly."""
    scatter_map = plan_map()
    reading = isodose.sample_at(scatter_map, 0.0, 59.1 * IN)
    assert reading.value_mGy == pytest.approx(0.087, rel=1e-9)
    assert reading.method == "interpolated"


def test_no_distance_correction_is_applied_inside_the_chart():
    """The decisive case: the chart's own value stands, even where it defies 1/d^2.

    At 4 m along the table axis the chart reads 0.002 mGy because the pedestal
    shadows that spot. Projecting the strongest cell on the same bearing by
    inverse square would give about 0.012 -- six times higher. Inside the
    chart the published value wins, because the chart already accounts for the
    distance to that point.
    """
    scatter_map = plan_map()
    reading = isodose.sample_at(scatter_map, 0.0, 157.5 * IN)
    assert reading.value_mGy == pytest.approx(0.002, rel=1e-9)
    assert not reading.is_extrapolated

    projected = isodose.extrapolate(scatter_map, 90.0, 157.5 * IN)
    assert projected.value_mGy > 5 * reading.value_mGy


def test_interpolation_between_printed_cells():
    """Halfway between two cells on a row, the value is halfway between them."""
    scatter_map = plan_map()
    midpoint = (19.7 + 39.4) / 2 * IN
    reading = isodose.sample_at(scatter_map, 0.0, midpoint)
    assert reading.method == "interpolated"
    assert reading.value_mGy == pytest.approx((0.757 + 0.195) / 2, rel=1e-9)


def test_masked_area_falls_back_to_the_nearest_cell():
    """Inside the gantry footprint there are no four corners to interpolate."""
    scatter_map = plan_map()
    reading = isodose.sample_at(scatter_map, 0.0, -39.4 * IN)
    assert reading.method == "nearest"
    assert any("masked" in note for note in reading.notes)


def test_beyond_the_chart_the_value_is_projected_by_inverse_square():
    scatter_map = plan_map()
    inside = isodose.sample_at(scatter_map, 0.0, 59.1 * IN)
    outside = isodose.sample_at(scatter_map, 0.0, 8.0)
    assert not inside.is_extrapolated
    assert outside.is_extrapolated
    # 1/d^2 from the strongest cell on the bearing.
    assert outside.value_mGy == pytest.approx(outside.strength / 64.0, rel=1e-9)
    assert "beyond the chart" in outside.describe()


def test_projection_falls_as_inverse_square_once_off_the_chart():
    scatter_map = plan_map()
    near = isodose.sample_at(scatter_map, 0.0, 6.0)
    far = isodose.sample_at(scatter_map, 0.0, 12.0)
    assert far.value_mGy == pytest.approx(near.value_mGy / 4.0, rel=1e-9)


def test_projection_uses_the_envelope_not_the_shadowed_cell():
    """Off-chart projection must not inherit the pedestal shadow."""
    scatter_map = plan_map()
    reading = isodose.extrapolate(scatter_map, 90.0, 8.0)
    assert any("disagree by a factor" in note for note in reading.notes)
    assert reading.strength == pytest.approx(0.087 * (59.1 * IN) ** 2, rel=1e-9)


def test_bearing_selects_the_right_side_of_the_chart():
    """Scatter is far stronger down the table than out through the gantry bore."""
    scatter_map = plan_map()
    down_table = isodose.sample_at(scatter_map, 0.0, 59.1 * IN).value_mGy
    through_bore = isodose.sample_at(scatter_map, 59.1 * IN, 0.0).value_mGy
    assert down_table > 40 * through_bore


def test_far_projection_is_flagged():
    scatter_map = plan_map()
    assert any("projected" in n for n in isodose.sample_at(scatter_map, 0.0, 30.0).notes)


def test_reading_at_the_isocentre_is_rejected():
    with pytest.raises(isodose.IsodoseError, match="at the isocentre"):
        isodose.sample_at(plan_map(), 0.0, 0.0)
    with pytest.raises(isodose.IsodoseError, match="distance must be positive"):
        isodose.extrapolate(plan_map(), 0.0, 0.0)


# --- workload basis ----------------------------------------------------------

def test_weekly_multiplier_by_chart_basis():
    """Charts quoted per mAs scale by workload, not by procedure count."""
    assert isodose.weekly_multiplier("procedure", 100, 5000) == 100
    assert isodose.weekly_multiplier("scan", 100, 5000) == 100
    assert isodose.weekly_multiplier("mAs", 100, 5000) == 5000
    assert isodose.weekly_multiplier("100 mAs", 100, 5000) == 50
    with pytest.raises(isodose.IsodoseError, match="unknown chart basis"):
        isodose.weekly_multiplier("per banana", 1, 1)


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
    poi = north_point(project, metres=59.1 * IN)
    poi.offset_applied = True
    source = project.source("ct1")

    source.rotation_deg = 0.0                     # table axis points north, at the point
    facing = evaluate_point(project, poi).methods[0].total
    source.rotation_deg = 90.0                    # bore points north instead
    side_on = evaluate_point(project, poi).methods[0].total

    # The chart reads 0.087 down the table and 0.002 out through the bore.
    assert facing == pytest.approx(0.087 * 100, rel=1e-9)
    assert facing > 40 * side_on


def test_chart_result_matches_a_hand_calculation():
    """The chart reads 0.087 mGy at that spot; times 100 procedures a week."""
    project = chart_project()
    poi = north_point(project, metres=59.1 * IN)
    poi.offset_applied = True
    result = evaluate_point(project, poi)

    assert result.methods[0].total == pytest.approx(0.087 * 100, rel=1e-9)
    assert result.contributions[0].terms["chart kerma at the point (mGy per procedure)"] == (
        pytest.approx(0.087, rel=1e-9)
    )
    assert any("no inverse-square correction" in n for n in result.contributions[0].notes)


def test_audit_trail_names_the_chart_and_says_where_it_was_read():
    project = chart_project()
    poi = north_point(project, metres=59.1 * IN)
    poi.offset_applied = True
    notes = evaluate_point(project, poi).contributions[0].notes
    assert any("Vendor plan view" in n and "plan view" in n for n in notes)
    assert any("chart read at" in n and "from the isocentre" in n for n in notes)


def test_audit_trail_shows_the_projection_when_the_chart_is_left_behind():
    project = chart_project()
    poi = north_point(project, metres=9.0)
    poi.offset_applied = True
    notes = evaluate_point(project, poi).contributions[0].notes
    assert any("beyond the chart" in n and "scaled by" in n for n in notes)


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
    assert stored.flip_x is False and stored.flip_y is False


def test_charts_saved_before_flip_existed_still_load():
    project = chart_project()
    data = project.to_dict()
    for scatter_map in data["scatter_maps"]:
        scatter_map.pop("flip_x")
        scatter_map.pop("flip_y")
    restored = Project.from_dict(data)
    assert restored.scatter_map("m1").flip_x is False
    assert restored.scatter_map("m1").flip_y is False


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

    # The placed point marks the barrier, so 0.3 m is added and the chart is
    # read 0.3 m further out rather than at the placed spot.
    poi.offset_applied = False
    with_standoff = evaluate_point(project, poi)
    assert with_standoff.contributions[0].distance_m == pytest.approx(4.3)
    assert any("chart read at 4.30 m" in n for n in with_standoff.contributions[0].notes)

    # An entered distance moves the read position out past the chart, where
    # inverse square legitimately takes over.
    poi.offset_applied = True
    poi.distance_overrides = {"ct1": 8.0}
    overridden = evaluate_point(project, poi)
    notes = overridden.contributions[0].notes
    assert any("chart read at 8.00 m" in n for n in notes)
    assert any("beyond the chart" in n for n in notes)
