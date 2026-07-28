# Radiation Shielding Calculation Application — Planning Document

Status: **planning only, no implementation code written yet.**

This document is the deliverable of the planning step: clarifying questions, proposed
architecture, data model, file structure, and a phased build plan.

---

## 0. What I have and haven't got

**Have:** AAPM TG-108 (Madsen et al., *Med Phys* 33(1), 2006) in full. I've extracted the
methodology, constants, fit parameters, and all six worked examples — enough to implement
and unit-test the PET/PET-CT engine against published values without guessing anything.

**Haven't got:** NCRP Report 147, and the existing Python shielding work referenced in the
spec. This matters more than it might appear — see Q2 below.

---

## 1. Clarifying questions

Each question carries a **recommended default** so implementation isn't blocked while
waiting for answers.

### Q1 — App type: desktop, local web, or Streamlit?

The repo currently holds a blank Streamlit template. That's a meaningful signal about
intended deployment, but Streamlit is the weakest of the three for this specific
interaction model. Tradeoffs:

| | Interaction quality | Deploy/share | Install burden |
|---|---|---|---|
| **FastAPI + JS canvas** | Excellent — full pointer-event control | Local now, hostable later | `pip install`, opens browser |
| **PyQt6 desktop** | Best — `QGraphicsView` is purpose-built for this | Local only | Heavy (~100 MB Qt) |
| **Streamlit** | Poor for this task | Trivial (Streamlit Cloud) | Lightest |

The deciding factor is that this app needs *sustained* canvas interaction: click-to-place,
**drag-to-adjust** an already-placed point, pan/zoom on a large drawing, click-two-points
calibration, and drawn link lines between sources and POIs. Streamlit's model —
`streamlit-image-coordinates` returns only the last click, and every interaction triggers a
full script rerun — makes drag and pan/zoom awkward to the point of being a daily
annoyance. It's viable *only* if you accept click-to-place-then-retype-coordinates instead
of dragging.

**Recommendation: FastAPI + PyMuPDF server-side rendering + a vanilla-JS canvas
front-end.** PyQt6 gives marginally better interaction but loses browser-based sharing and
adds a heavy dependency. Critically, since the physics package is UI-agnostic by design
(§2), a Streamlit front-end can be added later over the same engine if you want the
free-cloud-deploy path for a simplified view.

### Q2 — Reuse your existing TG-108 / NCRP 147 code? (highest-impact question)

Please share it. Specifically for **NCRP 147**, the method depends on large published data
tables that I should not reconstruct from memory for a report that has to be defensible:

- Archer fit parameters α, β, γ for lead and concrete at each kVp, for primary, scatter,
  and leakage radiation (three separate parameter sets per kVp per material)
- K₁ᴾ unshielded primary air kerma per patient at 1 m
- Workload distributions W_norm and per-patient workloads by room type
- Scatter fractions and leakage-radiation technique-factor assumptions
- Occupancy factor table (Table B.1)

If I write these from memory, some values will be subtly wrong, and a subtly wrong table is
worse than no table — it produces plausible numbers that fail peer review. Same reasoning
applies to your bounding-box screener: porting preserves conventions you've already
validated.

TG-108 is a different story. The paper gives everything needed, so that engine can be built
correctly today regardless of what you send.

**Recommendation:** Port from your code for NCRP 147; build TG-108 fresh from the paper
(cross-checking against yours if you send it).

### Q3 — "TG-108 for PET/SPECT" — SPECT isn't in scope of TG-108

Worth flagging before it becomes a wrong-methodology bug. TG-108 is titled *PET and PET/CT
Shielding Requirements* and its methodology is specific to 511 keV annihilation photons
from an ambulatory patient-as-source. It does **not** provide a SPECT/Tc-99m barrier
method. Where the report discusses gamma cameras, it's addressing the reverse problem —
protecting the *camera's image quality* from nearby PET patients, not protecting personnel
from a SPECT patient.

For Tc-99m / I-131 / general nuclear medicine, the same mathematical form works (dose-rate
constant × activity × time × decay ÷ d²), but with different nuclide constants, different
patient self-attenuation assumptions, and typically NCRP 49 / NCRP 151-era guidance for
barrier transmission. So:

- **(a)** Do you want a genuine SPECT method, implemented as a clearly-labeled
  *TG-108-style generalization* with per-nuclide constants (and which source guidance —
  NCRP 49? your own established practice?), **or**
- **(b)** is PET/PET-CT sufficient for v1?

**Recommendation:** Build the nuclear-medicine engine generalized over a nuclide registry
from the start, so Tc-99m works mechanically, but label non-F-18 results as
"TG-108 method extended to \<nuclide\>" rather than implying TG-108 endorses it.

### Q4 — Sources sum at a POI; they don't just pair

The spec says each POI "supports pairing with one or more source points." TG-108 Table VII
shows what that has to mean numerically: for each office, the weekly dose from the **uptake
room** and from the **tomograph room** are computed separately and **summed**, and a single
transmission factor is derived from the total:

> Office 1: 27.1 µSv (uptake) + 70.1 µSv (tomograph) = 97.2 µSv → B = 0.206

So the barrier requirement is a property of the **POI**, not of a source-POI pair. This
drives the data model (§3): the engine aggregates dose over all linked sources at each POI,
then solves for thickness once.

One caveat this raises: summation is only physically correct when the *same* barrier lies
between the POI and every source being summed. If two sources reach a POI through different
walls, summing over-shields. **Question:** do you want (a) simple summation with a warning
when linked sources are on different bearings, or (b) explicit barrier objects that sources
are assigned to?

**Recommendation:** (a) for v1 — matches TG-108's own worked example, keeps the UI simple.
Barrier objects are the natural v2 upgrade and the data model should leave room for them.

### Q5 — Where exactly is the point of interest?

Distance conventions differ between the two methods and are a classic source of error:

- **TG-108** (Fig. 5): source assumed **1 m above the floor**; target **0.5 m above the
  floor** for the room above; **1.7 m above the floor** for the room below. So for a 4.3 m
  floor-to-floor height, `d = 4.3 − 1 + 0.5 = 3.8 m` (above) and
  `d = 4.3 + 1 − 1.7 = 3.6 m` (below) — Examples 4 and 5.
- **NCRP 147** places the point of protection **0.3 m beyond** the distal side of the
  barrier.

**Question:** should the app apply these conventions automatically from a click position
(you click the wall/room, app adds the offsets), or do you want to place the protected
point literally and have the app use raw geometry?

**Recommendation:** automatic, method-aware, with every applied offset shown explicitly in
the audit output — never silently.

### Q6 — Units

**Recommendation: US customary on the surface, SI underneath.** Calibrate in feet, see
results in mm Pb and inches of concrete, but compute entirely in SI (m, MBq, µSv) so the
TG-108 equations are used exactly as published with no embedded conversion factors. A
single conversion boundary at the UI edge is far easier to test than conversions scattered
through the physics.

### Q7 — Remaining smaller items

1. **Design goal P.** TG-108 uses 20 µSv/wk uncontrolled, 100 µSv/wk controlled (ALARA).
   Configurable per project, or fixed? (*Rec: configurable, defaulting to these.*)
2. **Existing structural shielding credit.** Example 4 credits 10 cm of concrete floor
   (≈ 6.5 mm Pb) and reports only the *additional* barrier needed. Should every barrier
   carry a "existing construction" field? (*Rec: yes — it's what makes results actionable.*)
3. **Scanner self-shielding.** TG-108 notes the gantry realistically gives ~15% reduction.
   Optional per-source factor? (*Rec: yes, default 1.0 = no credit, i.e. conservative.*)
4. **Obliquity.** Slant paths through a wall traverse more than the wall's nominal
   thickness. Correct for it, or ignore (conservative for thickness *required*, but relevant
   for floors)? (*Rec: ignore in v1, note it in the audit output.*)
5. **Export format.** CSV only, or XLSX / formatted PDF report? (*Rec: CSV in v1, XLSX
   next — you already have an `xlsx` skill available.*)
6. **Multi-page PDFs.** One floor per PDF, or one floor per *page* of a multi-page PDF?
   (*Rec: support both; a floor references `(file, page_index)`.*)

---

## 2. Proposed architecture

### Layering

The hard rule: **`radshield.physics` imports nothing from the app.** No FastAPI, no PyMuPDF,
no file I/O, no unit strings. Pure functions over floats and dataclasses. That's what makes
it unit-testable against published examples and reusable in your other tools.

```
┌──────────────────────────────────────────────┐
│  web/  FastAPI + JS canvas                   │  clicks, rendering, zoom
├──────────────────────────────────────────────┤
│  engine/  binds project model → physics      │  aggregation, unit conversion
├──────────────────────────────────────────────┤
│  model/  floors, points, calibration, geom   │  persistence, 3D distance
├──────────────────────────────────────────────┤
│  physics/  TG-108, NCRP 147, Archer, tables  │  PURE. no I/O. no UI.
└──────────────────────────────────────────────┘
```

### Why server-side PDF rendering

PyMuPDF rasterizes a page to PNG at a chosen DPI and hands it to the canvas. The
alternative — PDF.js in the browser — means the click-coordinate mapping lives in JS and
Python never sees the page geometry. Server-side keeps one coordinate authority in Python,
where the calculations are.

### Coordinate strategy (important)

**Store every point in native PDF page coordinates (points, 1/72"), never in screen pixels
and never in calibrated real-world units.**

- Screen pixels break the moment you zoom or re-render at different DPI.
- Real-world units mean **re-calibrating a floor silently moves every point on it**.

PDF coordinates are the stable ground truth; calibration is a separate scalar applied at
calculation time. Recalibrate freely, points stay put, distances update. Screen ↔ PDF is a
pure affine transform from the current zoom/pan.

### The one 511 keV gotcha, recorded now

TG-108 Table IV lists lead thickness in **mm** and concrete/iron in **cm**, but the Table V
Archer fit parameters are in **cm⁻¹ for all three materials**. Mixing these produces results
off by 10×. Verified against the paper:

```
Archer:  B = [(1 + β/α)·e^(αγx) − β/α]^(−1/γ)     x in cm
Lead, x = 0.5 cm:  α=1.543, β=−0.4408, γ=2.136  →  B = 0.523
Table IV, 5 mm:                                     B = 0.5227   ✓
```

The materials module will carry explicit unit metadata per material, and a regression test
will pin the full Table IV grid against the Archer inversion.

---

## 3. Data model

Project file `*.rsproj` = a zip containing `project.json` plus the embedded source PDFs, so
a project is a single portable artifact (no broken paths when moved between machines).

```jsonc
{
  "schema_version": 1,
  "name": "Memorial Hospital PET Suite",
  "settings": {
    "display_units": "us",              // us | si
    "design_goal_uncontrolled_uSv_wk": 20,
    "design_goal_controlled_uSv_wk": 100
  },

  "floors": [
    {
      "id": "fl_below",
      "name": "Level 1 — Radiology",
      "pdf": { "file": "pdfs/level1.pdf", "page": 0 },
      "elevation_m": 0.0,               // floor slab, project datum
      "calibration": {
        "p1_pdf": [120.5, 340.2],       // native PDF points
        "p2_pdf": [520.5, 340.2],
        "known_distance": 40.0,
        "known_unit": "ft",
        "scale_m_per_pdfunit": 0.03048, // derived
        "calibrated_at": "2026-07-28T18:00:00Z"
      }
    }
    // fl_source elevation_m: 4.3, fl_above: 8.6 ...
  ],

  "sources": [
    {
      "id": "src_uptake_1",
      "floor_id": "fl_source",
      "position_pdf": [310.0, 220.0],
      "label": "Uptake Room 1 — patient chair",
      "height_above_floor_m": 1.0,      // TG-108 convention, editable
      "method": "tg108",
      "params": {
        "kind": "uptake",               // uptake | imaging
        "nuclide": "F-18",
        "administered_activity_MBq": 555,
        "patients_per_week": 40,
        "uptake_time_h": 1.0,
        "imaging_time_h": 0.5,
        "void_factor": 0.85,            // imaging only
        "scanner_self_shielding": 1.0   // 1.0 = no credit
      }
    },
    {
      "id": "src_ct_1",
      "floor_id": "fl_source",
      "position_pdf": [610.0, 190.0],
      "label": "CT Scanner",
      "method": "ncrp147",
      "params": { "modality": "ct", "...": "pending NCRP 147 tables" }
    }
  ],

  "pois": [
    {
      "id": "poi_office1",
      "floor_id": "fl_above",
      "position_pdf": [295.0, 210.0],
      "label": "Office 1",
      "area_type": "uncontrolled",
      "occupancy_factor": 1.0,
      "height_above_floor_m": 0.5,      // auto per §Q5, editable
      "linked_source_ids": ["src_uptake_1", "src_ct_1"],
      "barrier": {
        "existing_material": "concrete",
        "existing_thickness_cm": 10.0,  // credited, per TG-108 Ex. 4
        "proposed_material": "lead"
      }
    }
  ]
}
```

**3D distance.** Horizontal separation comes from each floor's own calibration (floors may
be at different drawing scales, so each converts its own PDF-space delta to metres
independently — they're only comparable once in metres). Vertical separation is
`(poi.floor.elevation + poi.height) − (src.floor.elevation + src.height)`. Then Euclidean.

Sanity check against Example 4 — source floor at 0.0, room above at 4.3 m floor-to-floor,
directly overhead: `Δz = (4.3 + 0.5) − (0.0 + 1.0) = 3.8 m` ✓ matches the paper.

---

## 4. File structure

```
rad-shielding-calc/
├── pyproject.toml
├── README.md
├── docs/
│   ├── PLAN.md                     ← this file
│   └── methodology.md              ← equations + citations, for report defense
├── src/radshield/
│   ├── physics/                    ← PURE. no I/O, no UI, no unit strings.
│   │   ├── archer.py               ← B(x) and its inversion x(B)
│   │   ├── materials.py            ← material registry + per-material unit metadata
│   │   ├── nuclides.py             ← TG-108 Table II dose-rate constants, half-lives
│   │   ├── decay.py                ← R(t), F_U
│   │   ├── limits.py               ← P values, occupancy presets
│   │   ├── tg108.py                ← uptake + imaging dose and transmission
│   │   └── ncrp147/
│   │       ├── tables.py           ← ⚠ blocked on your data / existing code
│   │       ├── primary.py
│   │       ├── secondary.py        ← scatter + leakage
│   │       ├── ct.py               ← DLP / scatter-fraction approach
│   │       └── screener.py         ← port of your bounding-box screener
│   ├── model/
│   │   ├── project.py, floor.py, points.py, calibration.py
│   │   ├── geometry.py             ← PDF-space ↔ real-world, 3D distance
│   │   └── store.py                ← .rsproj read/write, schema migration
│   ├── engine/
│   │   └── evaluate.py             ← per-POI aggregation → thickness + audit trail
│   ├── report/
│   │   └── export.py               ← CSV / XLSX
│   └── web/
│       ├── main.py, api.py
│       ├── render.py               ← PyMuPDF page → PNG
│       └── static/{app.js,canvas.js,index.html}
└── tests/
    ├── test_archer.py              ← Table IV grid ↔ Table V params
    ├── test_tg108_examples.py      ← paper Examples 1–6 + Tables VII, VIII
    ├── test_geometry.py            ← Examples 4 & 5 floor/ceiling distances
    ├── test_ncrp147_*.py
    └── test_roundtrip.py           ← save/load fidelity
```

### Test vectors already extracted from TG-108

These become the acceptance criteria for Phase 1 — real published values, not
self-generated ones:

| Test | Expected |
|---|---|
| Ex. 1 — uptake, 4 m, T=1, 40 pt/wk, 555 MBq, 1 h | B = 0.189 → ~1.2 cm Pb / 15 cm concrete |
| Ex. 2 — imaging, 3 m | 59.7 µSv/wk, B = 0.34 → 0.8 cm Pb / 11 cm concrete |
| Ex. 4 — room above, 4.3 m f-to-f | d = 3.8 m, 117 µSv, B = 0.17 |
| Ex. 5 — room below | d = 3.6 m, 131 µSv, B = 0.15 |
| Ex. 6 — console distance for 5 mSv/y | d = 2.32 m |
| Table VII | 12 rooms, dose + B each |
| Table VIII | wall-by-wall mm Pb |
| Table IV | full transmission grid, 3 materials |
| R(t), F-18 | 0.91 / 0.83 / 0.76 at 30 / 60 / 90 min |

Key constants: Γ_eff = 0.143 µSv·m²/MBq·h (F-18, effective dose equivalent, ANSI/ANS-6.1.1
1991); **0.092** µSv·m²/MBq·h after patient self-attenuation — this is the one that appears
in the dose equations; T½(F-18) = 110 min.

---

## 5. Phased build plan

I'd invert the order suggested in the spec and build **physics first, PDF second**. Reasons:
the physics is the part that must be *right*, it's verifiable today against published
examples with zero UI, and it's the only component that's a hard blocker for everything
downstream. PDF plumbing is well-understood work that can't fail in subtle, silent ways.

**Phase 1 — TG-108 physics engine** *(no blockers, can start immediately)*
Archer model + inversion, materials with unit metadata, nuclide registry, decay factors,
uptake/imaging transmission. Full test suite green against Examples 1–6 and Tables IV,
VII, VIII. Deliverable: a `pip install`-able package usable from a REPL — real value even
before any UI exists.

**Phase 2 — PDF load, render, calibrate**
FastAPI skeleton, PyMuPDF rendering, canvas with pan/zoom, two-click calibration with known
distance entry, per-floor calibration display and re-calibration. Deliverable: load a
drawing, calibrate, measure a known dimension, confirm it reads back correctly.

**Phase 3 — Point placement, metadata, persistence**
Place/drag/delete sources and POIs, TG-108 parameter forms, source↔POI linking with drawn
link lines, `.rsproj` save/load. Deliverable: a project survives a full close/reopen cycle
unchanged.

**Phase 4 — Multi-floor stack and 3D geometry**
Floor manager, elevations/floor-to-floor heights, cross-floor linking, method-aware height
conventions (§Q5), a "ghost" overlay showing another floor's points beneath the current
one for vertical alignment. Deliverable: reproduce Examples 4 and 5 end-to-end through the
UI.

**Phase 5 — Results, audit trail, export**
Per-POI aggregation across all linked sources (§Q4), results table, full intermediate-value
audit output (every term, every applied convention, every credited existing barrier), CSV
export. Deliverable: a table you could paste into a physics report.

**Phase 6 — NCRP 147 engine** *(blocked on Q2)*
Port primary/secondary/CT and the bounding-box screener from your existing code, add to the
same POI aggregation path, extend results.

**Phase 7 — Polish**
XLSX/PDF report export, barrier objects (the §Q4 v2 upgrade), obliquity correction,
material library expansion (steel, gypsum, leaded glass), project templates.

---

## 6. Summary of what I need from you

1. **Q1** — confirm FastAPI + JS canvas (or override).
2. **Q2** — your existing TG-108/NCRP 147 Python, and the NCRP 147 tables. **This is the
   long pole.** Phases 1–5 proceed without it; Phase 6 cannot start.
3. **Q3** — is SPECT genuinely in scope, and under which guidance?
4. **Q4/Q5** — confirm the dose-summation and distance-convention recommendations.
5. **Q7** — the six smaller defaults, or just "defaults are fine."

Answer 1 and 3–5 and Phase 1 starts immediately; everything there is fully specified by the
paper I already have.
