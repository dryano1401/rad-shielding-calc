"""Web API for the isotope/Archer editor: list, upsert, reset, delete."""

from __future__ import annotations

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

from radshield.physics import nuclides  # noqa: E402
from radshield.web import app as web_app  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A test client with the overlay routed to a scratch dir, restored after."""
    monkeypatch.setenv("RADSHIELD_HOME", str(tmp_path))
    before_registry = dict(nuclides._registry)
    before_archer = dict(nuclides._archer)
    with fastapi_testclient.TestClient(web_app.app) as test_client:
        yield test_client
    nuclides._registry.clear()
    nuclides._registry.update(before_registry)
    nuclides._archer.clear()
    nuclides._archer.update(before_archer)


def test_list_nuclides_includes_builtins_and_511kev_defaults(client):
    payload = client.get("/api/nuclides").json()
    names = {n["name"] for n in payload["nuclides"]}
    assert "F-18" in names
    f18 = next(n for n in payload["nuclides"] if n["name"] == "F-18")
    assert f18["is_builtin"] is True
    assert f18["is_customized"] is False
    assert payload["default_511_archer"]["lead"]["alpha"] == pytest.approx(1.543)


def test_add_isotope_defaults_archer_to_511kev_and_shows_up_in_options(client):
    defaults = client.get("/api/nuclides").json()["default_511_archer"]
    response = client.post(
        "/api/nuclides",
        json={
            "name": "Tc-99m",
            "half_life_min": 360.6,
            "gamma_eff": 0.0182,
            "gamma_patient": None,
            "is_511_kev": False,
            "source": "user-supplied",
            "archer": {
                "lead": {**defaults["lead"], "source": "prefilled from 511 keV, unverified for Tc-99m"},
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["nuclide"]["archer"]["lead"]["alpha"] == pytest.approx(defaults["lead"]["alpha"])
    assert "Tc-99m" in body["options"]["nuclides"]


def test_add_isotope_rejects_missing_name(client):
    response = client.post("/api/nuclides", json={"half_life_min": 1, "gamma_eff": 1})
    assert response.status_code == 400


def test_add_isotope_rejects_non_positive_alpha(client):
    response = client.post(
        "/api/nuclides",
        json={
            "name": "Bad-Iso",
            "half_life_min": 10,
            "gamma_eff": 0.1,
            "archer": {"lead": {"alpha": 0, "beta": 0, "gamma": 1}},
        },
    )
    assert response.status_code == 400


def test_edit_builtin_then_reset_via_delete(client):
    edit = client.post(
        "/api/nuclides",
        json={"name": "F-18", "half_life_min": 110, "gamma_eff": 0.150, "gamma_patient": 0.092, "is_511_kev": True},
    )
    assert edit.status_code == 200
    assert edit.json()["nuclide"]["is_customized"] is True

    reset = client.delete("/api/nuclides/F-18")
    assert reset.status_code == 200
    assert reset.json()["nuclide"]["gamma_eff"] == 0.143
    assert reset.json()["nuclide"]["is_customized"] is False


def test_delete_custom_isotope_removes_it_from_options(client):
    client.post("/api/nuclides", json={"name": "Custom-1", "half_life_min": 10, "gamma_eff": 0.1})
    response = client.delete("/api/nuclides/Custom-1")
    assert response.status_code == 200
    assert response.json()["nuclide"] is None
    assert "Custom-1" not in client.get("/api/options").json()["nuclides"]


def test_delete_unknown_isotope_is_404(client):
    response = client.delete("/api/nuclides/Nonexistent-99")
    assert response.status_code == 404
