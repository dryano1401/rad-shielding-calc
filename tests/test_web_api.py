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
        client.patch(f"/api/floors/{floor_id}", json={"alignment": [0, 0]})

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
