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
| 1. Physics engines + tests | **complete** — 78 tests passing |
| 2. PDF load, render, calibrate | not started |
| 3. Point placement and metadata | not started |
| 4. Multi-floor 3D geometry | not started |
| 5. Results, audit trail, export | not started |

## Install

```bash
pip install -e .
python -m pytest
```

No runtime dependencies. PDF and web extras are declared but unused so far.

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
