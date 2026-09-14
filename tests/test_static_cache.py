"""Static assets must revalidate, so a stale app.js cannot outlive a fresh index.html."""
from fastapi.testclient import TestClient
from radshield.web.app import app

client = TestClient(app)


def test_static_assets_carry_no_cache():
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert "no-cache" in r.headers["cache-control"]


def test_revalidation_still_yields_a_cheap_304():
    first = client.get("/static/app.js")
    second = client.get("/static/app.js",
                        headers={"if-none-match": first.headers["etag"]})
    assert second.status_code == 304
    assert "no-cache" in second.headers["cache-control"]
    assert not second.content
