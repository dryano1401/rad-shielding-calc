# Brief: making radshield deployable on Vercel

You're picking up a task the repo owner scoped out in a prior session, not something
you're discovering fresh. This document is self-contained — read it, don't assume
context from anywhere else.

## What this app is

A radiation shielding calculator for medical imaging facilities (NCRP 147 for
x-ray/fluoro/CT, TG-108 for nuclear medicine). Users upload floor-plan PDFs, place
source and point-of-interest markers on them, and the app computes required barrier
thicknesses. It's a FastAPI backend (`src/radshield/web/app.py`) serving a vanilla
JS/HTML/CSS single-page frontend (`src/radshield/web/static/`), backed by a pure-Python
physics/model layer (`src/radshield/physics/`, `src/radshield/model/`) that has nothing
to do with the web layer and should not need to change for this migration.

Run locally today via `python -m radshield.web` (see `src/radshield/web/__main__.py`),
which just launches `uvicorn` against `radshield.web.app:app` on localhost.

## The goal

Get this hosted on the repo owner's Vercel site. That's it — but "just deploy it" isn't
possible without real changes, for reasons below. Your job is the architecture rework
that makes it possible, not a config tweak.

## Why it doesn't run on Vercel as-is (confirmed, not a guess)

**1. It's a single stateful in-process object, and Vercel functions are stateless
per-request.**

`src/radshield/web/app.py` holds one global:

```python
@dataclass
class Session:
    project: Project = field(default_factory=Project)
    pdfs: dict[str, bytes] = field(default_factory=dict)
    path: Path | None = None

session = Session()
```

Nearly every one of the ~40 API endpoints in that file reads and mutates `session`
directly (`session.project.sources.append(...)`, etc.), and the docstring at the top of
the file says outright: *"State is held in a single in-process Session because this is a
local single-user tool."* On Vercel, each request can hit a different (or a fresh, cold)
function instance with no shared memory — a project built up over several requests would
partially or entirely vanish.

**2. PDF bytes are also only in memory.** `session.pdfs: dict[str, bytes]` holds every
uploaded floor-plan PDF. Same problem as above, plus Vercel functions only get an
ephemeral `/tmp` (not even guaranteed to persist between invocations of the *same* warm
instance for long), so there's nowhere durable to fall back to without external storage.

**3. There's a second, smaller instance of the same problem.** `src/radshield/physics/nuclides.py`
lets a user add/edit isotope shielding data through the UI, persisted to
`~/.radshield/custom_nuclides.json` on local disk (see `_custom_store_path()` in that
file). This is a real filesystem write outside `/tmp`, which won't work on Vercel at
all. Decide with the repo owner whether this feature ships in the hosted version (needs
the same external-storage treatment) or is scoped out as local-only.

**4. PyMuPDF (`fitz`) is a compiled native dependency**, used in `src/radshield/web/render.py`
to rasterize PDF pages to PNG for the canvas view. It ships prebuilt manylinux wheels, so
`pip install` should work, but Vercel's Python runtime has bundle-size limits — validate
this works and fits *before* investing in the rest of the migration (see Phase 0 below).

**5. `.rsproj` save/load already goes through a clean serialization boundary** — this is
good news, not another blocker. `src/radshield/model/store.py` already turns a `Project` +
its PDF bytes into a zip archive via `Project.to_dict()`/`Project.from_dict()`. That's most
of the serialization work for the new storage layer already done; you're changing *where*
the bytes go (external store instead of a local `.rsproj` file), not how the project
turns into bytes.

## What must not change

- The physics/model layer (`src/radshield/physics/`, `src/radshield/model/`) and its
  ~287 tests. This migration is entirely about the web/storage layer
  (`src/radshield/web/app.py` and friends). If you find yourself editing
  `physics/tg108.py` or `physics/ncrp147/`, stop — that's out of scope.
- `python -m radshield.web` should keep working for local development. Don't make the
  storage layer *require* live Vercel infrastructure to run locally — abstract it so
  local dev can use something simple (e.g. the filesystem, or a local SQLite file) and
  Vercel prod uses the real external store.

## Open product decisions — ask the repo owner, don't assume

1. **Single project or multi-tenant?** Today there's exactly one project, no accounts,
   no auth. Hosted on Vercel, is this still "one shared project for whoever has the
   link" (simplest: one fixed storage key, no auth), or does it need per-user/per-project
   isolation (needs an ID scheme — URL param, cookie, or real auth)? This decision
   drives the whole storage design; don't guess at it.
2. **Where does persistent state live?** Reasonable options: Vercel Blob for the PDF
   bytes (binary, potentially large) plus Vercel KV or Postgres for the project JSON
   (small, structured). Get the owner's preference/existing Vercel resources before
   building against a specific one.
3. **Does the custom-isotope editor (`/api/nuclides`, the "Edit isotopes…" panel) ship
   in the hosted version?** See point 3 above.

## Suggested approach

**Phase 0 — de-risk PyMuPDF first.** Before touching the storage architecture, get a
minimal FastAPI + PyMuPDF app deployed to Vercel and confirm PDF rasterization actually
works there (bundle size, cold-start time, runtime compatibility). If this doesn't work,
the fallback is rendering PDFs client-side (e.g. pdf.js in the browser) instead of
server-side rasterization in `render.py` — a bigger change, and you want to know that
before Phase 1, not after.

**Phase 1 — storage abstraction.** Introduce an interface the `Session` currently
hard-codes as in-memory (load project state at the start of a request, persist it at the
end). Keep a local/filesystem-backed implementation for `python -m radshield.web`, add a
Vercel-backed implementation (Blob + KV/Postgres, per whatever the owner picked in Open
Decision 2). `model/store.py`'s existing `Project.to_dict()`/`from_dict()` does the
hard part of the serialization already — reuse it rather than inventing a new format.

**Phase 2 — thread project/session identity through every endpoint.** Every handler in
`app.py` needs to know *which* project it's operating on (per Open Decision 1) and load
it via the new storage layer instead of touching the `session` global directly.

**Phase 3 — Vercel packaging.** Repo layout/config Vercel's Python runtime expects
(entrypoint file, `vercel.json`, `requirements.txt` or equivalent), and get the static
frontend served correctly (it's currently mounted via FastAPI's `StaticFiles`, which
should keep working, but verify it under Vercel's routing).

**Phase 4 — tests.** `tests/test_web_api.py` currently pokes the global `session` object
directly in its fixture (`web_app.session.project = ...`) — this will need to change to
go through whatever the new storage interface is. The rest of the test suite
(physics/model layer) should need no changes; if it does, you've likely drifted outside
scope.

## Verification before calling this done

- `pytest -q` still fully green, including an updated `test_web_api.py`.
- A full workflow (upload a floor PDF, calibrate, place a source and a point, calculate,
  save/reload) works end-to-end against the actual deployed Vercel instance, not just
  locally — state persistence is the entire point of this migration, so local-only
  verification proves nothing.
- `python -m radshield.web` still works unchanged for local development.
