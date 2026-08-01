"""End-to-end tests of the web API, from PDF upload through to CSV export."""

from __future__ import annotations

import csv
import io

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
pytest.importorskip("fitz", reason="PyMuPDF is needed to render PDFs")

from radshield.web import app as web_app  # noqa: E402
from radshield.web.render import PdfError, page_info, render_page  # noqa: E402


def make_pdf(width: float = 612, height: float = 792, pages: int = 1) -> bytes:
    """Build a small multi-page PDF standing in for a floor plan."""
    import fitz

    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page(width=width, height=height)
        page.draw_rect(fitz.Rect(72, 72, width - 72, height - 72))
        page.insert_text((90, 110), f"Plan page {index}")
    return doc.tobytes()


@pytest.fixture
def client():
    """A test client over a freshly reset session."""
    web_app.session.project = web_app.Project()
    web_app.session.pdfs = {}
    web_app.session.path = None
    with fastapi_testclient.TestClient(web_app.app) as test_client:
        yield test_client


def add_floor(client, name: str, elevation: float, pages: int = 1) -> dict:
    """Upload a PDF as a floor and return the resulting project payload."""
    response = client.post(
        "/api/floors",
        files={"file": (f"{name}.pdf", make_pdf(pages=pages), "application/pdf")},
        params={"name": name, "elevation_m": elevation},
    )
    assert response.status_code == 200, response.text
    return response.json()


def calibrate(client, floor_id: str, metres_per_unit: float = 0.1) -> dict:
    """Calibrate a floor so that one PDF unit equals ``metres_per_unit`` metres."""
    response = client.patch(
        f"/api/floors/{floor_id}",
        json={
            "calibration": {
                "p1": [0, 0],
                "p2": [100, 0],
                "known_distance": 100 * metres_per_unit,
                "unit": "m",
            }
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_page_info_and_render():
    pdf = make_pdf(pages=3)
    info = page_info(pdf, 0)
    assert info.page_count == 3
    assert info.width == pytest.approx(612)
    png = render_page(pdf, 1, zoom=2.0)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_rejects_bad_page_and_zoom():
    pdf = make_pdf()
    with pytest.raises(PdfError, match="out of range"):
        render_page(pdf, 5)
    with pytest.raises(PdfError, match="zoom"):
        render_page(pdf, 0, zoom=99)


def test_index_and_options(client):
    assert client.get("/").status_code == 200
    options = client.get("/api/options").json()
    assert "F-18" in options["nuclides"]
    assert any("Rad Room" in w for w in options["workloads"])
    assert options["occupancy"] and options["known_gaps"]


def test_upload_floor_records_page_geometry(client):
    project = add_floor(client, "Level 1", 0.0)
    assert len(project["floors"]) == 1
    floor = project["floors"][0]
    assert floor["page_width"] == pytest.approx(612)
    assert floor["scale_description"] == "not calibrated"
    assert "not calibrated" in " ".join(project["problems"])


def test_rejects_non_pdf_upload(client):
    response = client.post(
        "/api/floors", files={"file": ("notes.txt", b"just text", "text/plain")}
    )
    assert response.status_code == 400


def test_floor_image_endpoint_returns_png(client):
    project = add_floor(client, "Level 1", 0.0)
    response = client.get(f"/api/floors/{project['floors'][0]['id']}/image", params={"zoom": 2})
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_calibration_round_trip_and_description(client):
    project = add_floor(client, "Level 1", 0.0)
    floor_id = project["floors"][0]["id"]
    project = client.patch(
        f"/api/floors/{floor_id}",
        json={"calibration": {"p1": [100, 100], "p2": [500, 100],
                              "known_distance": 40, "unit": "ft"}},
    ).json()
    floor = project["floors"][0]
    assert floor["metres_per_unit"] == pytest.approx(40 * 0.3048 / 400)
    assert "40 ft" in floor["scale_description"]


def test_calibration_with_coincident_points_is_rejected(client):
    project = add_floor(client, "Level 1", 0.0)
    response = client.patch(
        f"/api/floors/{project['floors'][0]['id']}",
        json={"calibration": {"p1": [5, 5], "p2": [5, 5], "known_distance": 3, "unit": "m"}},
    )
    assert response.status_code == 400


def test_page_selection_updates_geometry(client):
    project = add_floor(client, "Level 1", 0.0, pages=3)
    floor_id = project["floors"][0]["id"]
    assert client.patch(f"/api/floors/{floor_id}", json={"page": 2}).json()["floors"][0]["page"] == 2
    assert client.patch(f"/api/floors/{floor_id}", json={"page": 9}).status_code == 400


def test_floors_sort_by_elevation_and_spacing_helper(client):
    add_floor(client, "Level 1", 0.0)
    add_floor(client, "Level 2", 0.0)
    add_floor(client, "Level 3", 0.0)
    project = client.post("/api/floors/spacing", json={"heights": [4.3, 3.5]}).json()
    assert [f["elevation_m"] for f in project["floors"]] == [0.0, 4.3, 7.8]
    assert client.post("/api/floors/spacing", json={"heights": [4.3]}).status_code == 400


def test_full_workflow_produces_results_and_csv(client):
    """Upload two floors, calibrate, align, place points, calculate, export."""
    project = add_floor(client, "Source floor", 0.0)
    project = add_floor(client, "Floor above", 4.3)
    lower, upper = project["floors"][0]["id"], project["floors"][1]["id"]

    for floor_id in (lower, upper):
        calibrate(client, floor_id)
        client.patch(f"/api/floors/{floor_id}", json={"alignment": [0, 0], "alignment2": [100, 0]})

    project = client.post("/api/sources", json={
        "floor_id": lower, "x": 0, "y": 0, "label": "Uptake room",
        "method": "tg108", "height_above_floor_m": 1.0,
        "params": {"kind": "uptake", "nuclide": "F-18",
                   "administered_activity_MBq": 555, "patients_per_week": 40,
                   "uptake_time_h": 1.0},
    }).json()
    source_id = project["sources"][0]["id"]

    project = client.post("/api/pois", json={
        "floor_id": upper, "x": 0, "y": 0, "label": "Office above",
        "occupancy": 1.0, "area_class": "uncontrolled",
        "auto_height": True, "linked_source_ids": [source_id],
    }).json()
    assert not project["problems"]

    payload = client.get("/api/results").json()
    result = payload["results"][0]
    assert not result["errors"]
    method = result["methods"][0]
    # TG-108 Example 4: 117 uSv/week at d = 3.8 m, B = 0.17.
    assert method["total"] == pytest.approx(117.0, rel=0.01)
    assert method["required_transmission"] == pytest.approx(0.17, rel=0.02)
    assert result["contributions"][0]["distance_m"] == pytest.approx(3.8)

    response = client.get("/api/results.csv")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert {"source", "total"} <= {row["row_type"] for row in rows}
    total_row = next(row for row in rows if row["row_type"] == "total")
    assert float(total_row["lead_mm"]) > 0


def test_dragging_a_point_persists_new_coordinates(client):
    project = add_floor(client, "Level 1", 0.0)
    floor_id = project["floors"][0]["id"]
    project = client.post("/api/sources", json={"floor_id": floor_id, "x": 10, "y": 10}).json()
    source_id = project["sources"][0]["id"]
    project = client.patch(f"/api/sources/{source_id}", json={"x": 250.5, "y": 91}).json()
    assert (project["sources"][0]["x"], project["sources"][0]["y"]) == (250.5, 91)


def test_deleting_a_source_unlinks_it_from_points(client):
    project = add_floor(client, "Level 1", 0.0)
    floor_id = project["floors"][0]["id"]
    project = client.post("/api/sources", json={"floor_id": floor_id, "x": 0, "y": 0}).json()
    source_id = project["sources"][0]["id"]
    client.post("/api/pois", json={"floor_id": floor_id, "x": 50, "y": 0,
                                   "linked_source_ids": [source_id]})
    project = client.delete(f"/api/sources/{source_id}").json()
    assert project["sources"] == []
    assert project["pois"][0]["linked_source_ids"] == []


def test_deleting_a_floor_removes_its_contents(client):
    project = add_floor(client, "Level 1", 0.0)
    floor_id = project["floors"][0]["id"]
    client.post("/api/sources", json={"floor_id": floor_id, "x": 0, "y": 0})
    project = client.delete(f"/api/floors/{floor_id}").json()
    assert project["floors"] == [] and project["sources"] == []


def test_save_and_reopen_preserves_everything(client, tmp_path):
    project = add_floor(client, "Level 1", 0.0)
    floor_id = project["floors"][0]["id"]
    calibrate(client, floor_id)
    client.patch(f"/api/floors/{floor_id}", json={"alignment": [12, 34]})
    project = client.post("/api/sources", json={
        "floor_id": floor_id, "x": 20, "y": 30, "label": "Scanner",
        "params": {"kind": "uptake", "nuclide": "F-18",
                   "administered_activity_MBq": 555, "patients_per_week": 40,
                   "uptake_time_h": 1.0},
    }).json()
    client.post("/api/pois", json={"floor_id": floor_id, "x": 90, "y": 30,
                                   "label": "Desk",
                                   "linked_source_ids": [project["sources"][0]["id"]]})
    client.post("/api/project/name", json={"name": "Saved project"})

    path = tmp_path / "demo.rsproj"
    assert client.post("/api/project/save", json={"path": str(path)}).status_code == 200
    assert path.exists()

    client.post("/api/project/new")
    assert client.get("/api/project").json()["floors"] == []

    reopened = client.post("/api/project/load", json={"path": str(path)}).json()
    assert reopened["name"] == "Saved project"
    assert reopened["floors"][0]["alignment"] == [12, 34]
    assert reopened["sources"][0]["label"] == "Scanner"
    assert reopened["pois"][0]["label"] == "Desk"
    # The embedded PDF must still render after a round trip.
    assert client.get(f"/api/floors/{reopened['floors'][0]['id']}/image").status_code == 200


def test_loading_a_missing_project_is_reported(client):
    assert client.post("/api/project/load", json={"path": "/nope/none.rsproj"}).status_code == 404


def test_material_selection_drives_reported_columns(client):
    project = add_floor(client, "Level 1", 0.0)
    floor_id = project["floors"][0]["id"]
    calibrate(client, floor_id)
    project = client.post("/api/sources", json={
        "floor_id": floor_id, "x": 0, "y": 0,
        "params": {"kind": "uptake", "nuclide": "F-18",
                   "administered_activity_MBq": 555, "patients_per_week": 40,
                   "uptake_time_h": 1.0},
    }).json()
    client.post("/api/pois", json={
        "floor_id": floor_id, "x": 40, "y": 0, "auto_height": False,
        "height_above_floor_m": 1.0,
        "linked_source_ids": [project["sources"][0]["id"]],
    })
    client.post("/api/materials", json={"materials": ["lead", "iron"]})
    payload = client.get("/api/results").json()
    assert set(payload["results"][0]["methods"][0]["thickness_mm"]) == {"lead", "iron"}


def test_measurement_lifecycle(client):
    """Record a measured wall distance, see its length, then delete it."""
    project = add_floor(client, "Level 1", 0.0)
    floor_id = project["floors"][0]["id"]
    calibrate(client, floor_id, metres_per_unit=0.1)

    project = client.post(
        f"/api/floors/{floor_id}/measurements",
        json={"p1": [0, 0], "p2": [100, 0], "label": "Wall to scanner"},
    ).json()
    measurement = project["floors"][0]["measurements"][0]
    assert measurement["label"] == "Wall to scanner"
    assert measurement["metres"] == pytest.approx(10.0)
    # Default display unit is feet; 10 m is 32 ft 9.7 in.
    assert measurement["display"].startswith("32'")

    project = client.delete(
        f"/api/floors/{floor_id}/measurements/{measurement['id']}"
    ).json()
    assert project["floors"][0]["measurements"] == []


def test_measurement_requires_a_calibrated_floor(client):
    project = add_floor(client, "Level 1", 0.0)
    floor_id = project["floors"][0]["id"]
    response = client.post(
        f"/api/floors/{floor_id}/measurements", json={"p1": [0, 0], "p2": [50, 0]}
    )
    assert response.status_code == 400
    assert "calibration" in response.json()["detail"]


def test_display_unit_switches_measurement_formatting(client):
    project = add_floor(client, "Level 1", 0.0)
    floor_id = project["floors"][0]["id"]
    calibrate(client, floor_id, metres_per_unit=0.1)
    client.post(f"/api/floors/{floor_id}/measurements", json={"p1": [0, 0], "p2": [100, 0]})

    project = client.post("/api/project/display-unit", json={"unit": "m"}).json()
    assert project["floors"][0]["measurements"][0]["display"] == "10.00 m"
    assert client.post("/api/project/display-unit", json={"unit": "furlong"}).status_code == 400


def two_floor_setup(client):
    """A source on the lower floor and a point directly above it, 4.3 m up."""
    project = add_floor(client, "Source floor", 0.0)
    project = add_floor(client, "Floor above", 4.3)
    lower, upper = project["floors"][0]["id"], project["floors"][1]["id"]
    for floor_id in (lower, upper):
        calibrate(client, floor_id)
        client.patch(f"/api/floors/{floor_id}", json={"alignment": [0, 0], "alignment2": [100, 0]})
    project = client.post("/api/sources", json={
        "floor_id": lower, "x": 0, "y": 0, "label": "Uptake room",
        "method": "tg108", "height_above_floor_m": 1.0,
        "params": {"kind": "uptake", "nuclide": "F-18",
                   "administered_activity_MBq": 555, "patients_per_week": 40,
                   "uptake_time_h": 1.0},
    }).json()
    source_id = project["sources"][0]["id"]
    project = client.post("/api/pois", json={
        "floor_id": upper, "x": 0, "y": 0, "label": "Office above",
        "occupancy": 1.0, "area_class": "uncontrolled", "auto_height": True,
        "linked_source_ids": [source_id],
    }).json()
    return project["pois"][0]["id"], source_id


def test_distances_endpoint_exposes_components(client):
    poi_id, source_id = two_floor_setup(client)
    payload = client.get("/api/distances").json()
    assert payload["display_unit"] == "ft"
    link = payload["points"][0]["links"][0]
    assert link["source_id"] == source_id
    assert link["distance_m"] == pytest.approx(3.8)
    assert link["vertical_m"] == pytest.approx(3.8)
    assert link["horizontal_m"] == pytest.approx(0.0)
    assert link["override_m"] is None
    assert link["display"].startswith("12'")


def test_entered_distance_overrides_geometry_and_changes_the_result(client):
    poi_id, source_id = two_floor_setup(client)
    before = client.get("/api/results").json()["results"][0]["methods"][0]["total"]

    client.patch(f"/api/pois/{poi_id}", json={"distance_overrides": {source_id: 7.6}})
    payload = client.get("/api/distances").json()
    link = payload["points"][0]["links"][0]
    assert link["distance_m"] == 7.6
    assert link["geometric_m"] == pytest.approx(3.8)

    result = client.get("/api/results").json()["results"][0]
    after = result["methods"][0]["total"]
    assert after == pytest.approx(before / 4.0, rel=0.01)
    assert result["contributions"][0]["geometric_distance_m"] == pytest.approx(3.8)
    assert any("entered manually" in n for n in result["contributions"][0]["notes"])


def test_clearing_an_override_returns_to_the_drawing_geometry(client):
    poi_id, source_id = two_floor_setup(client)
    client.patch(f"/api/pois/{poi_id}", json={"distance_overrides": {source_id: 9.0}})
    client.patch(f"/api/pois/{poi_id}", json={"distance_overrides": {source_id: ""}})
    link = client.get("/api/distances").json()["points"][0]["links"][0]
    assert link["override_m"] is None
    assert link["distance_m"] == pytest.approx(3.8)


def test_invalid_overrides_are_rejected(client):
    poi_id, source_id = two_floor_setup(client)
    assert client.patch(
        f"/api/pois/{poi_id}", json={"distance_overrides": {source_id: -2}}
    ).status_code == 400
    assert client.patch(
        f"/api/pois/{poi_id}", json={"distance_overrides": {source_id: "abc"}}
    ).status_code == 400


def test_unlinking_a_source_drops_its_override(client):
    poi_id, source_id = two_floor_setup(client)
    client.patch(f"/api/pois/{poi_id}", json={"distance_overrides": {source_id: 9.0}})
    project = client.patch(f"/api/pois/{poi_id}", json={"linked_source_ids": []}).json()
    assert project["pois"][0]["distance_overrides"] == {}


def test_measurements_and_overrides_survive_save_and_reopen(client, tmp_path):
    poi_id, source_id = two_floor_setup(client)
    floor_id = client.get("/api/project").json()["floors"][0]["id"]
    client.post(f"/api/floors/{floor_id}/measurements",
                json={"p1": [0, 0], "p2": [80, 0], "label": "Wall standoff"})
    client.patch(f"/api/pois/{poi_id}", json={"distance_overrides": {source_id: 5.5}})

    path = tmp_path / "measured.rsproj"
    client.post("/api/project/save", json={"path": str(path)})
    client.post("/api/project/new")
    reopened = client.post("/api/project/load", json={"path": str(path)}).json()

    assert reopened["floors"][0]["measurements"][0]["label"] == "Wall standoff"
    assert reopened["floors"][0]["measurements"][0]["metres"] == pytest.approx(8.0)
    assert reopened["pois"][0]["distance_overrides"] == {source_id: 5.5}


def test_csv_export_records_the_geometric_distance_alongside_the_override(client):
    poi_id, source_id = two_floor_setup(client)
    client.patch(f"/api/pois/{poi_id}", json={"distance_overrides": {source_id: 6.0}})
    rows = list(csv.DictReader(io.StringIO(client.get("/api/results.csv").text)))
    source_row = next(row for row in rows if row["row_type"] == "source")
    assert float(source_row["distance_m"]) == 6.0
    assert float(source_row["geometric_distance_m"]) == pytest.approx(3.8)


def wall_scenario(client):
    """A source and a point 8 m apart on one floor, straddling the line x = 0."""
    project = add_floor(client, "Level 1", 0.0)
    floor_id = project["floors"][0]["id"]
    calibrate(client, floor_id, metres_per_unit=0.1)
    client.patch(f"/api/floors/{floor_id}", json={"alignment": [0, 0], "alignment2": [100, 0]})
    project = client.post("/api/sources", json={
        "floor_id": floor_id, "x": -40, "y": 0, "label": "Uptake room",
        "method": "tg108", "height_above_floor_m": 1.0,
        "params": {"kind": "uptake", "nuclide": "F-18",
                   "administered_activity_MBq": 555, "patients_per_week": 40,
                   "uptake_time_h": 1.0},
    }).json()
    source_id = project["sources"][0]["id"]
    project = client.post("/api/pois", json={
        "floor_id": floor_id, "x": 40, "y": 0, "label": "Office",
        "occupancy": 1.0, "area_class": "uncontrolled", "auto_height": False,
        "height_above_floor_m": 1.0, "linked_source_ids": [source_id],
    }).json()
    client.post("/api/materials", json={"materials": ["lead"]})
    return floor_id, source_id, project["pois"][0]["id"]


def test_wall_crud(client):
    floor_id, _, _ = wall_scenario(client)
    project = client.post(f"/api/floors/{floor_id}/walls", json={
        "p1": [0, -50], "p2": [0, 50], "material": "concrete",
        "thickness_mm": 200, "top_height_m": 3.0, "label": "Corridor wall",
    }).json()
    wall = project["floors"][0]["walls"][0]
    assert wall["label"] == "Corridor wall"
    assert wall["thickness_mm"] == 200

    project = client.patch(f"/api/floors/{floor_id}/walls/{wall['id']}",
                           json={"material": "lead", "thickness_mm": 3}).json()
    assert project["floors"][0]["walls"][0]["material"] == "lead"
    # The geometry is preserved through an edit.
    assert project["floors"][0]["walls"][0]["p1"] == [0, -50]

    project = client.delete(f"/api/floors/{floor_id}/walls/{wall['id']}").json()
    assert project["floors"][0]["walls"] == []


def test_wall_validation_is_enforced(client):
    floor_id, _, _ = wall_scenario(client)
    assert client.post(f"/api/floors/{floor_id}/walls", json={
        "p1": [0, 0], "p2": [10, 0], "thickness_mm": 0}).status_code == 400
    assert client.post(f"/api/floors/{floor_id}/walls", json={
        "p1": [0, 0], "p2": [10, 0], "base_height_m": 3, "top_height_m": 1}).status_code == 400


def test_drawn_wall_attenuates_the_dose_and_shows_in_results(client):
    floor_id, source_id, poi_id = wall_scenario(client)
    before = client.get("/api/results").json()["results"][0]
    bare_thickness = before["governing_thickness_mm"]["lead"]

    client.post(f"/api/floors/{floor_id}/walls", json={
        "p1": [0, -50], "p2": [0, 50], "material": "concrete",
        "thickness_mm": 200, "top_height_m": 3.0, "label": "Corridor wall",
    })

    after = client.get("/api/results").json()["results"][0]
    contribution = after["contributions"][0]
    assert contribution["path_transmission"] < 1.0
    assert contribution["value"] < contribution["unshielded_value"]
    assert contribution["barriers"][0]["label"] == "Corridor wall"
    assert after["governing_thickness_mm"]["lead"] < bare_thickness


def test_barriers_endpoint_lists_the_path_before_calculating(client):
    floor_id, source_id, poi_id = wall_scenario(client)
    client.post(f"/api/floors/{floor_id}/walls", json={
        "p1": [0, -50], "p2": [0, 50], "material": "concrete", "thickness_mm": 200,
        "top_height_m": 3.0, "label": "Corridor wall"})
    link = client.get("/api/barriers").json()["points"][0]["links"][0]
    assert link["label"] == "Uptake room"
    assert [b["label"] for b in link["barriers"]] == ["Corridor wall"]
    assert link["barriers"][0]["drawn"] is True


def test_named_barrier_declared_for_one_path(client):
    floor_id, source_id, poi_id = wall_scenario(client)
    before = client.get("/api/results").json()["results"][0]["methods"][0]["total"]

    client.patch(f"/api/pois/{poi_id}", json={"manual_barriers": {
        source_id: [{"material": "lead", "thickness_mm": 3.0, "label": "Leaded door"}]}})

    result = client.get("/api/results").json()["results"][0]
    assert result["methods"][0]["total"] < before
    barrier = result["contributions"][0]["barriers"][0]
    assert barrier["label"] == "Leaded door"
    assert barrier["wall_id"] is None

    link = client.get("/api/barriers").json()["points"][0]["links"][0]
    assert link["barriers"][0]["drawn"] is False


def test_named_barrier_validation(client):
    _, source_id, poi_id = wall_scenario(client)
    assert client.patch(f"/api/pois/{poi_id}", json={"manual_barriers": {
        source_id: [{"material": "lead", "thickness_mm": -1}]}}).status_code == 400
    assert client.patch(f"/api/pois/{poi_id}", json={"manual_barriers": {
        source_id: [{"material": "lead", "thickness_mm": "thick"}]}}).status_code == 400


def test_obliquity_toggle_changes_the_result(client):
    """A wall crossed at an angle presents more material once obliquity is on."""
    project = add_floor(client, "Level 1", 0.0)
    floor_id = project["floors"][0]["id"]
    calibrate(client, floor_id, metres_per_unit=0.1)
    client.patch(f"/api/floors/{floor_id}", json={"alignment": [0, 0], "alignment2": [100, 0]})
    project = client.post("/api/sources", json={
        "floor_id": floor_id, "x": -10, "y": -10, "height_above_floor_m": 1.0,
        "params": {"kind": "uptake", "nuclide": "F-18",
                   "administered_activity_MBq": 555, "patients_per_week": 40,
                   "uptake_time_h": 1.0},
    }).json()
    client.post("/api/pois", json={
        "floor_id": floor_id, "x": 10, "y": 10, "auto_height": False,
        "height_above_floor_m": 1.0,
        "linked_source_ids": [project["sources"][0]["id"]],
    })
    client.post("/api/materials", json={"materials": ["lead"]})
    client.post(f"/api/floors/{floor_id}/walls", json={
        "p1": [0, -50], "p2": [0, 50], "material": "concrete", "thickness_mm": 50,
        "top_height_m": 3.0})

    straight = client.get("/api/results").json()["results"][0]
    client.post("/api/project/obliquity", json={"enabled": True})
    oblique = client.get("/api/results").json()["results"][0]

    assert oblique["contributions"][0]["barriers"][0]["angle_deg"] == pytest.approx(45, abs=1)
    assert oblique["contributions"][0]["barriers"][0]["oblique"] is True
    assert (oblique["contributions"][0]["path_transmission"]
            < straight["contributions"][0]["path_transmission"])
    assert oblique["governing_thickness_mm"]["lead"] < straight["governing_thickness_mm"]["lead"]


def test_walls_survive_save_and_reopen(client, tmp_path):
    floor_id, source_id, poi_id = wall_scenario(client)
    client.post(f"/api/floors/{floor_id}/walls", json={
        "p1": [0, -50], "p2": [0, 50], "material": "concrete", "thickness_mm": 200,
        "top_height_m": 3.4, "label": "Corridor wall"})
    client.patch(f"/api/pois/{poi_id}", json={"manual_barriers": {
        source_id: [{"material": "lead", "thickness_mm": 3.0, "label": "Leaded door"}]}})
    client.post("/api/project/obliquity", json={"enabled": True})

    path = tmp_path / "walls.rsproj"
    client.post("/api/project/save", json={"path": str(path)})
    client.post("/api/project/new")
    reopened = client.post("/api/project/load", json={"path": str(path)}).json()

    wall = reopened["floors"][0]["walls"][0]
    assert (wall["label"], wall["thickness_mm"], wall["top_height_m"]) == ("Corridor wall", 200, 3.4)
    assert reopened["pois"][0]["manual_barriers"][source_id][0]["label"] == "Leaded door"
    assert reopened["apply_obliquity"] is True


def test_csv_records_barriers_and_path_transmission(client):
    floor_id, source_id, poi_id = wall_scenario(client)
    client.post(f"/api/floors/{floor_id}/walls", json={
        "p1": [0, -50], "p2": [0, 50], "material": "concrete", "thickness_mm": 200,
        "top_height_m": 3.0, "label": "Corridor wall"})
    rows = list(csv.DictReader(io.StringIO(client.get("/api/results.csv").text)))
    source_row = next(row for row in rows if row["row_type"] == "source")
    assert "Corridor wall" in source_row["barriers"]
    assert float(source_row["path_transmission"]) < 1.0
    assert float(source_row["path_lead_equivalent_mm"]) > 0
    assert float(source_row["unshielded_value"]) > float(source_row["value"])


def test_deleting_a_source_clears_its_named_barriers(client):
    _, source_id, poi_id = wall_scenario(client)
    client.patch(f"/api/pois/{poi_id}", json={"manual_barriers": {
        source_id: [{"material": "lead", "thickness_mm": 3.0}]}})
    project = client.delete(f"/api/sources/{source_id}").json()
    assert project["pois"][0]["manual_barriers"] == {}


# A cut-down vendor plan view covering both the table axis and the bore, so
# rotating the scanner genuinely changes which part of the chart is read.
CHART_GRID = (
    "\t-59.1\t-19.7\t0\t19.7\t59.1\n"
    "-19.7\t0.001\t0.002\tNA\t0.002\t0.001\n"
    "0\t0.004\t0.005\tNA\t0.005\t0.004\n"
    "19.7\t0.018\t0.370\t0.757\t0.381\t0.018\n"
    "39.4\t0.053\t0.149\t0.195\t0.157\t0.053\n"
    "59.1\t0.047\t0.076\t0.087\t0.078\t0.047\n"
)


def chart_scenario(client, per="procedure"):
    """A CT at the isocentre with a chart, and a point 1.5 m due north of it."""
    project = add_floor(client, "Level 1", 0.0)
    floor_id = project["floors"][0]["id"]
    calibrate(client, floor_id, metres_per_unit=0.1)
    client.patch(f"/api/floors/{floor_id}", json={"alignment": [0, 0], "alignment2": [100, 0]})

    project = client.post("/api/scatter-maps", json={
        "name": "Vendor plan", "plane": "plan", "coordinate_unit": "in",
        "value_unit": "mGy", "per": per, "source": "vendor", "grid": CHART_GRID,
    }).json()
    map_id = project["scatter_maps"][0]["id"]

    params = {"scatter_method": "chart", "plan_map_id": map_id, "kvp": 125,
              "scatter_source": "vendor"}
    params.update({"mas_per_week": 5000} if "mAs" in per else {"procedures_per_week": 100})
    project = client.post("/api/sources", json={
        "floor_id": floor_id, "x": 0, "y": 0, "label": "CT", "method": "ncrp147_ct",
        "height_above_floor_m": 1.0, "rotation_deg": 0.0, "params": params,
    }).json()
    source_id = project["sources"][0]["id"]

    # 59.1 in north of the isocentre; PDF y grows downward so north is -y.
    offset = 59.1 * 0.0254 * 10
    project = client.post("/api/pois", json={
        "floor_id": floor_id, "x": 0, "y": -offset, "label": "Wall",
        "occupancy": 1.0, "area_class": "uncontrolled", "auto_height": False,
        "height_above_floor_m": 1.0, "offset_applied": True,
        "linked_source_ids": [source_id],
    }).json()
    client.post("/api/materials", json={"materials": ["lead"]})
    return floor_id, source_id, project["pois"][0]["id"], map_id


def test_chart_import_and_summary(client):
    _, _, _, map_id = chart_scenario(client)
    chart = client.get("/api/project").json()["scatter_maps"][0]
    assert chart["id"] == map_id
    assert "plan view" in chart["summary"]
    assert "23 cells" in chart["summary"]     # 5x5 grid less the two masked cells


def test_malformed_chart_is_rejected_on_import(client):
    add_floor(client, "Level 1", 0.0)
    assert client.post("/api/scatter-maps", json={"grid": "nonsense"}).status_code == 400
    assert client.post("/api/scatter-maps", json={"grid": ""}).status_code == 400


def test_flip_x_mirrors_which_side_of_the_chart_is_read(client):
    """A vendor's own left/right convention doesn't always match the source's
    rotation arrow; flip_x corrects it without re-pasting the grid."""
    project = add_floor(client, "Level 1", 0.0)
    floor_id = project["floors"][0]["id"]
    calibrate(client, floor_id, metres_per_unit=0.1)
    client.patch(f"/api/floors/{floor_id}", json={"alignment": [0, 0], "alignment2": [100, 0]})

    # Row-independent values, so only the column (x) matters to the read.
    grid = "\t-19.7\t0\t19.7\n-9.85\t0.010\t0.500\t0.030\n9.85\t0.010\t0.500\t0.030\n"
    project = client.post("/api/scatter-maps", json={
        "name": "Asymmetric", "plane": "plan", "coordinate_unit": "in",
        "value_unit": "mGy", "per": "procedure", "grid": grid,
    }).json()
    map_id = project["scatter_maps"][0]["id"]
    assert project["scatter_maps"][0]["flip_x"] is False

    project = client.post("/api/sources", json={
        "floor_id": floor_id, "x": 0, "y": 0, "label": "CT", "method": "ncrp147_ct",
        "height_above_floor_m": 1.0, "rotation_deg": 0.0,
        "params": {"scatter_method": "chart", "plan_map_id": map_id, "kvp": 125,
                   "scatter_source": "vendor", "procedures_per_week": 1},
    }).json()
    source_id = project["sources"][0]["id"]

    # 19.7 in east of the isocentre, i.e. along the chart's +x with no rotation.
    offset = 19.7 * 0.0254 * 10
    project = client.post("/api/pois", json={
        "floor_id": floor_id, "x": offset, "y": 0, "label": "Wall",
        "occupancy": 1.0, "area_class": "uncontrolled", "auto_height": False,
        "height_above_floor_m": 1.0, "offset_applied": True,
        "linked_source_ids": [source_id],
    }).json()
    client.post("/api/materials", json={"materials": ["lead"]})

    before = client.get("/api/results").json()["results"][0]["methods"][0]["total"]
    assert before == pytest.approx(0.030, rel=1e-6)

    project = client.patch(f"/api/scatter-maps/{map_id}", json={"flip_x": True}).json()
    assert project["scatter_maps"][0]["flip_x"] is True
    assert "columns flipped" in project["scatter_maps"][0]["summary"]

    after = client.get("/api/results").json()["results"][0]["methods"][0]["total"]
    assert after == pytest.approx(0.010, rel=1e-6)


def test_chart_value_is_used_as_published_on_the_same_floor(client):
    """The chart reads 0.087 mGy at that spot; no inverse-square correction."""
    chart_scenario(client)
    result = client.get("/api/results").json()["results"][0]
    assert result["methods"][0]["total"] == pytest.approx(0.087 * 100, rel=1e-6)
    assert any("no inverse-square correction" in n for n in result["contributions"][0]["notes"])


def test_rotation_changes_the_dose_through_the_api(client):
    _, source_id, _, _ = chart_scenario(client)
    facing = client.get("/api/results").json()["results"][0]["methods"][0]["total"]
    client.patch(f"/api/sources/{source_id}", json={"rotation_deg": 90})
    side_on = client.get("/api/results").json()["results"][0]["methods"][0]["total"]
    # Down the table the chart reads 0.087; out through the bore, 0.004.
    assert facing == pytest.approx(0.087 * 100, rel=1e-6)
    assert side_on == pytest.approx(0.004 * 100, rel=1e-6)


def test_chart_quoted_per_mas_scales_with_workload(client):
    """5000 mAs a week against a chart of 0.087 mGy per 100 mAs."""
    chart_scenario(client, per="100 mAs")
    result = client.get("/api/results").json()["results"][0]
    assert result["methods"][0]["total"] == pytest.approx(0.087 * 50, rel=1e-6)
    assert "chart kerma at the point (mGy per 100 mAs)" in result["contributions"][0]["terms"]


def test_charts_survive_save_and_reopen(client, tmp_path):
    _, source_id, _, _ = chart_scenario(client)
    client.patch(f"/api/sources/{source_id}", json={"rotation_deg": 42.5})
    path = tmp_path / "chart.rsproj"
    client.post("/api/project/save", json={"path": str(path)})
    client.post("/api/project/new")
    reopened = client.post("/api/project/load", json={"path": str(path)}).json()
    assert reopened["scatter_maps"][0]["name"] == "Vendor plan"
    assert reopened["sources"][0]["rotation_deg"] == 42.5
    assert client.get("/api/results").json()["results"][0]["methods"][0]["total"] > 0


def test_deleting_a_chart_unassigns_it(client):
    _, source_id, _, map_id = chart_scenario(client)
    project = client.delete(f"/api/scatter-maps/{map_id}").json()
    assert project["scatter_maps"] == []
    assert project["sources"][0]["params"]["plan_map_id"] == ""
    # The calculation reports the missing chart rather than crashing.
    result = client.get("/api/results").json()["results"][0]
    assert any("no chart is assigned" in e for e in result["errors"])


def test_wall_thickness_is_editable_after_drawing(client):
    floor_id, _, _ = wall_scenario(client)
    project = client.post(f"/api/floors/{floor_id}/walls", json={
        "p1": [0, -50], "p2": [0, 50], "material": "concrete",
        "thickness_mm": 203.2, "top_height_m": 3.0,
    }).json()
    wall_id = project["floors"][0]["walls"][0]["id"]

    project = client.patch(f"/api/floors/{floor_id}/walls/{wall_id}",
                           json={"thickness_mm": 152.4, "top_height_m": 4.2}).json()
    wall = project["floors"][0]["walls"][0]
    assert wall["thickness_mm"] == pytest.approx(152.4)
    assert wall["top_height_m"] == pytest.approx(4.2)
    assert wall["p1"] == [0, -50]        # the drawn geometry is untouched

    # A thinner wall attenuates less, so the requirement rises.
    thinner = client.get("/api/results").json()["results"][0]
    client.patch(f"/api/floors/{floor_id}/walls/{wall_id}", json={"thickness_mm": 400.0})
    thicker = client.get("/api/results").json()["results"][0]
    assert (thicker["contributions"][0]["path_transmission"]
            < thinner["contributions"][0]["path_transmission"])


def test_editing_a_wall_to_an_absurd_thickness_is_rejected(client):
    floor_id, _, _ = wall_scenario(client)
    project = client.post(f"/api/floors/{floor_id}/walls", json={
        "p1": [0, -50], "p2": [0, 50], "thickness_mm": 200}).json()
    wall_id = project["floors"][0]["walls"][0]["id"]
    response = client.patch(f"/api/floors/{floor_id}/walls/{wall_id}",
                            json={"thickness_mm": 200_000})
    assert response.status_code == 400
    assert "millimetres" in response.json()["detail"]
    # The original wall survives a rejected edit.
    assert client.get("/api/project").json()["floors"][0]["walls"][0]["thickness_mm"] == 200


def mix_scenario(client, components):
    """A source running the given isotope mix, with a point 4 m away."""
    project = add_floor(client, "Level 1", 0.0)
    floor_id = project["floors"][0]["id"]
    calibrate(client, floor_id, metres_per_unit=0.1)
    client.patch(f"/api/floors/{floor_id}", json={"alignment": [0, 0], "alignment2": [100, 0]})
    project = client.post("/api/sources", json={
        "floor_id": floor_id, "x": 0, "y": 0, "label": "Uptake room",
        "method": "tg108", "height_above_floor_m": 1.0,
        "params": {"components": components},
    }).json()
    client.post("/api/pois", json={
        "floor_id": floor_id, "x": 40, "y": 0, "label": "Office", "occupancy": 1.0,
        "auto_height": False, "height_above_floor_m": 1.0, "offset_applied": True,
        "linked_source_ids": [project["sources"][0]["id"]],
    })
    client.post("/api/materials", json={"materials": ["lead"]})
    return project["sources"][0]["id"]


FDG = {"kind": "uptake", "nuclide": "F-18", "administered_activity_MBq": 555,
       "patients_per_week": 40, "uptake_time_h": 1.0, "label": "F-18 FDG"}
DOTATATE = {"kind": "uptake", "nuclide": "Ga-68", "administered_activity_MBq": 185,
            "patients_per_week": 6, "uptake_time_h": 0.75, "label": "Ga-68 DOTATATE"}


def test_isotope_mix_sums_and_reports_each_component(client):
    mix_scenario(client, [FDG, DOTATATE])
    contribution = client.get("/api/results").json()["results"][0]["contributions"][0]
    assert len(contribution["components"]) == 2
    assert contribution["unshielded_value"] == pytest.approx(
        sum(c["unshielded_uSv"] for c in contribution["components"])
    )
    assert any("each decayed on its own" in n for n in contribution["notes"])


def test_adding_an_isotope_through_the_api_raises_the_requirement(client):
    source_id = mix_scenario(client, [FDG])
    before = client.get("/api/results").json()["results"][0]
    client.patch(f"/api/sources/{source_id}", json={"params": {"components": [FDG, DOTATATE]}})
    after = client.get("/api/results").json()["results"][0]
    assert after["methods"][0]["total"] > before["methods"][0]["total"]
    assert after["governing_thickness_mm"]["lead"] > before["governing_thickness_mm"]["lead"]


def test_bad_isotope_entries_are_rejected(client):
    source_id = mix_scenario(client, [FDG])
    for bad, message in [
        ({**FDG, "nuclide": "Xx-99"}, "not registered"),
        ({**FDG, "kind": "sideways"}, "kind must be"),
        ({**FDG, "uptake_time_h": 0}, "greater than zero"),
        ({**FDG, "administered_activity_MBq": -5}, "cannot be negative"),
    ]:
        response = client.patch(f"/api/sources/{source_id}",
                                json={"params": {"components": [bad]}})
        assert response.status_code == 400, bad
        assert message in response.json()["detail"]
    # The source survives every rejected edit.
    assert client.get("/api/results").json()["results"][0]["methods"][0]["total"] > 0


def test_isotope_mix_appears_in_the_csv(client):
    mix_scenario(client, [FDG, DOTATATE])
    rows = list(csv.DictReader(io.StringIO(client.get("/api/results.csv").text)))
    source_row = next(row for row in rows if row["row_type"] == "source")
    assert "F-18 FDG" in source_row["isotopes"]
    assert "Ga-68 DOTATATE" in source_row["isotopes"]


def test_scale_can_be_typed_instead_of_drawn(client):
    project = add_floor(client, "Level 1", 0.0)
    floor_id = project["floors"][0]["id"]
    project = client.patch(f"/api/floors/{floor_id}",
                           json={"calibration": {"scale": '1/4" = 1\''}}).json()
    floor = project["floors"][0]
    assert "1:48" in floor["scale_description"]
    assert floor["metres_per_unit"] == pytest.approx(0.0254 / 72 * 48)
    assert "not calibrated" not in " ".join(project["problems"])


def test_a_malformed_typed_scale_is_rejected(client):
    project = add_floor(client, "Level 1", 0.0)
    floor_id = project["floors"][0]["id"]
    response = client.patch(f"/api/floors/{floor_id}", json={"calibration": {"scale": "50"}})
    assert response.status_code == 400
    assert "paper:real" in response.json()["detail"]


def test_source_reports_its_unshielded_dose_at_one_metre(client):
    """A sanity figure that depends only on the source's own parameters."""
    mix_scenario(client, [FDG])
    source = client.get("/api/project").json()["sources"][0]
    reference = source["reference_dose"]
    assert reference["unit"] == "uSv/week"
    assert "at 1 m" in reference["note"]
    # 0.092 x 40 x 555 x 1 h x R(1 h) at 1 m.
    assert reference["value"] == pytest.approx(0.092 * 40 * 555 * 0.8327, rel=0.01)


def test_reference_dose_lists_each_isotope_of_a_mix(client):
    mix_scenario(client, [FDG, DOTATATE])
    reference = client.get("/api/project").json()["sources"][0]["reference_dose"]
    assert len(reference["components"]) == 2
    assert reference["value"] == pytest.approx(sum(c["value"] for c in reference["components"]))


def test_reference_dose_reports_an_unusable_source_rather_than_failing(client):
    source_id = mix_scenario(client, [FDG])
    client.patch(f"/api/sources/{source_id}",
                 json={"method": "ncrp147_ct",
                       "params": {"scatter_method": "chart", "plan_map_id": ""}})
    reference = client.get("/api/project").json()["sources"][0]["reference_dose"]
    assert "error" in reference
