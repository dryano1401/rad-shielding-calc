# Methodology, data provenance and known discrepancies

This document records where every constant comes from and where the source
documents disagree with themselves. It is intended to be readable by a
reviewer checking a shielding report produced with this tool.

## 1. The shared transmission model

Both methodologies use the Archer three-parameter broad-beam model:

    B(x) = [ (1 + b/a) * exp(a*g*x) - b/a ] ^ (-1/g)
    x(B) = ln[ (B^-g + b/a) / (1 + b/a) ] / (a*g)

Implemented once in `radshield.physics.archer`. Adding an isotope or material
is a data change, not a code change.

**Unit trap.** NCRP 147 tabulates alpha and beta in mm^-1; TG-108 tabulates
them in cm^-1. Confusing them is a factor-of-ten error in barrier thickness.
`ArcherParams.unit` carries the unit with the parameters, and
`test_units_differ_between_methodologies` pins the distinction.

TG-108 compounds the trap: its Table IV prints lead thickness in **mm** while
its Table V parameters are in **cm^-1**. `test_tg108_table_iv_lead` verifies
the conversion explicitly.

## 2. TG-108 (PET / nuclear medicine)

Source: Madsen et al., *AAPM Task Group 108: PET and PET/CT Shielding
Requirements*, Med Phys 33(1), 14-24, 2006.

### Equations

    uptake room (Eq. 3):   D_week = G * N_w * A0 * t_U * R(t_U) / d^2
    imaging room (Eq. 9):  D_week = G * N_w * A0 * v * F_U * t_I * R(t_I) / d^2
    transmission (Eq. 4):  B = P / (T * D_week)

    R(t) = 1.443 * (T_half / t) * (1 - exp(-0.693 * t / T_half))    [Eq. 1]
    F_U  = exp(-0.693 * t_U / T_half)

`G` is the patient-self-attenuated effective dose equivalent rate constant:
**0.092 uSv m^2 / (MBq h)** for F-18, against 0.143 free in air (Table II).

Occupancy scales the *design goal*, not the reported dose, per the footnote to
Table VII. Reported weekly doses are therefore unmodified by T.

### Constants

| Quantity | Value | Source |
|---|---|---|
| F-18 half-life | 110 min | TG-108 (used throughout its examples) |
| F-18 dose-rate constant, in air | 0.143 uSv m^2 / MBq h | Table II (ANSI/ANS-6.1.1 1991) |
| F-18, patient-attenuated | 0.092 uSv m^2 / MBq h | Eqs. 2-12 |
| Voiding credit | 0.85 | imaging-room text |
| Design goal, uncontrolled | 20 uSv/week | 1 mSv/y public limit |
| Design goal, controlled | 100 uSv/week | 5 mSv/y ALARA target |
| Archer fits at 511 keV | Pb 1.543 / -0.4408 / 2.136; concrete 0.1539 / -0.1161 / 2.0752; iron 0.5704 / -0.3063 / 0.6326 (cm^-1) | Table V |

### Floor and ceiling geometry (Fig. 5)

Source assumed 1.0 m above its floor; target 0.5 m above the floor for the
room above, 1.7 m above the floor for the room below:

    d_above = H - 1.0 + 0.5        d_below = H + 1.0 - 1.7

For H = 4.3 m these give 3.8 m and 3.6 m, matching Examples 4 and 5.

### Height is only meaningful across floors

A source's height above its floor and a point's height above its own are
entered independently -- a beam height, an occupied height -- and are not
assumed to describe how the two line up vertically. Across floors that
matters: it is exactly what turns a floor-to-floor gap into the 3.8 m and
3.6 m above. On the *same* floor it does not: two entries of 1.0 m and 1.7 m
do not mean the point is 0.7 m above the source, they are just two
unrelated numbers, so `distance()` and `chart_direction()` both take the
vertical separation as zero whenever the source and the point share a
floor, regardless of what each has entered. Only a genuine change of floor
elevation introduces a vertical component.

### Extension to isotopes TG-108 does not cover

The equations are isotope-agnostic; only the constants change. Register the
nuclide and its Archer parameters and the pipeline is unchanged:

```python
register_nuclide(Nuclide("Tc-99m", half_life_min=360.6, gamma_eff=0.0195,
                         gamma_patient=0.0140, is_511_kev=False, source="..."))
register_archer("Tc-99m", ArcherParams(alpha=..., beta=..., gamma=...,
                                       unit="cm", material="lead", source="..."))
```

Two guards apply. Transmission fits at 511 keV are shared automatically across
positron emitters because the attenuation belongs to the photon energy, not
the parent nuclide. But the F-18 patient-self-attenuation ratio (0.092/0.143)
is **not** applied to non-511 keV isotopes; `patient_dose_rate_constant` raises
instead, because body attenuation at 140 keV is not that at 511 keV. Supply
`gamma_patient` explicitly.

Results for non-511 keV isotopes should be labelled "TG-108 method extended to
\<isotope\>", not "TG-108" — TG-108 is a PET/PET-CT document and does not
provide a SPECT barrier method.

`register_nuclide`/`register_archer` above are the underlying primitives, used
programmatically and by the test suite. The application itself exposes the
same capability as an editable overlay (`radshield.physics.nuclides.upsert_record`
/ `delete_or_reset_record`, and the "Edit isotopes…" panel in the web UI) so an
isotope can be added, or a shipped TG-108 value corrected, without touching
code, and have it survive a restart. Edits persist to
`~/.radshield/custom_nuclides.json` (relocate with `RADSHIELD_HOME`) as a diff
against the shipped tables, applied on top of them at import time; a built-in
isotope's edits can be reset back to the shipped value, a purely custom one is
deleted outright. **No non-511 keV isotope ships with real Archer coefficients**
— the app has no verified, citable broad-beam fit for Tc-99m, I-131, or other
SPECT isotopes to seed, so a new isotope's Archer fields default to the shipped
511 keV fit purely as an editable starting point (per-material alpha/beta/gamma
inputs, prefilled from `default_511_archer()`), not as a claim that it is
correct for that isotope's actual photon energy. Overwrite it with real data
and cite the source before relying on the result; the `source` field is
carried into the record for exactly that audit purpose.

### Discrepancies found in TG-108

Both were found by reproducing the report's own examples and are encoded in the
test suite with explanatory comments rather than tuned away.

**(a) Table VII omits the voiding credit that Example 2 applies.**
Example 2 reports 59.7 uSv/week at 3 m from the tomograph. Table VII reports
70.1 uSv for the same facility and distance. 59.7 / 0.85 = 70.2, so the table
does not apply the 0.85 voiding factor. Reproducing Table VII therefore
requires `void_factor=1.0`; with that, all rows agree to better than 1.5%:

| Room | Model | Table VII | Diff |
|---|---|---|---|
| Office 1 | 97.4 | 97.2 | +0.2% |
| Office 2 | 118.1 | 118.8 | -0.6% |
| Office 3 | 39.6 | 40.0 | -1.0% |
| Office 8 | 44.7 | 45.3 | -1.4% |
| Office 9 | 28.9 | 29.2 | -1.1% |
| Corridor 1 | 374.1 | 378.8 | -1.2% |

Practical effect: taking the voiding credit is the less conservative choice.
The default here is to take it (0.85, as Example 2 does), and it is a per-source
input so it can be disabled.

**(b) Example 4's concrete figure contradicts its own Table IV.**
Example 4 derives B = 0.17 and states "1.3 cm of lead or 17 cm of concrete".
The lead figure is consistent with Table IV. The concrete figure is not:
Table IV gives 0.2243 at 14 cm and 0.1662 at 16 cm, so B = 0.170 corresponds to
~15.8 cm, not 17 cm. The Archer fit reproduces Table IV to better than 1% at
every tabulated point, and returns 15.83 cm. This implementation follows the
fit. Using 17 cm is conservative, so the discrepancy does not create a safety
issue, but a reviewer comparing against the printed example should know why the
numbers differ.

## 3. NCRP 147 (x-ray, fluoroscopy, CT)

Source: NCRP Report No. 147, *Structural Shielding Design for Medical X-Ray
Imaging Facilities*, 2015 printing. Tables transcribed by text extraction and
shipped as CSV under `physics/data/`.

### Equations

    primary (Eq. 4.2):    B_p   = P * d_P^2   / (K1^P   * U * N * T)
    secondary (Eq. 4.5):  B_sec = P * d_sec^2 / (K1^sec *     N * T)

K1 values are unshielded air kerma **per patient at 1 m**, in mGy. The design
goal P is in mGy air kerma per week: 0.02 uncontrolled, 0.1 controlled.

Point of protection is 0.3 m beyond the distal barrier surface. That offset is
a geometry-layer concern, not applied inside the physics functions. It applies
to TG-108 sources too, not just NCRP 147 ones: TG-108's own default
source-to-wall/floor distances are themselves drawn from NCRP guidance, so
there is no separate "TG-108 doesn't need this" convention to carve out. A
point's "NCRP standoff already applied" checkbox governs every source linked
to it, regardless of method.

### Shipped tables

| File | Source table | Contents |
|---|---|---|
| `ncrp147_primary_archer_kvp.csv` | A.1 | primary fits per 5 kVp, 6 materials |
| `ncrp147_primary_archer_workload.csv` | B.1 | primary fits per workload distribution |
| `ncrp147_secondary_archer.csv` | C.1 | secondary fits, per kVp and per workload |
| `ncrp147_workload_distribution.csv` | 4.2 | kVp distribution of workload |
| `ncrp147_workload_totals.csv` | 4.2 | Wnorm and surveyed patients/week |
| `ncrp147_use_factors.csv` | 4.4 | primary use factors U |
| `ncrp147_k1p.csv` | 4.5 | K1^P primary air kerma per patient |
| `ncrp147_k1sec.csv` | 4.7 | leakage and scatter kerma per patient |
| `ncrp147_occupancy.csv` | B.1 | occupancy factors — **unverified, see below** |

Workload-distribution-weighted fits (B.1, C.1) are preferred over single-kVp
fits because they are pre-integrated over the clinical spectrum.

### Known gaps in the extraction

Declared in `tables.KNOWN_GAPS`. A kVp lookup (Table A.1 primary, Table C.1
secondary) that falls between two tabulated values is linearly interpolated
between them (alpha, beta, gamma independently), with the substitution
disclosed in `ArcherParams.source` -- e.g. a CT tube at 130 kVp interpolates
between the tabulated 125 and 140 kVp rows. A kVp outside the tabulated range
still raises rather than extrapolating with no support for it, and a
non-kVp lookup (workload-keyed Table B.1, or Table C.1's workload rows) has
no ordering to interpolate along and is still exact-match only.

1. **Table A.1**: 40 and 45 kVp captured for concrete only; other materials
   jump 35 -> 50 kVp (now bridged by interpolation for materials whose range
   spans the gap, e.g. lead has 35 and 50 kVp rows either side).
2. **Table B.1**: Peripheral Angiography missing for steel, plate glass, wood.
3. **Table C.1**: for steel, plate glass and wood only 30, 50, 70, 125 and
   150 kVp were captured; the 100 kVp row and all workload rows are absent.
   Lead and concrete additionally have 120 and 140 kVp rows.
4. **Occupancy factors** were not part of the extraction. The shipped table is
   seeded from the published values and every row is flagged
   `NEEDS_VERIFICATION`. Verify against NCRP 147 Table B.1 before use in a
   report.
5. **CT isodose maps** are scanner-specific and are not shipped. The isodose
   method requires caller-supplied values and a `source` string. The DLP method
   ships its constants (below).

### CT secondary barriers, DLP method

Scattered air kerma from the dose-length product, with separate constants by
body region:

    head:  K_sec = kappa_head * DLP / d^2           kappa_head = 9e-5 cm^-1
    body:  K_sec = kappa_body * 1.2 * DLP / d^2     kappa_body = 3e-4 cm^-1

The body form carries an additional factor of **1.2**. It is stored and
reported separately from kappa rather than pre-multiplied into it, so the
audit trail shows both and a reviewer need not reverse engineer a single
composite constant.

Units resolve cleanly: kappa is per centimetre and DLP is in mGy cm, so their
product is the scattered air kerma in mGy at 1 m, which the inverse square
then carries to the distance of interest. DLP is the weekly total, that is
the per-procedure DLP times the weekly procedure count.

Per unit DLP the body form is exactly four times the head form
(3e-4 x 1.2 / 9e-5 = 4), which the test suite uses as a cheap check that the
1.2 factor has not been dropped. Both kappa and the region factor can be
overridden per scanner; the shipped values live in
`physics/data/ncrp147_ct_scatter.csv`.

### CT secondary barriers, manufacturer scatter charts

Vendors publish scatter as a grid of air kerma on a plane through the
isocentre: a plan view looking down, and an elevation view from the side.

**The chart is read where the point is.** It is a map of the room laid over
the drawing with its origin on the isocentre, so within the printed grid the
published value is used exactly as it stands. No inverse-square correction is
applied, because the chart already accounts for the distance to that spot and
scaling it again would count the distance twice. Reading between cells is a
bilinear interpolation of the four surrounding values, falling back to the
nearest printed cell where the chart is masked and no complete set of four
exists (the gantry footprint, the pedestal).

That the chart wins over any model is not a technicality. Four metres along
the table axis the chart used to develop this reads 0.002 mGy, because the
pedestal shadows that spot; a 1/d^2 projection of the same bearing gives about
0.012, six times higher. Inside the chart, the manufacturer's measurement
stands.

**Inverse square is only for where the chart does not reach**: a point past
the edge of the printed grid, or a point on another storey with no elevation
chart. There, each cell is normalised to

    S = K * r^2

the scatter strength on that bearing, which is independent of distance, and
the value follows as ``K = S / d^2``. On real charts ``S`` is steady: along
the table axis it holds to within 5% from 0.5 m to 2.5 m, and a test pins that
spread so a mistranscribed chart fails as a physical inconsistency rather than
passing as a plausible number.

When projecting, the largest ``S`` on the bearing is used rather than the
nearest in radius. Selecting by radius would let a projection inherit the
pedestal shadow, and the answer would then fall tenfold as the point moved a
few centimetres further out, in the unsafe direction.

**Geometry.** The placed source point is the isocentre, and each source
carries a rotation -- the angle its chart's +x axis makes with east -- so the
chart can be turned to match how the equipment sits on the drawing. Where the
NCRP standoff or an entered distance override applies, the read position is
moved outward to match, so a single distance governs throughout rather than
the chart being read at one place and the arithmetic quoting another.

**Plan against elevation.** A point on another floor is read from the
elevation chart when one is assigned, since that is the view describing what
leaves the gantry vertically. Falling back to the plan chart is allowed but
noted, because the plan view does not represent the vertical separation.

**Left/right and front/back are not universal.** Rotation places the chart's
axes on the drawing, but it cannot say which side of the printed grid is
which -- a vendor may print column offsets increasing toward either side of
the gantry, or row offsets increasing toward either the foot of the table or
the back of the gantry, and nothing in the grid itself says which. Each chart
carries a `flip_x`/`flip_y` pair for this, applied when the grid is turned
into cells: negating a coordinate moves a cell to the other side of the
isocentre without touching the value that was read there, so the grid stays
stored exactly as pasted and can still be checked cell by cell. Left
unset, a chart reads as printed; either flag can be changed after import,
without re-pasting, once a calculated pattern shows it is mirrored from the
real room.

**Workload basis.** Charts are usually quoted per procedure, but per mAs and
per 100 mAs occur too. The basis is recorded with the chart and decides what
the weekly total is formed from: a procedure count, or the weekly workload in
mAs.

### Validation status

The extracted set contains no worked examples, so the NCRP 147 tests verify
table integrity, unit handling, equation structure and the gap error paths —
not published end-to-end answers. Two independent physical cross-checks do
pass: lead at 100 kVp gives an asymptotic HVL of 0.277 mm against NCRP's
published ~0.27 mm, and transmission at 0.25 mm and 1.0 mm lands on the
published curve. When NCRP 147's Section 5 examples become available they
should be added as end-to-end fixtures, matching the TG-108 test file.

## 3a. Registering drawings to each other

Each floor's PDF has its own arbitrary origin, and sheets often lay the
building out at different orientations. Cross-floor geometry therefore needs
the drawings registered to one another, which is done by marking the same two
physical features -- columns, a stair core, a lift shaft -- on every floor.

Two features define a **similarity transform**: translation, rotation and
uniform scale. That is the complete and correct model for architectural
drawings, which are never sheared and never scaled differently along their two
axes.

One feature gives only the translation. It cannot express how a drawing is
turned, so a sheet at another orientation stays wrong however carefully the
single point is placed, and the error reaches the calculated distances rather
than just the display. On a sheet rotated 90 degrees, a point placed at the
same physical spot as the source one storey up reckons as 5.54 m away with one
feature and 3.80 m -- correct, directly overhead -- with two. The app reports
when a floor has only one.

**Consistency check.** The two features are the same two physical things on
every floor, so every floor must agree on how far apart they are. A
disagreement beyond 2% means a wrong scale or a misplaced feature, either of
which would quietly skew every cross-floor distance, and is reported.

## 4. Barriers on a path

A wall drawn on a plan is a vertical rectangle: its plan segment extruded
between a base and top height above its own floor.  A source-to-point path is
tested against every wall on every floor, and the height band does the
filtering, so one test serves both within-storey and between-storey paths --
a 3 m partition simply is not in the way of a ray that has already climbed
above it on its way to the floor above.  Barriers may also be declared by name
against a specific source-point pair, for structure that is not on the drawing
(a slab, a leaded door, a control window).

### Reducing a stack of barriers

Where a path crosses more than one barrier, the barriers are converted to the
equivalent thickness of a single reference material (lead), summed, and the
Archer fit applied **once**.

The tempting alternative -- computing each barrier's transmission separately
and multiplying them -- is wrong in the unsafe direction.  The fits already
embed build-up for a single barrier, and multiplying them ignores the beam
hardening that occurs between layers: the second barrier attenuates a spectrum
the first has already filtered, so it removes proportionally less than its
own fit predicts.  Multiplying therefore understates what gets through, which
overstates the protection achieved.

A barrier whose material the active methodology cannot attenuate (gypsum at
511 keV, for instance) is dropped with a warning rather than approximated.
Dropping a barrier understates shielding, so the result stays conservative,
and the warning appears in the audit trail.

### Attenuation is per path, not per point

Each source's dose is attenuated by the barriers on *its own* path before the
doses are summed.  This is what allows one source to reach a point through a
shielded wall while another nearby source reaches the same point unobstructed
-- the common case in practice, and one a single per-point credit cannot
express.

### Obliquity

A path crossing a barrier at angle theta from the normal traverses
``t / cos(theta)`` rather than ``t``.  This correction is available as a
toggle and is **off** by default: ignoring it under-counts material, so the
default errs safe and matches a hand calculation.  When enabled, the angle and
the corrected thickness both appear in the results.

## 5. Summing sources at a point

Where several sources are incident on one point, their unshielded doses are
summed *before* the transmission factor is derived — TG-108 Table VII does
exactly this for the uptake room and tomograph. The barrier requirement is
therefore a property of the point, not of a source-point pair. Each source's
dose is attenuated by the barriers on its own path before this summation (see
§4), so a source reaching the point through a shielded wall correctly sums
against another reaching it unobstructed rather than being over-shielded by a
barrier that only stands in one of their ways.

Where summed NCRP 147 sources carry different workload distributions, the
summed transmission requirement is solved with each source's own Archer
parameters and the governing (largest) thickness is returned.

### TG-108 and NCRP 147 sources at the same point

Photons are the whole of what both methodologies model here, and the
radiation weighting factor for photons is 1, so 1 uGy of NCRP 147 air kerma
and 1 uSv of TG-108 effective dose equivalent are the same quantity, not two
that merely happen to share a name. `physics/limits.py` already prices its
default weekly goals on that basis (0.02 mGy uncontrolled = 20 uSv
uncontrolled, 0.1 mGy controlled = 100 uSv controlled), so nothing about
adopting it is a new assumption -- it was already latent in the numbers.

A point linked to sources of both kinds therefore has its doses **summed**,
not solved separately with the larger of the two requirements taken as
before. The distinction matters: two sources each individually under the
weekly goal can still add up to more than it, and reporting whichever
methodology's own thickness is larger misses that entirely.

There is no closed-form thickness for the combined case the way there is for
one methodology alone, because the two contributions attenuate at different
rates through the same material -- different photon energies, different
Archer fits. `_solve_combined` finds it by bisection instead, which is safe
because transmission is monotonically decreasing in thickness for both. A
material tabulated for only one of the two methodologies (iron for TG-108,
gypsum/steel/glass/wood for NCRP 147) is reported as unavailable for the
combined requirement rather than silently ignoring whichever dose it has no
data for -- the same choice already made when a barrier's material has no
transmission data at all (§4).

Each methodology's own total and thickness are still reported alongside the
combined row, for the audit trail; it is the combined figure that governs.
