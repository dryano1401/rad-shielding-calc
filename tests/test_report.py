"""Word report generation: auto-filled data tables, placeholder narrative."""

from __future__ import annotations

import io

import pytest

docx = pytest.importorskip("docx")

from radshield.engine.evaluate import evaluate_project  # noqa: E402
from radshield.model.project import PointOfInterest  # noqa: E402
from radshield.web import report  # noqa: E402

from .test_geometry_and_engine import build_project, uptake_source  # noqa: E402


def _read(data: bytes):
    return docx.Document(io.BytesIO(data))


def _all_text(document) -> str:
    """Every paragraph and table cell's text, concatenated for substring checks."""
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def test_report_with_no_points_is_still_a_valid_document():
    project = build_project()
    data = report.build_report(project, [])
    document = _read(data)
    text = _all_text(document)
    assert "Radiation Shielding Report" in text
    assert "No points of interest have been evaluated yet" in text


def test_design_targets_table_is_populated_from_results():
    project = build_project()
    project.sources.append(uptake_source())
    project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, label="Office above",
                        auto_height=True, linked_source_ids=["src1"], offset_applied=True)
    )
    results = evaluate_project(project)
    data = report.build_report(project, results)
    document = _read(data)
    text = _all_text(document)

    assert "Office above" in text
    # TG-108 Example 4: 117 uSv/week.
    assert "117" in text
    assert "tg108" in text


def test_existing_shielding_table_appears_only_when_entered():
    project = build_project()
    project.sources.append(uptake_source())
    poi = PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, label="Office above",
                          auto_height=True, linked_source_ids=["src1"], offset_applied=True)
    project.pois.append(poi)

    bare = report.build_report(project, evaluate_project(project))
    bare_text = _all_text(_read(bare))
    assert "No existing/as-installed shielding was entered" in bare_text

    poi.existing_material = "lead"
    poi.existing_thickness = 5.0
    with_existing = report.build_report(project, evaluate_project(project))
    text = _all_text(_read(with_existing))
    assert "No existing/as-installed shielding" not in text
    assert "lead" in text
    assert "Office above" in text


def test_needs_shielding_points_are_named_in_the_summary():
    project = build_project()
    project.materials = ["lead"]
    source = uptake_source()
    source.params["administered_activity_MBq"] = 5550  # 10x, well over goal
    project.sources.append(source)
    project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl1", x=20, y=0, label="Close office",
                        auto_height=False, height_above_floor_m=1.0,
                        linked_source_ids=["src1"])
    )
    results = evaluate_project(project)
    assert results[0].governing_thickness_mm.get("lead", 0) > 0

    text = _all_text(_read(report.build_report(project, results)))
    assert "Close office" in text
    assert "require additional shielding" in text


def test_shielding_overview_reflects_methods_actually_in_use():
    """Only the methodologies with a source present should get a goal bullet."""
    project = build_project()
    project.sources.append(uptake_source())
    project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, label="Office above",
                        auto_height=True, linked_source_ids=["src1"], offset_applied=True)
    )
    text = _all_text(_read(report.build_report(project, evaluate_project(project))))
    assert "TG-108" in text
    assert "NCRP 147" not in text


def test_report_has_no_narrative_free_text_beyond_placeholders_and_computed_facts():
    """Every non-boilerplate, non-computed sentence must be visibly marked as
    a placeholder -- the report must never silently assert something about
    the facility, assumptions or findings it has no basis for."""
    project = build_project()
    data = report.build_report(project, [])
    document = _read(data)
    all_paragraphs = list(document.paragraphs)
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                all_paragraphs.extend(cell.paragraphs)
    placeholder_runs = [run.text for p in all_paragraphs for run in p.runs if run.italic]
    assert any("Enter facility" in t for t in placeholder_runs)
    assert any("Enter project-specific assumptions" in t for t in placeholder_runs)
    assert any("Signature" in t for t in placeholder_runs)
