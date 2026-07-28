# rad-shielding-calc

Radiation shielding calculations for medical imaging facilities, implementing
**NCRP Report 147** (x-ray, fluoroscopy, CT) and **AAPM TG-108** (PET and
nuclear medicine).

The end goal is an application that loads architectural PDFs, calibrates a
real-world scale per floor, places sources and points of interest on the
drawing, and computes required barrier thickness including across floors.
This repository currently contains the calculation core.

## Status

| Phase | State |
|---|---|
| 1. Physics engines + tests | **complete** |
| 2. PDF load, render, calibrate | **complete** |
| 3. Point placement and metadata | **complete** |
| 4. Multi-floor 3D geometry | **complete** |
| 5. Results, audit trail, CSV export | **complete** |
| 6. NCRP 147 CT scatter constants | needs source data |
| 7. Barrier objects, report export | not started |

145 tests passing, including TG-108 Examples 1-6 and Tables IV, VII and VIII.

## Install and run

```bash
pip install -e ".[web]"
python -m radshield.web        # opens http://127.0.0.1:8000/
```

`radshield.physics` has no dependencies at all; FastAPI and PyMuPDF are only
needed for the GUI.

## Workflow

1. **Add floors.** Upload one PDF per floor. Set elevations directly, or enter
   floor-to-floor heights (one per gap, lowest first) and apply them to the stack.
2. **Set the scale.** Pick the *Set scale* tool and click two points a known
   distance apart, then type the distance with its unit ("40 ft", "12.5 m").
   Each floor is calibrated independently, so drawings may be at different scales.
3. **Set an alignment point.** Click the same physical feature — a column, stair
   core, lift shaft — on every floor. Each PDF has its own arbitrary origin, so
   without this, horizontal distances *between* floors are meaningless. The app
   warns when it is missing.
4. **Measure anything.** The *Measure* tool reports the distance between any two
   clicked points — a wall standoff, a room width — with a live readout as you
   drag out the second point. Naming a measurement keeps it on the drawing and in
   the saved project; Esc cancels.
5. **Place points.** Add sources and points of interest, drag to adjust. A new
   point of interest links to every existing source by default; edit the links
   in the inspector.
6. **Check the distances.** Each linked source shows its source-to-point distance,
   broken into horizontal and vertical components, before you calculate. Type a
   value into the box to override the drawing geometry — useful when the path is
   not what the plan implies. The entered figure and the geometric one it replaced
   both appear in the results and the CSV, and a discrepancy over 25% is flagged.
7. **Calculate.** Every source linked to a point contributes and their doses are
   summed before the barrier is solved. Expand the detail row to see every
   intermediate value, or export the CSV.

Distances display in feet-and-inches or metres — the toggle is in the header.
The metric value is always shown alongside, since the physics works in metres.

## Use

```python
from radshield.physics.limits import tg108_goal
from radshield.physics.tg108 import PatientSource, solve_barrier

uptake = PatientSource(kind="uptake", nuclide="F-18",
                       administered_activity_MBq=555, patients_per_week=40,
                       uptake_time_h=1.0, label="Uptake room")
scanner = PatientSource(kind="imaging", nuclide="F-18",
                        administered_activity_MBq=555, patients_per_week=40,
                        uptake_time_h=1.0, imaging_time_h=0.5, label="Tomograph")

result = solve_barrier(
    sources=[(uptake, 8.0), (scanner, 3.0)],   # (source, distance in metres)
    goal=tg108_goal("uncontrolled"),
    occupancy=1.0,
    materials=["lead", "concrete"],
)

print(result.total_weekly_dose_uSv)          # summed over both sources
print(result.required_transmission)
print(result.thickness_by_material)          # {'lead': cm, 'concrete': cm}
for dose in result.per_source:
    print("\n".join(dose.audit_lines()))     # every intermediate value
```

## Design

`radshield.physics` imports nothing from the application layer — no PDF
library, no web framework, no file I/O beyond its own CSV tables. That is what
lets it be validated against published examples and reused elsewhere.

Published data lives in `physics/data/*.csv` rather than in Python literals, so
values can be audited and extended without touching code. Isotopes absent from
TG-108 are added by registering a nuclide and its Archer parameters; the
calculation path does not change.

Every result carries its inputs and intermediate values, so the output is
auditable for a physics report rather than being a bare number.

## Documentation

- `docs/methodology.md` — equations, constants, data provenance, known gaps,
  and two discrepancies found within TG-108 itself
- `docs/PLAN.md` — architecture and phased build plan
