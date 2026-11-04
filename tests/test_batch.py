"""使用真实 PDF 与 API 验证逐页任务，事件只用于稳定控制并发时序。"""

from concurrent.futures import ThreadPoolExecutor
import importlib
import io
import json
import threading
import time
import zipfile

from fastapi.testclient import TestClient
import pymupdf
import pytest

from pdfdiff.app import TTL_SECONDS, app


app_module = importlib.import_module("pdfdiff.app")
batch_module = importlib.import_module("pdfdiff.batch")


@pytest.fixture
def client():
    with TestClient(app) as connection:
        yield connection


def make_pdf(lines, *, width=220, height=260, rotation=0, cropbox=None):
    with pymupdf.open() as document:
        for text in lines:
            page = document.new_page(width=width, height=height)
            if text:
                page.insert_text((25, 50), text, fontsize=12)
            if cropbox is not None:
                page.set_cropbox(pymupdf.Rect(cropbox))
            if rotation:
                page.set_rotation(rotation)
        return document.tobytes()


def upload_pair(client, reference, candidate, filename="reference.pdf"):
    ids = []
    for data, name in ((reference, filename), (candidate, "candidate.pdf")):
        response = client.post("/api/documents", params={"filename": name}, content=data)
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    return {"reference_id": ids[0], "candidate_id": ids[1], "dpi": 96}


def start(client, settings):
    response = client.post("/api/batches", json=settings)
    assert response.status_code == 202, response.text
    return response.json()


def wait_terminal(client, key, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/batches/{key}")
        assert response.status_code == 200, response.text
        snapshot = response.json()
        if snapshot["status"] in {"completed", "cancelled", "failed"}:
            return snapshot
        time.sleep(0.02)
    pytest.fail("Batch did not reach a terminal state")


def test_identical_multiple_pages_and_blank_measurements(client):
    data = make_pdf(["First page", "", "Final page"])
    settings = upload_pair(client, data, data)
    initial = start(client, settings)
    assert initial["status"] == "queued"
    assert set(initial["exports"]) == {"json", "html", "zip"}
    result = wait_terminal(client, initial["id"])
    assert result["status"] == "completed"
    assert result["total_pages"] == result["completed_pages"] == 3
    assert result["summary"] == {"compared_pages": 3, "changed_pages": 0, "unchanged_pages": 3, "error_pages": 0}
    assert [page["page"] for page in result["pages"]] == [1, 2, 3]
    assert result["pages"][1]["report"]["text"]["median_dx_mm"] is None
    for page in result["pages"]:
        assert page["report"]["detail_level"] == "summary"
        assert "line_differences" not in page["report"]
        assert "images" not in page["report"]
        assert "files" not in page["report"]
    assert result["settings"]["region_mm"] is None
    assert len(result["files"]["reference"]["sha256"]) == 64


def test_one_changed_page_is_identified(client):
    reference = make_pdf(["Page alpha", "Page beta", "Page gamma"])
    candidate = make_pdf(["Page alpha", "Page changed", "Page gamma"])
    result = wait_terminal(client, start(client, upload_pair(client, reference, candidate))["id"])
    assert result["summary"] == {"compared_pages": 3, "changed_pages": 1, "unchanged_pages": 2, "error_pages": 0}
    assert [page["has_visual_difference"] for page in result["pages"]] == [False, True, False]
    diagnostics = result["pages"][1]["report"]["diagnostics"]
    assert any(finding["type"] == "text.content" for finding in diagnostics["findings"])
    assert "line_differences" not in result["pages"][1]["report"]
    detail = client.post(f"/api/batches/{result['id']}/pages/2/compare").json()
    assert detail["report"]["diagnostics"] == diagnostics
    exported = client.get(result["exports"]["json"]).json()
    assert exported["pages"][1]["report"]["diagnostics"] == diagnostics


def test_full_document_diagnostic_ids_are_unique_across_pages(client):
    reference = make_pdf(["Reference alpha", "Reference beta"])
    candidate = make_pdf(["Candidate alpha", "Candidate beta"])
    result = wait_terminal(client, start(client, upload_pair(client, reference, candidate))["id"])
    findings = [item for page in result["pages"] for item in page["report"]["diagnostics"]["findings"]]
    assert findings
    identifiers = [item["id"] for item in findings]
    assert len(identifiers) == len(set(identifiers))
    for item in findings:
        page = item["reference"]["page"]
        assert item["candidate"]["page"] == page
        assert f"r{page}-c{page}-" in item["id"]


@pytest.mark.parametrize("change", ["size", "rotation", "cropbox"])
def test_geometry_changes_are_detected_even_for_blank_pixels(client, change):
    reference_options = {"width": 200, "height": 200}
    candidate_options = dict(reference_options)
    if change == "size":
        candidate_options["width"] = 201
    elif change == "rotation":
        candidate_options["rotation"] = 90
    else:
        reference_options = {"width": 220, "height": 220, "cropbox": (0, 0, 200, 200)}
        candidate_options = {"width": 220, "height": 220, "cropbox": (10, 0, 210, 200)}
    settings = upload_pair(client, make_pdf([""], **reference_options), make_pdf([""], **candidate_options))
    result = wait_terminal(client, start(client, settings)["id"])
    assert result["pages"][0]["report"]["visual"]["rgb_changed_pixels"] == 0
    assert result["pages"][0]["has_visual_difference"] is True


def test_bad_ids_unequal_page_counts_and_region_are_rejected(client):
    settings = upload_pair(client, make_pdf(["one"]), make_pdf(["one", "two"]))
    assert client.post("/api/batches", json=settings).status_code == 400
    assert not client.app.state.batches.jobs
    assert client.post("/api/batches", json={**settings, "candidate_id": "missing"}).status_code == 404
    assert client.post("/api/batches", json={}).status_code == 422
    assert client.post("/api/batches", json={**settings, "region_mm": None}).status_code == 422
    assert client.get("/api/batches/missing").status_code == 404
    assert client.delete("/api/batches/missing").status_code == 404


def test_progress_cancel_active_conflict_and_running_exports(client, monkeypatch):
    data = make_pdf(["Page one", "Page two", "Page three"])
    settings = upload_pair(client, data, data)
    entered = threading.Event()
    release = threading.Event()
    original = app_module.compare_pages

    def controlled(*args, **kwargs):
        if kwargs["reference_page"] == 2:
            entered.set()
            assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(app_module, "compare_pages", controlled)
    initial = start(client, settings)
    try:
        assert entered.wait(3)
        snapshot = client.get(f"/api/batches/{initial['id']}").json()
        assert snapshot["status"] == "running"
        assert snapshot["completed_pages"] == 1
        assert snapshot["current_page"] == 2
        assert client.post("/api/batches", json=settings).status_code == 409
        partial = client.get(initial["exports"]["json"])
        assert partial.status_code == 200
        assert partial.json()["completed_pages"] == 1
        assert client.get(initial["exports"]["html"]).status_code == 409
        assert client.get(initial["exports"]["zip"]).status_code == 409
        assert client.post(f"/api/batches/{initial['id']}/pages/2/compare").status_code == 409
        assert client.post(f"/api/batches/{initial['id']}/pages/99/compare").status_code == 400
        # 即使 TTL 到期，未完成任务也不能被清理；源上传记录可独立淘汰。
        job = client.app.state.batches.jobs[initial["id"]]
        job.touched = time.monotonic() - TTL_SECONDS - 10
        client.app.state.batches.cleanup()
        client.app.state.store.documents.clear()
        assert initial["id"] in client.app.state.batches.jobs
        cancelled = client.delete(f"/api/batches/{initial['id']}").json()
        assert cancelled["cancel_requested"] is True
    finally:
        release.set()
    result = wait_terminal(client, initial["id"])
    assert result["status"] == "cancelled"
    assert result["completed_pages"] == 2
    assert result["current_page"] is None
    assert len(result["pages"]) == 2
    # 已完成页面仍能使用任务 pin 的源文件打开详情。
    detail = client.post(f"/api/batches/{initial['id']}/pages/1/compare")
    assert detail.status_code == 200
    assert "line_differences" in detail.json()["report"]


def test_page_error_does_not_stop_remaining_pages_or_expose_internal_paths(client, monkeypatch):
    data = make_pdf(["Page one", "Page two", "Page three"])
    settings = upload_pair(client, data, data)
    original = app_module.compare_pages

    def fail_middle(*args, **kwargs):
        if kwargs["reference_page"] == 2:
            raise RuntimeError("private E:/hidden/internal/path.pdf")
        return original(*args, **kwargs)

    monkeypatch.setattr(app_module, "compare_pages", fail_middle)
    result = wait_terminal(client, start(client, settings)["id"])
    assert result["status"] == "completed"
    assert result["completed_pages"] == 3
    assert result["summary"] == {"compared_pages": 2, "changed_pages": 0, "unchanged_pages": 2, "error_pages": 1}
    assert result["pages"][1]["status"] == "error"
    assert result["pages"][1]["report"] is None
    assert result["pages"][1]["has_visual_difference"] is None
    assert "hidden" not in json.dumps(result)
    assert client.post(f"/api/batches/{result['id']}/pages/2/compare").status_code == 400


def test_summary_survives_document_and_single_result_cache_eviction(client):
    data = make_pdf(["A stable page"])
    result = wait_terminal(client, start(client, upload_pair(client, data, data))["id"])
    client.app.state.store.documents.clear()
    first_detail = None
    for _ in range(5):
        response = client.post(f"/api/batches/{result['id']}/pages/1/compare")
        assert response.status_code == 200
        payload = response.json()
        first_detail = first_detail or payload
        assert payload["report"]["settings"]["dpi"] == 96
        assert set(payload) == {"id", "report", "images", "exports"}
    assert client.get(first_detail["exports"]["json"]).status_code == 404
    after = client.get(f"/api/batches/{result['id']}").json()
    assert after["pages"] == result["pages"]
    assert after["summary"] == result["summary"]
    regenerated = client.post(f"/api/batches/{result['id']}/pages/1/compare").json()
    assert client.get(regenerated["images"]["reference"]).status_code == 200


def test_completed_job_lru_and_ttl(client):
    data = make_pdf(["Cache lifetime"])
    settings = upload_pair(client, data, data)
    keys = [wait_terminal(client, start(client, settings)["id"])["id"] for _ in range(5)]
    assert len(client.app.state.batches.jobs) == 4
    assert client.get(f"/api/batches/{keys[0]}").status_code == 404
    last = client.app.state.batches.jobs[keys[-1]]
    last.touched = time.monotonic() - TTL_SECONDS - 1
    assert client.get(f"/api/batches/{keys[-1]}").status_code == 404


def test_report_size_limit_retains_completed_pages(client, monkeypatch):
    data = make_pdf(["Page one", "Page two", "Page three"])
    settings = upload_pair(client, data, data)
    entered, release = threading.Event(), threading.Event()
    original = app_module.compare_pages

    def controlled(*args, **kwargs):
        if kwargs["reference_page"] == 2:
            entered.set()
            assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(app_module, "compare_pages", controlled)
    initial = start(client, settings)
    try:
        assert entered.wait(3)
        job = client.app.state.batches.jobs[initial["id"]]
        monkeypatch.setattr(batch_module, "MAX_BATCH_REPORT_BYTES", job.report_bytes + 8)
    finally:
        release.set()
    result = wait_terminal(client, initial["id"])
    assert result["status"] == "failed"
    assert result["completed_pages"] == 1
    assert result["pages"][0]["page"] == 1
    assert "64 MiB" in result["error"]


def test_single_page_detail_can_run_between_batch_pages(client, monkeypatch):
    data = make_pdf(["Page one", "Page two", "Page three"])
    settings = upload_pair(client, data, data)
    page_two, page_three = threading.Event(), threading.Event()
    release_two, release_three = threading.Event(), threading.Event()
    detail_entered = threading.Event()
    original_engine = app_module.compare_pages
    original_render = app_module.render_comparison

    def controlled(*args, **kwargs):
        if kwargs["reference_page"] == 2:
            page_two.set()
            assert release_two.wait(5)
        elif kwargs["reference_page"] == 3:
            page_three.set()
            assert release_three.wait(5)
        return original_engine(*args, **kwargs)

    async def detail_render(*args, **kwargs):
        detail_entered.set()
        return await original_render(*args, **kwargs)

    monkeypatch.setattr(app_module, "compare_pages", controlled)
    monkeypatch.setattr(app_module, "render_comparison", detail_render)
    initial = start(client, settings)
    try:
        assert page_two.wait(3)
        with ThreadPoolExecutor(max_workers=1) as requests:
            detail = requests.submit(client.post, f"/api/batches/{initial['id']}/pages/1/compare")
            assert detail_entered.wait(3)
            release_two.set()
            response = detail.result(timeout=3)
            assert response.status_code == 200
            assert page_three.wait(3)
            assert client.get(f"/api/batches/{initial['id']}").json()["status"] == "running"
    finally:
        release_two.set()
        release_three.set()
    assert wait_terminal(client, initial["id"])["status"] == "completed"


def test_batch_exports_round_trip_without_all_page_images(client):
    data = make_pdf(["Page one", "Page two"])
    settings = upload_pair(client, data, data, filename="<label>.pdf")
    result = wait_terminal(client, start(client, settings)["id"])
    report_response = client.get(result["exports"]["json"])
    assert report_response.status_code == 200
    report = report_response.json()
    assert report["kind"] == "document"
    assert report["summary"] == result["summary"]
    assert report["settings"] == result["settings"]
    html = client.get(result["exports"]["html"])
    assert html.status_code == 200
    assert "<label>.pdf" not in html.text
    assert "data:image/png;base64," not in html.text
    bundle_response = client.get(result["exports"]["zip"])
    assert bundle_response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(bundle_response.content)) as archive:
        assert json.loads(archive.read("report.json")) == report
        assert "report.html" in archive.namelist()
        page_jsons = [name for name in archive.namelist() if name.startswith("pages/") and name.endswith(".json")]
        assert len(page_jsons) == 2
        assert not any(name.endswith(".png") for name in archive.namelist())
    assert report_response.headers["cache-control"] == "no-store"


def test_batch_endpoints_preserve_same_origin_boundary(client):
    assert client.post("/api/batches", json={}, headers={"origin": "https://example.com"}).status_code == 403
    assert client.get("/api/batches/missing", headers={"sec-fetch-site": "cross-site"}).status_code == 403


def test_lifespan_cancels_and_awaits_current_page_before_releasing_jobs(monkeypatch):
    data = make_pdf(["Page one", "Page two"])
    entered, release = threading.Event(), threading.Event()
    shutting_down, rendered = threading.Event(), threading.Event()
    original_engine = app_module.compare_pages
    original_shutdown = batch_module.BatchManager.shutdown

    def controlled(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        result = original_engine(*args, **kwargs)
        rendered.set()
        return result

    async def observed_shutdown(manager):
        shutting_down.set()
        await original_shutdown(manager)

    def release_on_shutdown():
        shutting_down.wait(5)
        release.set()

    monkeypatch.setattr(app_module, "compare_pages", controlled)
    monkeypatch.setattr(batch_module.BatchManager, "shutdown", observed_shutdown)
    watcher = threading.Thread(target=release_on_shutdown, daemon=True)
    try:
        with TestClient(app) as connection:
            initial = start(connection, upload_pair(connection, data, data))
            job = connection.app.state.batches.jobs[initial["id"]]
            try:
                assert entered.wait(3)
            finally:
                watcher.start()
        watcher.join(timeout=3)
        assert shutting_down.is_set()
        assert rendered.is_set()
        assert job.task.done()
        assert job.status == "cancelled"
        assert len(job.pages) == 1
        assert not connection.app.state.batches.jobs
        assert not connection.app.state.store.documents
    finally:
        release.set()
