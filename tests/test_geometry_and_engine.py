"""Model, geometry and engine tests, including the TG-108 floor/ceiling examples."""

from __future__ import annotations

import pytest

from radshield.engine.evaluate import evaluate_point, evaluate_project, results_to_rows
from radshield.model.geometry import GeometryError, check_project, distance
from radshield.model.project import (
    Calibration,
    Floor,
    PointOfInterest,
    Project,
    SourcePoint,
)
from radshield.model.store import load, save


def build_project(floor_to_floor_m: float = 4.3) -> Project:
    """Three stacked floors at 1 PDF unit = 0.1 m, registered by two features."""
    project = Project(name="test")
    for index, name in enumerate(["Below", "Source", "Above"]):
        project.floors.append(
            Floor(
                id=f"fl{index}",
                name=name,
                pdf_name="plan.pdf",
                page=0,
                elevation_m=index * floor_to_floor_m,
                calibration=Calibration(p1=(0, 0), p2=(100, 0), known_distance=10, unit="m"),
                alignment=(0.0, 0.0),
                # A second reference feature along +x, so the frame is exactly
                # the one a single point implied and every expectation below
                # is unchanged -- but the drawings are now properly registered.
                alignment2=(100.0, 0.0),
                page_width=600,
                page_height=400,
            )
        )
    return project


def uptake_source(floor_id: str = "fl1", x: float = 0.0, y: float = 0.0) -> SourcePoint:
    """A TG-108 uptake room matching the parameters used throughout the report."""
    return SourcePoint(
        id="src1",
        floor_id=floor_id,
        x=x,
        y=y,
        label="Uptake room",
        method="tg108",
        height_above_floor_m=1.0,
        params={
            "kind": "uptake",
            "nuclide": "F-18",
            "administered_activity_MBq": 555,
            "patients_per_week": 40,
            "uptake_time_h": 1.0,
        },
    )


def test_calibration_scale():
    calibration = Calibration(p1=(100, 100), p2=(500, 100), known_distance=40, unit="ft")
    assert calibration.pixel_distance == 400
    assert calibration.metres_per_unit == pytest.approx(40 * 0.3048 / 400)
    assert "40 ft" in calibration.describe()


def test_calibration_rejects_coincident_points():
    with pytest.raises(ValueError, match="coincident"):
        Calibration(p1=(10, 10), p2=(10, 10), known_distance=5, unit="m").metres_per_unit


def test_floor_to_floor_sets_elevations():
    project = build_project()
    project.set_floor_to_floor([3.0, 5.0])
    assert [f.elevation_m for f in project.floors] == [0.0, 3.0, 8.0]
    with pytest.raises(ValueError, match="expected 2"):
        project.set_floor_to_floor([3.0])


def test_uncalibrated_floor_blocks_distance():
    project = build_project()
    project.floor("fl2").calibration = None
    source = uptake_source()
    poi = PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0)
    with pytest.raises(GeometryError, match="calibration"):
        distance(project, source, poi)


def test_example_4_room_above_distance():
    """TG-108 Example 4: 4.3 m floor-to-floor gives d = 3.8 m to the room above."""
    project = build_project()
    source = uptake_source()
    poi = PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, auto_height=True)
    result = distance(project, source, poi)
    assert result.vertical_m == pytest.approx(3.8)
    assert result.metres == pytest.approx(3.8)
    assert any("0.5 m above" in note for note in result.notes)


def test_example_5_room_below_distance():
    """TG-108 Example 5: the room below gives d = 3.6 m."""
    project = build_project()
    source = uptake_source()
    poi = PointOfInterest(id="poi1", floor_id="fl0", x=0, y=0, auto_height=True)
    result = distance(project, source, poi)
    assert result.vertical_m == pytest.approx(-3.6)
    assert result.metres == pytest.approx(3.6)
    assert any("1.7 m above" in note for note in result.notes)


def test_horizontal_distance_uses_floor_scale():
    """1 PDF unit = 0.1 m, so a 40 unit offset is 4 m."""
    project = build_project()
    source = uptake_source()
    poi = PointOfInterest(id="poi1", floor_id="fl1", x=40, y=0, auto_height=False,
                          height_above_floor_m=1.0)
    result = distance(project, source, poi)
    assert result.horizontal_m == pytest.approx(4.0)
    assert result.vertical_m == pytest.approx(0.0)
    assert result.metres == pytest.approx(4.0)


def test_three_dimensional_distance_combines_components():
    project = build_project()
    source = uptake_source()
    poi = PointOfInterest(id="poi1", floor_id="fl2", x=40, y=0, auto_height=True)
    result = distance(project, source, poi)
    assert result.metres == pytest.approx((4.0**2 + 3.8**2) ** 0.5)


def test_vertical_only_mode_ignores_horizontal_offset():
    project = build_project()
    source = uptake_source()
    poi = PointOfInterest(id="poi1", floor_id="fl2", x=400, y=0, auto_height=True)
    result = distance(project, source, poi, vertical_only=True)
    assert result.metres == pytest.approx(3.8)


def test_alignment_point_shifts_cross_floor_geometry():
    """Drawings with different origins must be registered by their alignment points."""
    project = build_project()
    # The upper floor's drawing is offset by 100 units, but the same physical
    # feature sits at 100 there and 0 below, so the point is directly overhead.
    project.floor("fl2").alignment = (100.0, 0.0)
    source = uptake_source()
    poi = PointOfInterest(id="poi1", floor_id="fl2", x=100, y=0, auto_height=True)
    result = distance(project, source, poi)
    assert result.horizontal_m == pytest.approx(0.0)


def test_missing_alignment_warns():
    project = build_project()
    project.floor("fl2").alignment = None
    source = uptake_source()
    poi = PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0)
    result = distance(project, source, poi)
    assert any("alignment" in w for w in result.warnings)


def test_ncrp_standoff_added_when_offset_not_applied():
    project = build_project()
    source = SourcePoint(
        id="src1", floor_id="fl1", x=0, y=0, method="ncrp147",
        params={"workload": "Rad Room (all barriers)", "barrier_type": "secondary"},
    )
    poi = PointOfInterest(id="poi1", floor_id="fl1", x=30, y=0, auto_height=False,
                          height_above_floor_m=1.0)
    without = distance(project, source, poi, apply_ncrp_standoff=False)
    with_standoff = distance(project, source, poi, apply_ncrp_standoff=True)
    assert with_standoff.metres == pytest.approx(without.metres + 0.3)
    assert any("standoff" in note for note in with_standoff.notes)


def test_check_project_reports_problems():
    project = build_project()
    project.floor("fl0").calibration = None
    project.floor("fl2").alignment = None
    project.sources.append(uptake_source())
    project.pois.append(PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0))
    problems = check_project(project)
    assert any("not calibrated" in p for p in problems)
    assert any("alignment point" in p for p in problems)
    assert any("no linked sources" in p for p in problems)


def test_engine_reproduces_example_4_end_to_end():
    """From placed points to barrier thickness, matching TG-108 Example 4."""
    project = build_project()
    project.sources.append(uptake_source())
    project.pois.append(
        PointOfInterest(
            id="poi1", floor_id="fl2", x=0, y=0, label="Room above",
            occupancy=1.0, area_class="uncontrolled", auto_height=True,
            linked_source_ids=["src1"],
        )
    )
    project.materials = ["lead", "concrete"]

    result = evaluate_point(project, project.pois[0])
    assert not result.errors
    method = result.methods[0]
    assert method.total == pytest.approx(117.0, rel=0.01)
    assert method.required_transmission == pytest.approx(0.17, rel=0.02)
    # 1.3 cm of lead, reported in millimetres.
    assert result.governing_thickness_mm["lead"] == pytest.approx(12.5, abs=1.5)


def test_engine_sums_two_sources_at_one_point():
    """Table VII behaviour: contributions add before the barrier is solved."""
    project = build_project()
    project.sources.append(uptake_source(x=0, y=0))
    project.sources.append(
        SourcePoint(
            id="src2", floor_id="fl1", x=0, y=0, label="Tomograph", method="tg108",
            height_above_floor_m=1.0,
            params={
                "kind": "imaging", "nuclide": "F-18", "administered_activity_MBq": 555,
                "patients_per_week": 40, "uptake_time_h": 1.0, "imaging_time_h": 0.5,
                "void_factor": 1.0,
            },
        )
    )
    project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, auto_height=True,
                        linked_source_ids=["src1", "src2"])
    )
    result = evaluate_point(project, project.pois[0])
    assert len(result.contributions) == 2
    total = sum(c.value for c in result.contributions)
    assert result.methods[0].total == pytest.approx(total)


def test_existing_shielding_credited_end_to_end():
    project = build_project()
    project.sources.append(uptake_source())
    project.materials = ["lead"]
    base = PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, auto_height=True,
                           linked_source_ids=["src1"])
    project.pois.append(base)
    gross = evaluate_point(project, base).governing_thickness_mm["lead"]

    base.existing_material = "lead"
    base.existing_thickness = 0.65  # cm, TG-108 material unit
    net = evaluate_point(project, base).governing_thickness_mm["lead"]
    assert net == pytest.approx(gross - 6.5, abs=0.01)


def test_point_with_no_sources_reports_error_not_crash():
    project = build_project()
    project.pois.append(PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0))
    result = evaluate_point(project, project.pois[0])
    assert result.errors and "no sources" in result.errors[0]
    assert not result.shielding_required


def test_mixed_methodologies_solved_separately():
    """TG-108 and NCRP 147 use different dose quantities and must not be summed."""
    project = build_project()
    project.sources.append(uptake_source())
    project.sources.append(
        SourcePoint(
            id="src2", floor_id="fl1", x=0, y=0, label="Rad room", method="ncrp147",
            params={"workload": "Rad Room (all barriers)", "barrier_type": "secondary"},
        )
    )
    project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, auto_height=True,
                        linked_source_ids=["src1", "src2"])
    )
    result = evaluate_point(project, project.pois[0])
    assert {m.method for m in result.methods} == {"tg108", "ncrp147"}
    assert any("solved separately" in w for w in result.warnings)
    # The governing requirement is the larger of the two, not their sum.
    per_method = [m.thickness_mm.get("lead", 0.0) for m in result.methods]
    assert result.governing_thickness_mm["lead"] == pytest.approx(max(per_method))


def test_csv_rows_include_sources_totals_and_notes():
    project = build_project()
    project.sources.append(uptake_source())
    project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, label="Office",
                        auto_height=True, linked_source_ids=["src1"])
    )
    rows = results_to_rows(evaluate_project(project), project.materials)
    kinds = {row["row_type"] for row in rows}
    assert "source" in kinds and "total" in kinds
    total_row = next(r for r in rows if r["row_type"] == "total")
    assert "lead_mm" in total_row


def test_project_round_trips_through_archive(tmp_path):
    project = build_project()
    project.sources.append(uptake_source())
    project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl2", x=5, y=7, label="Office",
                        linked_source_ids=["src1"])
    )
    path = save(project, tmp_path / "p.rsproj", {"plan.pdf": b"%PDF-1.4 fake"})

    reloaded, pdfs = load(path)
    assert reloaded.name == project.name
    assert pdfs["plan.pdf"] == b"%PDF-1.4 fake"
    assert reloaded.floor("fl1").calibration.metres_per_unit == pytest.approx(0.1)
    assert reloaded.floor("fl1").alignment == (0.0, 0.0)
    assert reloaded.poi("poi1").linked_source_ids == ["src1"]
    assert reloaded.pois[0].x == 5


def test_saving_without_pdf_content_is_rejected(tmp_path):
    project = build_project()
    with pytest.raises(ValueError, match="missing PDF"):
        save(project, tmp_path / "p.rsproj", {})


def test_removing_a_floor_unlinks_its_sources():
    project = build_project()
    project.sources.append(uptake_source())
    project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, linked_source_ids=["src1"])
    )
    project.remove_floor("fl1")
    assert not project.sources
    assert project.poi("poi1").linked_source_ids == []
