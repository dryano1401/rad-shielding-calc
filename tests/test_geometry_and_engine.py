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


def test_same_floor_ignores_a_difference_in_entered_heights():
    """A beam height and an occupied height are independent entries, not an
    implied floor-to-floor gap -- only a real change of floor is that."""
    project = build_project()
    source = uptake_source()  # height_above_floor_m=1.0
    poi = PointOfInterest(id="poi1", floor_id="fl1", x=40, y=0, auto_height=False,
                          height_above_floor_m=1.7)
    result = distance(project, source, poi)
    assert result.vertical_m == pytest.approx(0.0)
    assert result.metres == pytest.approx(result.horizontal_m)
    assert any("same floor" in n for n in result.notes)

    # auto_height on the same floor takes the point's own entered height too,
    # so it is equally unaffected.
    poi.auto_height = True
    auto = distance(project, source, poi)
    assert auto.vertical_m == pytest.approx(0.0)
    assert auto.metres == pytest.approx(auto.horizontal_m)


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


def test_standoff_applies_to_tg108_sources_too():
    """TG-108's own default distances are drawn from NCRP guidance, so the
    0.3 m point-of-protection standoff is not an NCRP-147-only concept -- a
    TG-108 source and an NCRP 147 source at the same spot must read the same
    distance to a point once the standoff is (or isn't) accounted for.
    """
    project = build_project()
    tg108 = uptake_source(x=0, y=0)
    ncrp = SourcePoint(
        id="src2", floor_id="fl1", x=0, y=0, method="ncrp147",
        params={"workload": "Rad Room (all barriers)", "barrier_type": "secondary"},
    )
    project.sources.extend([tg108, ncrp])
    poi = PointOfInterest(
        id="poi1", floor_id="fl1", x=30, y=0, auto_height=False, height_above_floor_m=1.0,
        offset_applied=False, linked_source_ids=["src1", "src2"],
    )
    project.pois.append(poi)

    bare = distance(project, tg108, poi, apply_ncrp_standoff=False)

    result = evaluate_point(project, poi)
    by_id = {c.source_id: c for c in result.contributions}
    # Co-located sources on the same floor: bare geometry is identical, so the
    # two contributions must differ from the bare distance by exactly the
    # standoff, and agree with each other -- not be exempt for one and not
    # the other.
    assert by_id["src1"].distance_m == pytest.approx(bare.metres + 0.3)
    assert by_id["src2"].distance_m == pytest.approx(bare.metres + 0.3)
    assert any("standoff" in note for note in by_id["src1"].notes)
    assert any("standoff" in note for note in by_id["src2"].notes)

    poi.offset_applied = True
    result_applied = evaluate_point(project, poi)
    by_id_applied = {c.source_id: c for c in result_applied.contributions}
    assert by_id_applied["src1"].distance_m == pytest.approx(bare.metres)
    assert not any("standoff" in note for note in by_id_applied["src1"].notes)


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
            # This point marks Example 4's own stated distance, not a
            # barrier surface, so the NCRP standoff should not stack on it.
            offset_applied=True,
        )
    )
    project.materials = ["lead", "concrete"]

    result = evaluate_point(project, project.pois[0])
    assert not result.errors
    method = result.methods[0]
    assert method.total == pytest.approx(117.0, rel=0.01)
    # No walls on this path, so the unshielded and shielded totals coincide.
    assert method.unshielded_total == pytest.approx(method.total)
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


def test_mixed_methodologies_are_summed():
    """1 uGy and 1 uSv coincide for these photon energies, so a point seeing
    both source types needs a wall thick enough for their combined dose --
    solving each alone and taking the larger requirement can understate that.
    """
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
    assert {m.method for m in result.methods} == {"tg108", "ncrp147", "combined"}
    assert any("summed" in w for w in result.warnings)

    combined = next(m for m in result.methods if m.method == "combined")
    tg108_method = next(m for m in result.methods if m.method == "tg108")
    ncrp147_method = next(m for m in result.methods if m.method == "ncrp147")
    # 1 uGy = 1 uSv for these photon energies, so the combined unshielded
    # total is the plain sum once NCRP 147's mGy is converted to uSv.
    assert combined.unshielded_total == pytest.approx(
        tg108_method.unshielded_total + ncrp147_method.unshielded_total * 1000.0
    )
    per_method = [m.thickness_mm.get("lead", 0.0) for m in result.methods if m.method != "combined"]
    # At the thicker of the two independent requirements, the methodology
    # that set it is exactly at the weekly goal by construction; the other
    # methodology's dose still gets through on top of that, so meeting the
    # combined goal takes strictly more than either alone -- never just the
    # larger of the two, and never their plain sum either (transmission has
    # already knocked the harder one down to size).
    assert combined.thickness_mm["lead"] > max(per_method)
    assert result.governing_thickness_mm["lead"] == pytest.approx(combined.thickness_mm["lead"])


def test_mixed_methodologies_mark_single_methodology_materials_unavailable():
    """Iron has no NCRP 147 data and gypsum has no TG-108 data in this app, so
    once both source types are present at a point, a combined requirement
    can't be verified for either -- reporting one anyway would silently
    ignore whichever dose that material has no data for."""
    project = build_project()
    project.materials = ["lead", "iron", "gypsum"]
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
    combined = next(m for m in result.methods if m.method == "combined")
    assert "iron" in combined.unavailable
    assert "gypsum" in combined.unavailable
    assert "lead" in combined.thickness_mm
    assert "iron" not in result.governing_thickness_mm
    assert "gypsum" not in result.governing_thickness_mm
    assert "lead" in result.governing_thickness_mm


def test_csv_rows_are_one_per_point_with_the_governing_totals():
    project = build_project()
    project.sources.append(uptake_source())
    project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl2", x=0, y=0, label="Office",
                        auto_height=True, linked_source_ids=["src1"], offset_applied=True)
    )
    rows = results_to_rows(evaluate_project(project), project.materials)
    assert len(rows) == 1
    row = rows[0]
    assert row["point"] == "Office"
    assert row["source"] == "Uptake room"
    assert "lead_mm_required" in row
    assert row["unshielded_dose_rate"] == pytest.approx(117.0, rel=0.01)
    assert row["shielded_dose_rate"] == pytest.approx(117.0, rel=0.01)
    assert row["goal_P"] == pytest.approx(20.0, rel=0.01)
    assert row["pct_of_goal"] == pytest.approx(100.0 / 0.17, rel=0.02)


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
