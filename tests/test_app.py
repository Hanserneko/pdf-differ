import io
import json
import time
import zipfile

from fastapi.testclient import TestClient
import pytest

from pdfdiff.app import MAX_FILE_BYTES, TTL_SECONDS, app
from pdfdiff.demo import make_demo_pair


@pytest.fixture
def client():
    with TestClient(app) as connection:
        yield connection


def test_local_page_and_assets(client):
    assert client.get("/api/health").json()["status"] == "ok"
    page = client.get("/")
    assert page.status_code == 200
    assert "PDF" in page.text
    for asset in ("app.css", "app.js"):
        assert client.get(f"/static/{asset}").status_code == 200
    assert page.headers["x-content-type-options"] == "nosniff"


def test_upload_validates_file_and_size(client):
    assert client.post("/api/documents", content=b"hello").status_code == 400
    assert client.post("/api/documents", content=b"%PDF-broken").status_code == 400
    assert client.post(
        "/api/documents", content=b"", headers={"content-length": str(MAX_FILE_BYTES + 1)}
    ).status_code == 413
    reference, _ = make_demo_pair()
    upload = client.post("/api/documents", params={"filename": "../文档.pdf"}, content=reference)
    assert upload.status_code == 201
    metadata = upload.json()
    assert metadata["filename"] == "文档.pdf"
    assert metadata["page_count"] == 2
    assert metadata["pages"][0]["width_mm"] == pytest.approx(210, abs=0.1)


def test_compare_export_round_trip(client):
    demo = client.post("/api/demo")
    assert demo.status_code == 200
    documents = demo.json()
    response = client.post(
        "/api/compare",
        json={
            "reference_id": documents["reference"]["id"],
            "candidate_id": documents["candidate"]["id"],
            "dpi": 96,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    report = payload["report"]
    assert report["text"]["matched_characters"] > 20
    assert report["text"]["median_dx_mm"] == pytest.approx(1, abs=0.02)
    assert report["text"]["line_spacing_delta_mm"] == pytest.approx(0.12, abs=0.02)
    diagnostics = report["diagnostics"]
    assert diagnostics["schema_version"] == 1
    assert diagnostics["findings"]
    assert any(finding["type"] == "text.position" for finding in diagnostics["findings"])
    assert all(finding["source_location"] is None for finding in diagnostics["findings"])
    assert len(report["files"]["reference"]["sha256"]) == 64
    for image_url in payload["images"].values():
        image = client.get(image_url)
        assert image.status_code == 200
        assert image.content.startswith(b"\x89PNG")
        assert image.headers["cache-control"] == "no-store"
    exported_json = client.get(payload["exports"]["json"])
    assert exported_json.json() == report
    exported_html = client.get(payload["exports"]["html"])
    assert exported_html.status_code == 200
    assert "data:image/png;base64," in exported_html.text
    assert "差异诊断" in exported_html.text
    archive = client.get(payload["exports"]["zip"])
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        assert set(bundle.namelist()) == {
            "report.json", "report.html", "reference.png", "candidate.png", "overlay.png", "diff.png"
        }
        assert json.loads(bundle.read("report.json")) == report


def test_bad_parameters_are_user_errors(client):
    documents = client.post("/api/demo").json()
    settings = {
        "reference_id": documents["reference"]["id"],
        "candidate_id": documents["candidate"]["id"],
        "dpi": 96,
    }
    for change, expected in (
        ({"reference_page": 0}, 422),
        ({"reference_page": 99}, 400),
        ({"dpi": 123}, 422),
        ({"ink_threshold": 999}, 422),
        ({"region_mm": [2, 2, 1, 1]}, 400),
        ({"region_mm": [0, 0, 10000, 10000]}, 400),
        ({"candidate_id": "missing"}, 404),
    ):
        response = client.post("/api/compare", json={**settings, **change})
        assert response.status_code == expected, response.text
        assert isinstance(response.json()["detail"], str)
    invalid_json = json.dumps({**settings, "region_mm": [0, 0, float("nan"), 100]})
    assert client.post(
        "/api/compare", content=invalid_json, headers={"content-type": "application/json"}
    ).status_code == 422


def test_expired_document_requires_reupload(client):
    documents = client.post("/api/demo").json()
    reference_id = documents["reference"]["id"]
    client.app.state.store.documents[reference_id].touched = time.monotonic() - TTL_SECONDS - 1
    response = client.post(
        "/api/compare",
        json={"reference_id": reference_id, "candidate_id": documents["candidate"]["id"]},
    )
    assert response.status_code == 404


def test_external_web_pages_cannot_use_local_api(client):
    for headers in (
        {"origin": "https://example.com"},
        {"sec-fetch-site": "cross-site"},
        {"origin": "null"},
    ):
        assert client.post("/api/demo", headers=headers).status_code == 403
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 400
    assert client.post("/api/demo", headers={"origin": "http://testserver"}).status_code == 200
