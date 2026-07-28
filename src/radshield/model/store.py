"""Project persistence.

A project is a single ``.rsproj`` file: a zip archive holding ``project.json``
plus the source PDFs under ``pdfs/``.  Embedding the drawings means a project
can be moved between machines without breaking file paths.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from .project import Project

PROJECT_JSON = "project.json"
PDF_DIR = "pdfs"


def save(project: Project, path: str | Path, pdf_sources: dict[str, bytes]) -> Path:
    """Write a project archive.

    Args:
        project: The project to save.
        path: Destination ``.rsproj`` path.
        pdf_sources: PDF bytes keyed by ``Floor.pdf_name``.

    Returns:
        The path written.
    """
    path = Path(path)
    missing = {f.pdf_name for f in project.floors} - set(pdf_sources)
    if missing:
        raise ValueError(f"missing PDF content for: {sorted(missing)}")

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(PROJECT_JSON, json.dumps(project.to_dict(), indent=2))
        for name, blob in pdf_sources.items():
            archive.writestr(f"{PDF_DIR}/{name}", blob)
    return path


def load(path: str | Path) -> tuple[Project, dict[str, bytes]]:
    """Read a project archive.

    Returns:
        ``(project, pdf_sources)`` with PDF bytes keyed by file name.
    """
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        project = Project.from_dict(json.loads(archive.read(PROJECT_JSON)))
        pdfs = {
            Path(name).name: archive.read(name)
            for name in archive.namelist()
            if name.startswith(f"{PDF_DIR}/") and not name.endswith("/")
        }
    missing = {f.pdf_name for f in project.floors} - set(pdfs)
    if missing:
        raise ValueError(f"project archive is missing PDF content for: {sorted(missing)}")
    return project, pdfs
