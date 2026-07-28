"""Wall barriers: 3D path intersection, obliquity, stacking and attenuation."""

from __future__ import annotations

import math

import pytest

from radshield.engine.evaluate import describe_barriers, evaluate_point
from radshield.model.geometry import path_barriers
from radshield.model.project import Barrier, PointOfInterest, Project, Wall
from radshield.model.store import load, save
from radshield.physics.archer import ArcherParams, equivalent_thickness, transmission

from .test_geometry_and_engine import build_project, uptake_source


def add_wall(project: Project, floor_id: str, **kwargs) -> Wall:
    """Attach a wall to a floor and return it.

    The test project scales 1 PDF unit to 0.1 m, so a wall drawn from
    (0, -50) to (0, 50) is a 10 m run on the x = 0 line.
    """
    defaults = dict(
        id=kwargs.pop("id", "w1"),
        p1=(0.0, -50.0),
        p2=(0.0, 50.0),
        material="concrete",
        thickness_mm=200.0,
        base_height_m=0.0,
        top_height_m=3.0,
    )
    defaults.update(kwargs)
    wall = Wall(**defaults)
    project.floor(floor_id).walls.append(wall)
    return wall


def horizontal_pair(project: Project) -> tuple:
    """A source and point on the same floor, 8 m apart, straddling x = 0."""
    source = uptake_source(floor_id="fl1", x=-40.0, y=0.0)
    poi = PointOfInterest(
        id="poi1", floor_id="fl1", x=40.0, y=0.0, auto_height=False,
        height_above_floor_m=1.0, linked_source_ids=["src1"],
    )
    return source, poi


def test_wall_rejects_impossible_geometry():
    with pytest.raises(ValueError, match="thickness must be positive"):
        Wall(id="w", p1=(0, 0), p2=(10, 0), thickness_mm=0)
    with pytest.raises(ValueError, match="must be above its base"):
        Wall(id="w", p1=(0, 0), p2=(10, 0), base_height_m=3.0, top_height_m=2.0)


def test_path_crossing_a_wall_is_detected():
    project = build_project()
    add_wall(project, "fl1")
    source, poi = horizontal_pair(project)
    crossings, warnings = path_barriers(project, source, poi)
    assert not warnings
    assert len(crossings) == 1
    assert crossings[0].material == "concrete"
    assert crossings[0].thickness_mm == 200.0


def test_path_missing_the_wall_end_is_not_blocked():
    """A wall only 1 m long does not block a path passing well clear of it."""
    project = build_project()
    add_wall(project, "fl1", p1=(0.0, 200.0), p2=(0.0, 210.0))
    source, poi = horizontal_pair(project)
    crossings, _ = path_barriers(project, source, poi)
    assert crossings == []


def test_path_parallel_to_a_wall_does_not_cross_it():
    project = build_project()
    add_wall(project, "fl1", p1=(-100.0, 20.0), p2=(100.0, 20.0))
    source, poi = horizontal_pair(project)  # runs along y = 0, wall along y = 2 m
    crossings, _ = path_barriers(project, source, poi)
    assert crossings == []


def test_wall_behind_the_source_does_not_count():
    """Only barriers between the two points matter, not ones beyond them."""
    project = build_project()
    add_wall(project, "fl1", p1=(-800.0, -50.0), p2=(-800.0, 50.0))
    source, poi = horizontal_pair(project)
    crossings, _ = path_barriers(project, source, poi)
    assert crossings == []


def test_two_walls_on_a_path_are_both_counted():
    project = build_project()
    add_wall(project, "fl1", id="w1", p1=(-10.0, -50.0), p2=(-10.0, 50.0))
    add_wall(project, "fl1", id="w2", p1=(10.0, -50.0), p2=(10.0, 50.0), material="lead",
             thickness_mm=2.0)
    source, poi = horizontal_pair(project)
    crossings, _ = path_barriers(project, source, poi)
    assert {c.material for c in crossings} == {"concrete", "lead"}


# --- cross-floor behaviour, which is what the wall height is for -------------

def test_short_wall_does_not_block_a_path_to_the_floor_above():
    """A 3 m partition is below a ray that has already climbed past it.

    Source at (-4 m, z = 1.0) rising to (+4 m, z = 4.8) on the floor above.
    At the wall's line, x = +2 m, the ray is at z = 3.85 m -- clear of a
    partition topping out at 3 m.
    """
    project = build_project()
    add_wall(project, "fl1", p1=(20.0, -50.0), p2=(20.0, 50.0), top_height_m=3.0)
    source = uptake_source(floor_id="fl1", x=-40.0, y=0.0)
    poi = PointOfInterest(id="poi1", floor_id="fl2", x=40.0, y=0.0, auto_height=True)
    crossings, _ = path_barriers(project, source, poi)
    assert crossings == []


def test_full_height_shaft_wall_does_block_the_path_above():
    """The identical path is blocked by a wall carried up to the storey above."""
    project = build_project()
    add_wall(project, "fl1", p1=(20.0, -50.0), p2=(20.0, 50.0), top_height_m=8.0)
    source = uptake_source(floor_id="fl1", x=-40.0, y=0.0)
    poi = PointOfInterest(id="poi1", floor_id="fl2", x=40.0, y=0.0, auto_height=True)
    crossings, _ = path_barriers(project, source, poi)
    assert len(crossings) == 1
    assert crossings[0].floor_name == "Source"


def test_wall_on_the_upper_floor_blocks_a_path_arriving_there():
    """Walls are tested on every floor, not just the source's.

    At x = +3.5 m the ray has reached z = 4.56 m, which is inside the upper
    floor's partition band of 4.3 to 7.3 m.
    """
    project = build_project()
    add_wall(project, "fl2", p1=(35.0, -50.0), p2=(35.0, 50.0), base_height_m=0.0,
             top_height_m=3.0)
    source = uptake_source(floor_id="fl1", x=-40.0, y=0.0)
    poi = PointOfInterest(id="poi1", floor_id="fl2", x=40.0, y=0.0, auto_height=True)
    crossings, _ = path_barriers(project, source, poi)
    assert len(crossings) == 1
    assert crossings[0].floor_name == "Above"


def test_wall_starting_above_the_ray_is_missed():
    """A clerestory panel starting at 2.5 m is above a path running at 1 m."""
    project = build_project()
    add_wall(project, "fl1", base_height_m=2.5, top_height_m=4.0)
    source, poi = horizontal_pair(project)
    crossings, _ = path_barriers(project, source, poi)
    assert crossings == []


# --- obliquity ---------------------------------------------------------------

def test_perpendicular_crossing_has_no_obliquity_penalty():
    project = build_project()
    add_wall(project, "fl1")
    source, poi = horizontal_pair(project)
    crossings, _ = path_barriers(project, source, poi, apply_obliquity=True)
    assert crossings[0].angle_deg == pytest.approx(0.0, abs=0.01)
    assert crossings[0].effective_thickness_mm == pytest.approx(200.0)
    assert not crossings[0].is_oblique


def test_oblique_crossing_traverses_more_material_when_enabled():
    """At 45 degrees the path sees thickness / cos(45) = 1.414 x the nominal."""
    project = build_project()
    add_wall(project, "fl1")
    source = uptake_source(floor_id="fl1", x=-40.0, y=-40.0)
    poi = PointOfInterest(id="poi1", floor_id="fl1", x=40.0, y=40.0, auto_height=False,
                          height_above_floor_m=1.0)

    off, _ = path_barriers(project, source, poi, apply_obliquity=False)
    on, _ = path_barriers(project, source, poi, apply_obliquity=True)

    assert on[0].angle_deg == pytest.approx(45.0, abs=0.5)
    assert off[0].effective_thickness_mm == pytest.approx(200.0)
    assert on[0].effective_thickness_mm == pytest.approx(200.0 * math.sqrt(2), rel=0.01)
    assert on[0].is_oblique
    # Ignoring obliquity under-counts material, so the default errs safe.
    assert off[0].effective_thickness_mm < on[0].effective_thickness_mm


# --- manual barriers ---------------------------------------------------------

def test_named_barriers_apply_to_one_source_only():
    """Different sources reach a point through different structures."""
    project = build_project()
    source_a = uptake_source(floor_id="fl1", x=-40.0, y=0.0)
    source_b = uptake_source(floor_id="fl1", x=40.0, y=0.0)
    object.__setattr__(source_b, "id", "src2")
    poi = PointOfInterest(
        id="poi1", floor_id="fl1", x=0.0, y=80.0, auto_height=False,
        height_above_floor_m=1.0, linked_source_ids=["src1", "src2"],
        manual_barriers={"src1": [Barrier(material="lead", thickness_mm=3.0, label="Door")]},
    )
    from_a, _ = path_barriers(project, source_a, poi)
    from_b, _ = path_barriers(project, source_b, poi)
    assert [c.label for c in from_a] == ["Door"]
    assert from_b == []


def test_named_barriers_stack_with_drawn_walls():
    project = build_project()
    add_wall(project, "fl1")
    source, poi = horizontal_pair(project)
    poi.manual_barriers = {"src1": [Barrier(material="lead", thickness_mm=2.0, label="Glazing")]}
    crossings, _ = path_barriers(project, source, poi)
    assert len(crossings) == 2
    assert {c.label for c in crossings} == {"concrete wall", "Glazing"}


# --- effect on the calculation ----------------------------------------------

def evaluate_with(project: Project, poi: PointOfInterest) -> float:
    """Weekly dose at the point after path attenuation."""
    return evaluate_point(project, poi).methods[0].total


def test_a_wall_reduces_the_dose_and_the_required_thickness():
    project = build_project()
    project.materials = ["lead"]
    source, poi = horizontal_pair(project)
    project.sources.append(source)
    project.pois.append(poi)

    unshielded = evaluate_with(project, poi)
    required_bare = evaluate_point(project, poi).governing_thickness_mm["lead"]

    add_wall(project, "fl1")
    shielded = evaluate_with(project, poi)
    result = evaluate_point(project, poi)

    assert shielded < unshielded
    assert result.governing_thickness_mm["lead"] < required_bare
    contribution = result.contributions[0]
    assert contribution.unshielded_value == pytest.approx(unshielded)
    assert contribution.path_transmission < 1.0
    assert contribution.path_equivalent_mm > 0
    assert contribution.barriers[0]["material"] == "concrete"


def test_attenuation_matches_the_equivalent_thickness_calculation():
    """The engine's path transmission must equal the hand calculation."""
    project = build_project()
    project.materials = ["lead"]
    source, poi = horizontal_pair(project)
    project.sources.append(source)
    project.pois.append(poi)
    add_wall(project, "fl1", material="concrete", thickness_mm=200.0)

    concrete = ArcherParams(0.1539, -0.1161, 2.0752, "cm", "concrete")
    lead = ArcherParams(1.543, -0.4408, 2.136, "cm", "lead")
    lead_equivalent_cm = equivalent_thickness(concrete, 20.0, lead)  # 200 mm = 20 cm
    expected_b = transmission(lead, lead_equivalent_cm)

    contribution = evaluate_point(project, poi).contributions[0]
    assert contribution.path_transmission == pytest.approx(expected_b, rel=1e-9)
    assert contribution.path_equivalent_mm == pytest.approx(lead_equivalent_cm * 10, rel=1e-9)


def test_enough_wall_removes_the_requirement_entirely():
    """Verification falls out of the hybrid: a thick enough wall needs no more."""
    project = build_project()
    project.materials = ["lead"]
    source, poi = horizontal_pair(project)
    project.sources.append(source)
    project.pois.append(poi)
    add_wall(project, "fl1", material="lead", thickness_mm=30.0)

    result = evaluate_point(project, poi)
    assert not result.shielding_required
    assert result.governing_thickness_mm["lead"] == 0.0


def test_sources_behind_different_walls_are_attenuated_separately():
    """The headline case: one source shielded, a nearby one not."""
    project = build_project()
    project.materials = ["lead"]
    shielded_source = uptake_source(floor_id="fl1", x=-40.0, y=0.0)
    open_source = uptake_source(floor_id="fl1", x=40.0, y=0.0)
    object.__setattr__(open_source, "id", "src2")
    object.__setattr__(open_source, "label", "Unshielded source")
    project.sources += [shielded_source, open_source]

    poi = PointOfInterest(
        id="poi1", floor_id="fl1", x=0.0, y=80.0, auto_height=False,
        height_above_floor_m=1.0, linked_source_ids=["src1", "src2"],
    )
    project.pois.append(poi)
    # A wall between the first source and the point only.
    add_wall(project, "fl1", p1=(-30.0, 30.0), p2=(-10.0, 50.0), material="lead",
             thickness_mm=5.0)

    result = evaluate_point(project, poi)
    by_id = {c.source_id: c for c in result.contributions}
    assert by_id["src1"].path_transmission < 1.0
    assert by_id["src2"].path_transmission == 1.0
    # Same source strength and distance, so the wall is the only difference.
    assert by_id["src1"].value < by_id["src2"].value
    assert by_id["src1"].unshielded_value == pytest.approx(by_id["src2"].unshielded_value)


def test_unsupported_wall_material_is_dropped_with_a_warning():
    """TG-108 has no 511 keV data for gypsum; ignoring it is conservative."""
    project = build_project()
    project.materials = ["lead"]
    source, poi = horizontal_pair(project)
    project.sources.append(source)
    project.pois.append(poi)
    add_wall(project, "fl1", material="gypsum", thickness_mm=16.0)

    contribution = evaluate_point(project, poi).contributions[0]
    assert contribution.path_transmission == 1.0
    assert any("gypsum" in note and "ignored" in note for note in contribution.notes)


def test_obliquity_toggle_changes_the_answer():
    project = build_project()
    project.materials = ["lead"]
    # Close enough that a barrier is genuinely required: d = 2.83 m, B = 0.094.
    source = uptake_source(floor_id="fl1", x=-10.0, y=-10.0)
    poi = PointOfInterest(id="poi1", floor_id="fl1", x=10.0, y=10.0, auto_height=False,
                          height_above_floor_m=1.0, linked_source_ids=["src1"])
    project.sources.append(source)
    project.pois.append(poi)
    add_wall(project, "fl1", thickness_mm=50.0)

    project.apply_obliquity = False
    straight = evaluate_point(project, poi)
    project.apply_obliquity = True
    oblique = evaluate_point(project, poi)

    # More material traversed means more attenuation and less to add.
    assert oblique.contributions[0].path_transmission < straight.contributions[0].path_transmission
    assert oblique.governing_thickness_mm["lead"] < straight.governing_thickness_mm["lead"]


def test_describe_barriers_lists_the_path_without_calculating():
    project = build_project()
    source, poi = horizontal_pair(project)
    project.sources.append(source)
    project.pois.append(poi)
    add_wall(project, "fl1", label="Corridor wall")
    poi.manual_barriers = {"src1": [Barrier(material="lead", thickness_mm=2.0, label="Door")]}

    links = describe_barriers(project)[0]["links"]
    assert links[0]["label"] == "Uptake room"
    labels = [b["label"] for b in links[0]["barriers"]]
    assert labels == ["Corridor wall", "Door"]
    assert links[0]["barriers"][0]["drawn"] is True
    assert links[0]["barriers"][1]["drawn"] is False


def test_walls_and_barriers_survive_a_save_and_reload(tmp_path):
    project = build_project()
    add_wall(project, "fl1", label="North wall", material="steel", thickness_mm=6.0,
             top_height_m=4.2)
    project.pois.append(
        PointOfInterest(id="poi1", floor_id="fl1", x=0, y=0,
                        manual_barriers={"src1": [Barrier("lead", 2.5, "Door")]})
    )
    project.apply_obliquity = True

    path = save(project, tmp_path / "p.rsproj", {"plan.pdf": b"%PDF-1.4 fake"})
    reloaded, _ = load(path)

    wall = reloaded.floor("fl1").walls[0]
    assert (wall.label, wall.material, wall.thickness_mm, wall.top_height_m) == (
        "North wall", "steel", 6.0, 4.2)
    assert reloaded.poi("poi1").manual_barriers["src1"][0].label == "Door"
    assert reloaded.apply_obliquity is True


def test_projects_saved_before_walls_existed_still_load():
    project = build_project()
    data = project.to_dict()
    for floor in data["floors"]:
        floor.pop("walls")
    data.pop("apply_obliquity")
    restored = Project.from_dict(data)
    assert restored.floor("fl1").walls == []
    assert restored.apply_obliquity is False
