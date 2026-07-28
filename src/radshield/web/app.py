"""FastAPI application hosting the shielding calculator.

State is held in a single in-process :class:`Session` because this is a local
single-user tool.  Everything durable goes into the ``.rsproj`` archive, so
restarting the server loses nothing that was saved.  Splitting the session out
behind an interface keeps the door open for a multi-project deployment later.
"""

from __future__ import annotations

import csv
import io
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from ..engine.evaluate import describe_distances, evaluate_project, results_to_rows
from ..model.geometry import check_project, format_length, measurement_length
from ..model.project import (
    LENGTH_UNITS,
    Calibration,
    Floor,
    Measurement,
    PointOfInterest,
    Project,
    SourcePoint,
    new_id,
)
from ..model.store import load as load_project
from ..model.store import save as save_project
from ..physics import nuclides
from ..physics.ncrp147 import tables as ncrp_tables
from . import render

STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class Session:
    """The single project currently open, plus its PDF bytes."""

    project: Project = field(default_factory=Project)
    pdfs: dict[str, bytes] = field(default_factory=dict)
    path: Path | None = None


session = Session()
app = FastAPI(title="Radiation Shielding Calculator")


def _project_payload() -> dict[str, Any]:
    """Serialise the project plus derived display values."""
    data = session.project.to_dict()
    for floor in data["floors"]:
        stored = session.project.floor(floor["id"])
        floor["scale_description"] = (
            stored.calibration.describe() if stored.calibration else "not calibrated"
        )
        floor["metres_per_unit"] = (
            stored.calibration.metres_per_unit if stored.calibration else None
        )
        for raw, item in zip(floor["measurements"], stored.measurements):
            if stored.calibration:
                metres = measurement_length(stored, item)
                raw["metres"] = metres
                raw["display"] = format_length(metres, session.project.display_unit)
            else:
                raw["metres"] = None
                raw["display"] = "floor not calibrated"
    data["problems"] = check_project(session.project)
    data["path"] = str(session.path) if session.path else None
    return data


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """Serve the single-page application."""
    return HTMLResponse((STATIC_DIR / "index.html").read_text())


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """Serve an inline icon so browsers stop logging a 404 for it."""
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        "<rect width='32' height='32' rx='6' fill='#12151a'/>"
        "<circle cx='16' cy='16' r='4' fill='#ff8a3d'/>"
        "<circle cx='16' cy='16' r='9' fill='none' stroke='#40d0a0' stroke-width='2'/>"
        "</svg>"
    )
    return Response(svg, media_type="image/svg+xml")


@app.get("/api/project")
def get_project() -> dict[str, Any]:
    """Return the current project state."""
    return _project_payload()


@app.post("/api/project/name")
def set_name(payload: dict[str, str]) -> dict[str, Any]:
    """Rename the project."""
    session.project.name = payload.get("name", session.project.name)
    return _project_payload()


@app.post("/api/project/new")
def new_project() -> dict[str, Any]:
    """Discard the current project and start an empty one."""
    session.project = Project()
    session.pdfs = {}
    session.path = None
    return _project_payload()


@app.get("/api/options")
def options() -> dict[str, Any]:
    """Return the choices the UI needs to populate its forms."""
    return {
        "nuclides": nuclides.available_nuclides(),
        "workloads": ncrp_tables.available_workloads(),
        "materials": ["lead", "concrete", "iron", "gypsum", "steel", "glass", "wood"],
        "occupancy": [
            {"factor": factor, "description": description, "verified": verified}
            for factor, description, verified in ncrp_tables.occupancy_factors()
        ],
        "known_gaps": list(ncrp_tables.KNOWN_GAPS),
    }


@app.post("/api/floors")
async def add_floor(file: UploadFile, name: str = "", elevation_m: float = 0.0) -> dict[str, Any]:
    """Upload a PDF and add it as a new floor."""
    blob = await file.read()
    if not blob:
        raise HTTPException(400, "uploaded file is empty")

    pdf_name = file.filename or f"floor_{len(session.project.floors) + 1}.pdf"
    # Keep names unique so two floors can come from identically-named files.
    if pdf_name in session.pdfs:
        stem, _, suffix = pdf_name.rpartition(".")
        pdf_name = f"{stem}_{len(session.pdfs)}.{suffix}" if suffix else f"{pdf_name}_{len(session.pdfs)}"

    try:
        info = render.page_info(blob, 0)
    except render.PdfError as exc:
        raise HTTPException(400, str(exc)) from exc

    session.pdfs[pdf_name] = blob
    floor = Floor(
        id=new_id("fl"),
        name=name or Path(pdf_name).stem,
        pdf_name=pdf_name,
        page=0,
        elevation_m=elevation_m,
        page_width=info.width,
        page_height=info.height,
    )
    session.project.floors.append(floor)
    session.project.floors.sort(key=lambda f: f.elevation_m)
    return _project_payload()


@app.patch("/api/floors/{floor_id}")
def update_floor(floor_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Update a floor's name, page, elevation, calibration or alignment point."""
    try:
        floor = session.project.floor(floor_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc

    if "name" in payload:
        floor.name = payload["name"]
    if "elevation_m" in payload:
        floor.elevation_m = float(payload["elevation_m"])
    if "page" in payload:
        page = int(payload["page"])
        try:
            info = render.page_info(session.pdfs[floor.pdf_name], page)
        except render.PdfError as exc:
            raise HTTPException(400, str(exc)) from exc
        floor.page = page
        floor.page_width, floor.page_height = info.width, info.height
    if "alignment" in payload:
        value = payload["alignment"]
        floor.alignment = tuple(value) if value else None
    if "calibration" in payload:
        value = payload["calibration"]
        if not value:
            floor.calibration = None
        else:
            calibration = Calibration(
                p1=tuple(value["p1"]),
                p2=tuple(value["p2"]),
                known_distance=float(value["known_distance"]),
                unit=value.get("unit", "ft"),
            )
            try:
                calibration.metres_per_unit
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            floor.calibration = calibration

    session.project.floors.sort(key=lambda f: f.elevation_m)
    return _project_payload()


@app.delete("/api/floors/{floor_id}")
def delete_floor(floor_id: str) -> dict[str, Any]:
    """Remove a floor and everything placed on it."""
    session.project.remove_floor(floor_id)
    return _project_payload()


@app.post("/api/floors/spacing")
def set_spacing(payload: dict[str, list[float]]) -> dict[str, Any]:
    """Set floor elevations from consecutive floor-to-floor heights."""
    try:
        session.project.set_floor_to_floor([float(h) for h in payload.get("heights", [])])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _project_payload()


@app.get("/api/floors/{floor_id}/image")
def floor_image(floor_id: str, zoom: float = 2.0) -> Response:
    """Rasterise the floor's page to PNG."""
    try:
        floor = session.project.floor(floor_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    try:
        png = render.render_page(session.pdfs[floor.pdf_name], floor.page, zoom)
    except render.PdfError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/floors/{floor_id}/measurements")
def add_measurement(floor_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Record a measured distance between two points on a drawing."""
    try:
        floor = session.project.floor(floor_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    if floor.calibration is None:
        raise HTTPException(400, f"floor {floor.name!r} has no scale calibration")
    floor.measurements.append(
        Measurement(
            id=new_id("msr"),
            p1=tuple(payload["p1"]),
            p2=tuple(payload["p2"]),
            label=payload.get("label", ""),
        )
    )
    return _project_payload()


@app.delete("/api/floors/{floor_id}/measurements/{measurement_id}")
def delete_measurement(floor_id: str, measurement_id: str) -> dict[str, Any]:
    """Remove a recorded measurement."""
    try:
        floor = session.project.floor(floor_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    floor.measurements = [m for m in floor.measurements if m.id != measurement_id]
    return _project_payload()


@app.get("/api/distances")
def distances() -> dict[str, Any]:
    """Source-to-point distances for every link, without running the physics."""
    return {"display_unit": session.project.display_unit, "points": describe_distances(session.project)}


@app.post("/api/project/display-unit")
def set_display_unit(payload: dict[str, str]) -> dict[str, Any]:
    """Choose the unit distances are displayed in."""
    unit = payload.get("unit", "ft")
    if unit not in LENGTH_UNITS:
        raise HTTPException(400, f"unknown unit {unit!r}; known: {sorted(LENGTH_UNITS)}")
    session.project.display_unit = unit
    return _project_payload()


@app.post("/api/sources")
def add_source(payload: dict[str, Any]) -> dict[str, Any]:
    """Place a source point."""
    source = SourcePoint(
        id=new_id("src"),
        floor_id=payload["floor_id"],
        x=float(payload["x"]),
        y=float(payload["y"]),
        label=payload.get("label", ""),
        method=payload.get("method", "tg108"),
        height_above_floor_m=float(payload.get("height_above_floor_m", 1.0)),
        params=payload.get("params", {}),
    )
    session.project.sources.append(source)
    return _project_payload()


@app.patch("/api/sources/{source_id}")
def update_source(source_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Update a source's position, label, method or parameters."""
    try:
        source = session.project.source(source_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    for attribute in ("x", "y", "height_above_floor_m"):
        if attribute in payload:
            setattr(source, attribute, float(payload[attribute]))
    for attribute in ("label", "method", "floor_id"):
        if attribute in payload:
            setattr(source, attribute, payload[attribute])
    if "params" in payload:
        source.params = payload["params"]
    return _project_payload()


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: str) -> dict[str, Any]:
    """Delete a source and unlink it from every point."""
    session.project.remove_source(source_id)
    return _project_payload()


@app.post("/api/pois")
def add_poi(payload: dict[str, Any]) -> dict[str, Any]:
    """Place a point of interest."""
    poi = PointOfInterest(
        id=new_id("poi"),
        floor_id=payload["floor_id"],
        x=float(payload["x"]),
        y=float(payload["y"]),
        label=payload.get("label", ""),
        occupancy=float(payload.get("occupancy", 1.0)),
        area_class=payload.get("area_class", "uncontrolled"),
        height_above_floor_m=float(payload.get("height_above_floor_m", 1.7)),
        auto_height=bool(payload.get("auto_height", True)),
        offset_applied=bool(payload.get("offset_applied", False)),
        existing_material=payload.get("existing_material", ""),
        existing_thickness=float(payload.get("existing_thickness", 0.0)),
        linked_source_ids=payload.get("linked_source_ids", []),
        distance_overrides=payload.get("distance_overrides", {}),
    )
    session.project.pois.append(poi)
    return _project_payload()


@app.patch("/api/pois/{poi_id}")
def update_poi(poi_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Update a point of interest."""
    try:
        poi = session.project.poi(poi_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    for attribute in ("x", "y", "occupancy", "height_above_floor_m", "existing_thickness"):
        if attribute in payload:
            setattr(poi, attribute, float(payload[attribute]))
    for attribute in ("label", "area_class", "existing_material", "floor_id"):
        if attribute in payload:
            setattr(poi, attribute, payload[attribute])
    for attribute in ("auto_height", "offset_applied"):
        if attribute in payload:
            setattr(poi, attribute, bool(payload[attribute]))
    if "linked_source_ids" in payload:
        poi.linked_source_ids = list(payload["linked_source_ids"])
        # Drop overrides for sources no longer contributing to this point.
        poi.distance_overrides = {
            k: v for k, v in poi.distance_overrides.items() if k in poi.linked_source_ids
        }
    if "distance_overrides" in payload:
        overrides: dict[str, float] = {}
        for source_id, value in (payload["distance_overrides"] or {}).items():
            if value in (None, ""):
                continue
            try:
                metres = float(value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, f"distance for {source_id} is not a number") from exc
            if metres <= 0:
                raise HTTPException(400, f"distance for {source_id} must be positive")
            overrides[source_id] = metres
        poi.distance_overrides = overrides
    return _project_payload()


@app.delete("/api/pois/{poi_id}")
def delete_poi(poi_id: str) -> dict[str, Any]:
    """Delete a point of interest."""
    session.project.pois = [p for p in session.project.pois if p.id != poi_id]
    return _project_payload()


@app.post("/api/materials")
def set_materials(payload: dict[str, list[str]]) -> dict[str, Any]:
    """Choose which materials results are reported in."""
    materials = payload.get("materials") or ["lead"]
    session.project.materials = materials
    return _project_payload()


@app.get("/api/results")
def results() -> dict[str, Any]:
    """Evaluate every point of interest."""
    computed = evaluate_project(session.project)
    return {
        "problems": check_project(session.project),
        "materials": session.project.materials,
        "results": [asdict(r) for r in computed],
    }


@app.get("/api/results.csv")
def results_csv() -> Response:
    """Export results as CSV, including the per-source audit rows."""
    computed = evaluate_project(session.project)
    rows = results_to_rows(computed, session.project.materials)
    if not rows:
        return Response("no results\n", media_type="text/csv")

    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="shielding_results.csv"'},
    )


@app.post("/api/project/save")
def save(payload: dict[str, str]) -> dict[str, Any]:
    """Write the project archive to disk."""
    path = Path(payload.get("path") or "project.rsproj").expanduser()
    if path.suffix != ".rsproj":
        path = path.with_suffix(".rsproj")
    try:
        save_project(session.project, path, session.pdfs)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.path = path
    return {"saved": str(path)}


@app.post("/api/project/load")
def load(payload: dict[str, str]) -> dict[str, Any]:
    """Open a project archive from disk."""
    path = Path(payload.get("path", "")).expanduser()
    if not path.exists():
        raise HTTPException(404, f"no such file: {path}")
    try:
        project, pdfs = load_project(path)
    except Exception as exc:
        raise HTTPException(400, f"could not open project: {exc}") from exc
    session.project = project
    session.pdfs = pdfs
    session.path = path
    return _project_payload()


@app.post("/api/project/upload")
async def upload_project(file: UploadFile) -> dict[str, Any]:
    """Open a project archive uploaded through the browser."""
    blob = await file.read()
    temp = Path(".uploaded.rsproj")
    temp.write_bytes(blob)
    try:
        project, pdfs = load_project(temp)
    except Exception as exc:
        raise HTTPException(400, f"could not open project: {exc}") from exc
    finally:
        temp.unlink(missing_ok=True)
    session.project = project
    session.pdfs = pdfs
    session.path = None
    return _project_payload()


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
