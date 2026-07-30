# Project History

A chronological record of how radshield was built: what each phase added,
what broke along the way, and how it got fixed. Where `docs/PLAN.md` records
decisions and `docs/methodology.md` records the physics and modeling
rationale, this document records the sequence of events — useful for
understanding *why* the code looks the way it does, not just what it does.

## 1. Planning

Before any code, we worked through the shape of the problem: NCRP 147
(X-ray/fluoroscopy/CT) and AAPM TG-108 (PET/nuclear medicine) as the two
governing methodologies, PDF floor plans as the input format, multi-floor 3D
geometry, scale calibration, point placement with metadata, and a calculation
engine sitting behind a browser GUI. Captured in `docs/PLAN.md`.

## 2. Physics core (`649a4d0`)

The Archer three-parameter broad-beam transmission model
(`src/radshield/physics/archer.py`) went in first, since both methodologies
share it — NCRP 147 parameters in mm⁻¹, TG-108 parameters in cm⁻¹ at 511 keV,
a unit mismatch deliberately covered by tests. On top of it: the TG-108 dose
engine (`tg108.py`) reproducing the report's own worked examples exactly, and
the NCRP 147 tables. Two real discrepancies in TG-108 itself were found and
documented rather than "fixed": Table VII silently omits the voiding factor
that Example 2 uses, and Example 4's stated 17 cm of concrete contradicts its
own Table IV (~15.8 cm is correct). Both are encoded as intentional test
deviations with comments explaining the mismatch.

## 3. GUI (`291a80a`)

FastAPI backend, PyMuPDF for rendering PDF floor plans to images, a
vanilla-JS canvas frontend with no build step. Workflow: upload a PDF per
floor, set floor-to-floor spacing, calibrate scale by clicking two points a
known distance apart, place source and point-of-interest markers, view
results.

## 4. Measurement and overrides (`ef63d1b`)

Added a measure tool for walls on the PDF, and made source-to-point distances
visible and directly editable — since the geometric distance from marker
positions is sometimes wrong (a wall isn't where the drawing says, a point
sits at an odd offset), the override always takes precedence when set.

## 5. Wall barriers and per-path attenuation (`e28dd59`)

The largest architectural piece: draw walls directly on the PDF with
material/thickness, auto-detect where a source-to-point ray crosses one as a
3D ray-vs-vertical-rectangle intersection, height-banded so cross-floor rays
are tested against each wall's base/top height on every floor in between.
This is a *hybrid* design: automatically detected walls are credited, but a
point can still carry manually-named per-source barriers for cases the
geometry can't infer. Critically, each source's dose is reduced only by the
barriers on *its own* path to a point — sources reaching the same point
through different walls are attenuated independently, then summed, not given
a single shared per-point credit. Barrier stacks reduce to an equivalent
thickness of one reference material (lead) before the Archer fit is applied
once — multiplying individual transmissions would ignore beam hardening and
be non-conservative. Obliquity (1/cos θ) shipped as an opt-in toggle, off by
default.

Three test expectations in this phase were wrong on the *test* side (hand-
computed ray-height intersections that didn't match the geometry actually
drawn) — corrected by recomputing by hand rather than changing the code,
which was right.

## 6. CT DLP fix (`d4802ab`)

A user-supplied correction caught a real, non-conservative bug: the body
region's 1.2 multiplier was missing entirely from the secondary-scatter
equation (`K_sec = κ·DLP` instead of `κ·1.2·DLP`), a 20%-low result. Fixed
per the correct equations (body: 3×10⁻⁴ cm⁻¹ × 1.2 × DLP; head: 9×10⁻⁵ cm⁻¹ ×
DLP), with NCRP 147's constants shipped as data
(`physics/data/ncrp147_ct_scatter.csv`) and a regression test asserting body
is exactly 4× head per unit DLP (3e-4×1.2 / 9e-5 = 4) specifically so the
factor can't be silently dropped again.

## 7. Manufacturer isodose charts (`cf828ef`, `47a9eca`)

Support for reading a vendor's own printed scatter/isodose chart instead of
computing DLP-based scatter: place the chart's isocenter geometrically, then
sample dose at a point by bearing and radius.

Two real modeling bugs were found and fixed here:

- **Shadow-cliff bug** (self-found): initially selected the nearest chart
  cell by radius on a matched bearing, which could land on a low-value
  "shadow" cell (e.g. a pedestal shadow in a real vendor chart) and then
  extrapolate outward from it — producing a ~10× cliff in dose as distance
  crossed that cell's radius. Fixed by selecting the *strongest* normalized
  "strength" (S = value × r²) cell along a bearing (an "envelope" read)
  instead of the nearest one.
- **Double distance-correction bug** (user-corrected): the original
  implementation applied 1/d² correction to every chart read, including
  points that fell inside the chart's own printed grid — double-counting
  distance, since the tabulated value at that grid position already reflects
  the true measured distance. Fixed by rewriting `sample_at()` so it reads
  the chart directly (bilinear interpolation, or nearest-cell fallback in
  masked regions) whenever the position is inside `scatter_map.covers()`,
  and only extrapolates via inverse-square when genuinely outside the
  chart's extent or on a floor with no elevation chart of its own. Chart
  units (per procedure, per mAs, per 100 mAs) are handled via a
  `WORKLOAD_BASIS`/`weekly_multiplier()` lookup rather than assuming mGy per
  procedure.

## 8. Isotope mixes and reference dose (`753ab31`)

A placed PET/NM source can now hold a list of isotope components, each with
its own activity, patient count, and timings, each decayed on its own
half-life (R(t), F_U per TG-108 Eq. 1) — doses summed across the mix via the
new `combined_weekly_dose()`. A silent `.replace()` failure during this work
(an indentation mismatch caused zero matches, so only the first isotope's
dose was reported in `components` while the *summed total* elsewhere stayed
correct) was caught by a test asserting `len(contribution.components) == 2`
and failing with `0 == 2` — after that, every subsequent patch script
asserted its `old` string was found before proceeding.

Same phase: sources display their unshielded dose at 1 m as a sanity check
(`reference_dose()` in `engine/evaluate.py`), scale can be typed directly
("1:50", "8ft:2.4m") instead of only calibrated by clicking two points
(`parse_scale()`), and source/POI positions from other floors can be
overlaid ("Show other floors") once drawings are aligned to each other.

## 9. Wall thickness unit bug (`e37a27e`)

User-reported: in metric display mode the UI labeled the field
"millimetres" but the JS multiplied the entered value by 1000 as if it were
metres — typing "200" for a 200 mm wall silently created a 200 *metre* wall.
An undetected 1000× error that would present as implausible over-shielding
rather than a crash. Fixed the unit handling, added a
`MAX_WALL_THICKNESS_MM = 3000.0` sanity bound on `Wall` construction/edit
that rejects absurd thicknesses with a "units mixup" hint, and made
thickness and top-height editable in the wall list (previously only
material/label were), displayed to two decimal places. Verified by a full
round-trip in the browser: 200 mm → edit to 152.4 mm → switch to feet
(shows 6.00") → edit to 8.00" → stores as 203.20 mm exactly.

## 10. Two-point floor registration (`6878548`)

User-reported: cross-floor overlays didn't line up, and "maybe need more
alignment points?" The original registration used one alignment point per
floor, which fixes only *where* a drawing sits, not *how it's turned* — on
sheets drawn at different orientations this was wrong not just visually but
in every calculated cross-floor distance (demonstrated case: 5.539 m
computed vs. 3.800 m correct, a 46% error that compounds under inverse-square
dose scaling). Fixed by requiring two alignment points per floor — a full
similarity transform (translation + rotation + uniform scale) — with
graceful fallback to the old single-point behavior when a second point isn't
set (now explicitly flagged in the UI as "rotation uncorrected"), plus a
consistency check that flags any pair of floors disagreeing by more than 2%
on the distance between their two reference features (a strong signal of a
wrong scale or a misplaced feature). Verified against a programmatically
generated PDF sheet rotated 90°: one-feature registration gave a horizontal
distance of 4.030 m (wrong), two-feature gave 0.000 m (correct — directly
overhead), with zero PDF-unit placement error in the overlay.

## Testing approach

Two verification methods were used throughout, in combination:

1. **Reference-value tests** — reproducing published or vendor numbers by
   hand wherever possible: TG-108's own worked examples, a real vendor
   isodose chart transcribed by hand, NCRP 147 CT calculations cross-checked
   against a hand-derived "4× head vs. body" invariant.
2. **Live browser verification** — actually driving the running FastAPI + JS
   app in headless Chromium via Playwright, printing intermediate JS state
   and computed values, and cross-checking them against independent hand
   calculations.

Both caught bugs the other would likely have missed: the wall-thickness unit
bug and the rotation-registration bug were both found this way, and an
infinite-recursion bug (a stray duplicated code block calling `draw()` from
inside a function `draw()` itself calls) was only visible via the browser's
JS stack trace.

## Where things stand

261 tests passing as of the two-point registration work (`6878548`,
`5908fbb`). All features requested through that point are implemented,
tested, verified live in-browser, and pushed to `main`, with the
`claude/radiation-shielding-calc-q84ggc` branch kept fast-forwarded to match.
