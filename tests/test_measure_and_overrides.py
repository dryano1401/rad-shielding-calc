"""Wall measurement and source-to-point distance override behaviour."""

from __future__ import annotations

import pytest

from radshield.engine.evaluate import describe_distances, evaluate_point
from radshield.model.geometry import (
    GeometryError,
    distance,
    format_length,
    measure,
    measurement_length,
)
from radshield.model.project import Measurement, PointOfInterest, Project
from radshield.model.store import load, save

from .test_geometry_and_engine import build_project, uptake_source


def test_measure_converts_pdf_units_to_metres():
    """1 PDF unit = 0.1 m on the test project, so 100 units is 10 m."""
    project = build_project()
    floor = project.floor("fl1")
    assert measure(floor, (0, 0), (100, 0)) == pytest.approx(10.0)
    assert measure(floor, (0, 0), (30, 40)) == pytest.approx(5.0)


def test_measure_requires_calibration():
    project = build_project()
    project.floor("fl1").calibration = None
    with pytest.raises(GeometryError, match="calibration"):
        measure(project.floor("fl1"), (0, 0), (10, 0))


def test_measurement_length_uses_the_owning_floor_scale():
    """Floors may be plotted at different scales, so each converts its own."""
    project = build_project()
    project.floor("fl2").calibration.known_distance = 20  # 1 unit = 0.2 m here
    item = Measurement(id="m1", p1=(0, 0), p2=(50, 0))
    assert measurement_length(project.floor("fl1"), item) == pytest.approx(5.0)
    assert measurement_length(project.floor("fl2"), item) == pytest.approx(10.0)


def test_format_length_in_feet_and_metres():
    assert format_length(3.048, "ft") == "10' 0.0\" (3.05 m)"
    assert format_length(1.5, "m") == "1.50 m"
    # 1.5 m is 4 ft 11.1 in; the metric value is shown alongside.
    assert format_length(1.5, "ft") == "4' 11.1\" (1.50 m)"


def test_feet_and_inches_roll_over_rather_than_printing_twelve_inches():
    """A hair under 5 ft must read 5' 0.0\", never 4' 12.0\"."""
    assert format_length(5 * 0.3048 - 0.001, "ft") == "5' 0.0\" (1.52 m)"


def test_measurements_survive_a_save_and_reload(tmp_path):
    project = build_project()
    project.floor("fl1").measurements.append(
        Measurement(id="m1", p1=(0, 0), p2=(100, 0), label="Wall to scanner")
    )
    path = save(project, tmp_path / "p.rsproj", {"plan.pdf": b"%PDF-1.4 fake"})
    reloaded, _ = load(path)
    stored = reloaded.floor("fl1").measurements[0]
    assert stored.label == "Wall to scanner"
    assert measurement_length(reloaded.floor("fl1"), stored) == pytest.approx(10.0)


def test_override_replaces_the_geometric_distance():
    project = build_project()
    source = uptake_source()
    poi = PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, auto_height=True)

    geometric = distance(project, source, poi)
    assert geometric.metres == pytest.approx(3.8)
    assert not geometric.is_overridden

    overridden = distance(project, source, poi, override_m=4.0)
    assert overridden.metres == 4.0
    assert overridden.geometric_m == pytest.approx(3.8)
    assert overridden.is_overridden
    assert any("entered manually" in note for note in overridden.notes)


def test_large_override_discrepancy_warns():
    project = build_project()
    source = uptake_source()
    poi = PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, auto_height=True)
    result = distance(project, source, poi, override_m=12.0)
    assert any("differs from the drawing geometry" in w for w in result.warnings)
    # A small correction is routine and must not warn.
    assert not distance(project, source, poi, override_m=3.9).warnings


def test_override_must_be_positive():
    project = build_project()
    poi = PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0)
    with pytest.raises(GeometryError, match="must be positive"):
        distance(project, uptake_source(), poi, override_m=0)


def test_override_rescues_coincident_points():
    """Two points at the same place normally fail; an entered distance fixes it."""
    project = build_project()
    source = uptake_source(floor_id="fl1", x=10, y=10)
    poi = PointOfInterest(id="poi1", floor_id="fl1", x=10, y=10, auto_height=False,
                          height_above_floor_m=1.0)
    with pytest.raises(GeometryError, match="coincident"):
        distance(project, source, poi)
    assert distance(project, source, poi, override_m=2.5).metres == 2.5


def test_standoff_is_not_stacked_on_top_of_an_override():
    """An entered distance is the final figure, not a base to add 0.3 m to."""
    project = build_project()
    source = uptake_source()
    poi = PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, auto_height=True)
    result = distance(project, source, poi, apply_ncrp_standoff=True, override_m=5.0)
    assert result.metres == 5.0
    assert not any("standoff" in note for note in result.notes)


def test_override_changes_the_calculated_dose():
    """The override must reach the physics, not just the display."""
    project = build_project()
    project.sources.append(uptake_source())
    poi = PointOfInterest(
        id="poi1", floor_id="fl2", x=0, y=0, auto_height=True, linked_source_ids=["src1"]
    )
    project.pois.append(poi)

    at_geometry = evaluate_point(project, poi).methods[0].total
    poi.distance_overrides = {"src1": 7.6}  # double the distance
    at_override = evaluate_point(project, poi).methods[0].total

    # Inverse square: doubling the distance quarters the dose.
    assert at_override == pytest.approx(at_geometry / 4.0, rel=0.01)
    contribution = evaluate_point(project, poi).contributions[0]
    assert contribution.distance_m == 7.6
    assert contribution.geometric_distance_m == pytest.approx(3.8)


def test_describe_distances_reports_components_and_overrides():
    project = build_project()
    project.sources.append(uptake_source())
    project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl2", x=40, y=0, label="Office",
                        auto_height=True, linked_source_ids=["src1"],
                        distance_overrides={"src1": 6.0})
    )
    report = describe_distances(project)
    link = report[0]["links"][0]
    assert link["distance_m"] == 6.0
    assert link["geometric_m"] == pytest.approx((4.0**2 + 3.8**2) ** 0.5)
    assert link["vertical_m"] == pytest.approx(3.8)
    assert link["horizontal_m"] == pytest.approx(4.0)
    assert "m" in link["display"]
    assert link["label"] == "Uptake room"


def test_describe_distances_reports_per_link_errors_without_aborting():
    """One broken link must not hide the distances of the others."""
    project = build_project()
    project.sources.append(uptake_source())
    project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, auto_height=True,
                        linked_source_ids=["src1", "ghost"])
    )
    links = describe_distances(project)[0]["links"]
    assert links[0]["distance_m"] == pytest.approx(3.8)
    assert links[1]["error"] == "source no longer exists"


def test_overrides_are_dropped_when_a_source_is_removed():
    project = build_project()
    project.sources.append(uptake_source())
    project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0,
                        linked_source_ids=["src1"], distance_overrides={"src1": 5.0})
    )
    project.remove_source("src1")
    assert project.poi("poi1").distance_overrides == {}


def test_overrides_are_dropped_when_a_floor_is_removed():
    project = build_project()
    project.sources.append(uptake_source())
    project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0,
                        linked_source_ids=["src1"], distance_overrides={"src1": 5.0})
    )
    project.remove_floor("fl1")
    assert project.poi("poi1").distance_overrides == {}


def test_overrides_survive_a_save_and_reload(tmp_path):
    project = build_project()
    project.sources.append(uptake_source())
    project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0,
                        linked_source_ids=["src1"], distance_overrides={"src1": 4.25})
    )
    project.display_unit = "m"
    path = save(project, tmp_path / "p.rsproj", {"plan.pdf": b"%PDF-1.4 fake"})
    reloaded, _ = load(path)
    assert reloaded.poi("poi1").distance_overrides == {"src1": 4.25}
    assert reloaded.display_unit == "m"


def test_old_projects_without_the_new_fields_still_load():
    """Projects saved before measurements and overrides existed must open."""
    project = build_project()
    data = project.to_dict()
    for floor in data["floors"]:
        floor.pop("measurements")
    data.pop("display_unit")
    restored = Project.from_dict(data)
    assert restored.floor("fl1").measurements == []
    assert restored.display_unit == "ft"
