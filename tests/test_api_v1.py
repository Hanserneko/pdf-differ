"""使用真实 PDF 与临时文件验证机器调用 API 的快照和结果语义。"""

import hashlib
import importlib
import os
from pathlib import Path
import stat
import threading
import time

from fastapi.testclient import TestClient
import pymupdf
import pytest

from pdfdiff.app import MAX_FILE_BYTES, app


app_module = importlib.import_module("pdfdiff.app")
BASE = "/api/v1/comparisons"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app.state, "configured_roots", (tmp_path.resolve(),), raising=False)
    with TestClient(app) as connection:
        connection.app.state.allowed_roots = (tmp_path.resolve(),)
        yield connection


def pdf_bytes(lines=("First stable line", "Second stable line"), *, dx_mm=0):
    with pymupdf.open() as document:
        for text in lines:
            page = document.new_page(width=220, height=260)
            if text:
                page.insert_text((25 + dx_mm * 72 / 25.4, 50), text, fontsize=12)
        return document.tobytes()


def source_paths(directory, reference=None, candidate=None):
    reference = pdf_bytes() if reference is None else reference
    candidate = reference if candidate is None else candidate
    left, right = directory / "参考 document.pdf", directory / "candidate.pdf"
    left.write_bytes(reference)
    right.write_bytes(candidate)
    return left, right


def request_body(paths, **extra):
    left, right = paths
    return {
        "reference": {"path": str(left.resolve())},
        "candidate": {"path": str(right.resolve())},
        "settings": {"dpi": 96, "ink_threshold": 220},
        **extra,
    }


def create(client, body, **kwargs):
    response = client.post(BASE, json=body, **kwargs)
    assert response.status_code == 202, response.text
    payload = response.json()
    assert response.headers["location"].endswith(f"{BASE}/{payload['id']}")
    assert response.headers["cache-control"] == "no-store"
    return payload


def terminal(client, key, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"{BASE}/{key}")
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in {"completed", "cancelled", "failed"}:
            return payload
        time.sleep(0.02)
    pytest.fail("Comparison did not finish within the test deadline")


def report_for(client, key):
    response = client.get(f"{BASE}/{key}/report")
    assert response.status_code == 200, response.text
    return response.json()


def problem(response, status, code):
    assert response.status_code == status, response.text
    assert response.headers["content-type"].split(";", 1)[0] == "application/problem+json"
    value = response.json()
    assert value["status"] == status
    assert value["code"] == code
    assert isinstance(value["detail"], str) and value["detail"]
    assert isinstance(value["retryable"], bool)
    assert {"type", "title", "instance"} <= value.keys()
    return value


def test_paths_all_returns_compact_status_and_complete_machine_report(client, tmp_path):
    paths = source_paths(tmp_path, candidate=pdf_bytes(dx_mm=1))
    initial = create(client, request_body(paths))
    status = terminal(client, initial["id"])
    assert status["status"] == "completed"
    assert status["progress"] == {"total_pairs": 2, "processed_pairs": 2, "succeeded_pairs": 2, "failed_pairs": 0, "current_pair_index": None}
    assert {"self", "report", "cancel"} <= status["links"].keys()
    assert all(status[key] for key in ("created_at", "updated_at", "expires_at"))
    assert "pairs" not in status
    report = report_for(client, initial["id"])
    assert report["coverage"]["all_pairs_compared"] is True
    assert report["coverage"]["unprocessed_pairs"] == 0
    assert len(report["pairs"]) == 2
    assert report["files"]["reference"]["sha256"] == hashlib.sha256(paths[0].read_bytes()).hexdigest()
    assert report["files"]["candidate"]["size_bytes"] == paths[1].stat().st_size
    assert report["files"]["reference"]["read_at"]
    assert report["settings"]["dpi"] == 96
    pair = report["pairs"][0]
    assert pair["index"] == pair["reference_page"] == pair["candidate_page"] == 1
    assert pair["status"] == "done" and pair["error"] is None
    assert pair["report"]["text"]["median_dx_mm"] == pytest.approx(1, abs=0.001)
    assert "line_differences" in pair["report"]
    assert pair["report"]["diagnostics"]["findings"]
    assert set(pair["images"]) == {"reference", "candidate", "overlay", "diff"}
    image = client.get(pair["images"]["reference"])
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")


def test_explicit_pairs_support_different_counts_and_single_region(client, tmp_path):
    paths = source_paths(tmp_path, pdf_bytes(["Shared anchor"]), pdf_bytes(["Other page", "Shared anchor"]))
    body = request_body(paths, pages={"mode": "pairs", "pairs": [{"reference_page": 1, "candidate_page": 2}]}, region_mm=[5, 10, 60, 25])
    result = terminal(client, create(client, body)["id"])
    report = report_for(client, result["id"])
    pair = report["pairs"][0]
    assert pair["index"] == 1 and pair["reference_page"] == 1 and pair["candidate_page"] == 2
    assert pair["report"]["reference"]["page"] == 1
    assert pair["report"]["candidate"]["page"] == 2
    assert pair["report"]["settings"]["region_mm"] == [5, 10, 60, 25]
    assert pair["report"]["text"]["matched_characters"] > 5
    legacy_detail = client.post(f"/api/batches/{result['id']}/pages/1/compare")
    assert legacy_detail.status_code == 200
    assert legacy_detail.json()["report"]["candidate"]["page"] == 2
    assert legacy_detail.json()["report"]["settings"]["region_mm"] == [5, 10, 60, 25]


def test_document_ids_and_mixed_sources_work_with_existing_uploads(client, tmp_path):
    data = pdf_bytes(["Upload interoperability"])
    uploaded = client.post("/api/documents", content=data).json()
    paths = source_paths(tmp_path, data, data)
    body = request_body(paths)
    body["reference"] = {"document_id": uploaded["id"]}
    result = terminal(client, create(client, body)["id"])
    assert result["summary"]["visual_unchanged_pairs"] == 1


def test_unequal_all_and_invalid_pair_selection_have_problem_details(client, tmp_path):
    paths = source_paths(tmp_path, pdf_bytes(["one"]), pdf_bytes(["one", "two"]))
    problem(client.post(BASE, json=request_body(paths)), 422, "page_count_mismatch")
    body = request_body(paths, pages={"mode": "pairs", "pairs": [{"reference_page": 9, "candidate_page": 1}]})
    problem(client.post(BASE, json=body), 422, "invalid_request")
    body["pages"]["pairs"] = []
    problem(client.post(BASE, json=body), 422, "invalid_request")


@pytest.mark.parametrize("source", [{}, {"path": "some.pdf", "document_id": "some-id"}, {"url": "https://example.com/file.pdf"}])
def test_source_union_requires_exactly_one_supported_source(client, tmp_path, source):
    body = request_body(source_paths(tmp_path))
    body["reference"] = source
    problem(client.post(BASE, json=body), 422, "invalid_request")


def test_paths_are_restricted_to_allowed_roots_and_regular_pdf_files(client, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    paths = source_paths(allowed)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(pdf_bytes())
    client.app.state.allowed_roots = (allowed.resolve(),)
    body = request_body(paths)
    body["reference"] = {"path": str(outside.resolve())}
    problem(client.post(BASE, json=body), 403, "source_access_denied")
    body["reference"] = {"path": str((allowed / "missing.pdf").resolve())}
    problem(client.post(BASE, json=body), 404, "source_not_found")
    body["reference"] = {"path": str(allowed.resolve())}
    problem(client.post(BASE, json=body), 403, "source_access_denied")
    invalid = allowed / "broken.pdf"
    invalid.write_bytes(b"%PDF-broken-content")
    body["reference"] = {"path": str(invalid.resolve())}
    problem(client.post(BASE, json=body), 422, "invalid_pdf")


def test_resolved_symlink_cannot_escape_allowed_root(client, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    paths = source_paths(allowed)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(pdf_bytes())
    link = allowed / "linked.pdf"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"The platform does not allow creating test symlinks: {error}")
    client.app.state.allowed_roots = (allowed.resolve(),)
    body = request_body(paths)
    body["reference"] = {"path": str(link.absolute())}
    problem(client.post(BASE, json=body), 403, "source_access_denied")


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction regression")
def test_windows_directory_junction_cannot_escape_allowed_root(client, tmp_path):
    import _winapi

    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    paths = source_paths(allowed)
    target = outside / "document.pdf"
    target.write_bytes(pdf_bytes())
    junction = allowed / "linked-directory"
    _winapi.CreateJunction(str(outside.resolve()), str(junction.absolute()))
    try:
        assert junction.lstat().st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
        assert (junction / target.name).resolve() == target.resolve()
        client.app.state.allowed_roots = (allowed.resolve(),)
        body = request_body(paths)
        body["reference"] = {"path": str(junction / target.name)}
        problem(client.post(BASE, json=body), 403, "source_access_denied")
    finally:
        # 移除目录联接本身，目标目录由 pytest 临时目录机制清理。
        junction.rmdir()


def test_oversized_source_is_rejected_before_pdf_parsing(client, tmp_path):
    paths = source_paths(tmp_path)
    oversized = tmp_path / "large.pdf"
    with oversized.open("wb") as stream:
        stream.truncate(MAX_FILE_BYTES + 1)
    body = request_body(paths)
    body["reference"] = {"path": str(oversized.resolve())}
    problem(client.post(BASE, json=body), 413, "source_too_large")


@pytest.mark.parametrize("path,status,code", [
    ("relative.pdf", 422, "invalid_request"),
    ("C:relative.pdf", 422, "invalid_request"),
    ("file:///not-a-source.pdf", 403, "source_access_denied"),
    (r"\\never-connect.invalid\share\document.pdf", 403, "source_access_denied"),
    (r"\\.\NUL", 403, "source_access_denied"),
])
def test_relative_url_network_and_device_paths_are_rejected(client, tmp_path, path, status, code):
    body = request_body(source_paths(tmp_path))
    body["reference"] = {"path": path}
    problem(client.post(BASE, json=body), status, code)


def test_source_modified_during_snapshot_read_is_retryable_and_releases_busy_slot(client, tmp_path, monkeypatch):
    api = importlib.import_module("pdfdiff.api_v1")
    paths = source_paths(tmp_path)
    target = paths[0].resolve()
    replacement = pdf_bytes(["Changed source bytes during the snapshot read"])
    opened_path = api._opened_path
    changed = False

    def change_after_open(stream, fallback):
        nonlocal changed
        actual = opened_path(stream, fallback)
        if actual == target and not changed:
            changed = True
            target.write_bytes(replacement)
        return actual

    monkeypatch.setattr(api, "_opened_path", change_after_open)
    body = request_body(paths)
    detail = problem(client.post(BASE, json=body), 409, "source_changed")
    assert detail["retryable"] is True
    monkeypatch.setattr(api, "_opened_path", opened_path)
    # 修复来源后能立即提交，准备阶段失败不能留下占用名额。
    paths[1].write_bytes(replacement)
    result = terminal(client, create(client, body)["id"])
    assert result["progress"]["succeeded_pairs"] == 1


def test_roi_only_applies_to_one_pair(client, tmp_path):
    paths = source_paths(tmp_path)
    problem(client.post(BASE, json=request_body(paths, region_mm=[5, 10, 60, 25])), 422, "invalid_request")
    body = request_body(paths, pages={"mode": "pairs", "pairs": [{"reference_page": 1, "candidate_page": 1}]}, region_mm=[10, 10, 5, 20])
    problem(client.post(BASE, json=body), 422, "invalid_request")


def test_problem_envelope_covers_missing_results_and_cross_origin_requests(client):
    problem(client.get("/api/v1/unknown-route"), 404, "route_not_found")
    wrong_method = client.get(BASE)
    problem(wrong_method, 405, "method_not_allowed")
    assert "POST" in wrong_method.headers["allow"]
    problem(client.get(f"{BASE}/missing"), 404, "comparison_not_found")
    problem(client.get(f"{BASE}/missing/report"), 404, "comparison_not_found")
    response = client.post(BASE, json={}, headers={"origin": "https://example.com"})
    problem(response, 403, "origin_not_allowed")
    assert client.get("/").status_code == 200
    assert client.get("/api/health").status_code == 200


def test_progress_idempotent_retry_and_cancel_keep_the_original_snapshot(client, tmp_path, monkeypatch):
    reference = pdf_bytes()
    original_candidate = pdf_bytes(dx_mm=1)
    paths = source_paths(tmp_path, reference, original_candidate)
    body = request_body(paths)
    key = {"Idempotency-Key": "snapshot.retry-01"}
    entered, release = threading.Event(), threading.Event()
    original = app_module.compare_pages

    def controlled(*args, **kwargs):
        if kwargs["reference_page"] == 1:
            entered.set()
            assert release.wait(8)
        return original(*args, **kwargs)

    monkeypatch.setattr(app_module, "compare_pages", controlled)
    initial = create(client, body, headers=key)
    try:
        assert entered.wait(3)
        status = client.get(f"{BASE}/{initial['id']}").json()
        assert status["progress"]["processed_pairs"] == 0
        assert status["progress"]["current_pair_index"] == 1
        problem(client.get(f"{BASE}/{initial['id']}/report"), 409, "result_not_ready")
        problem(client.get(f"{BASE}/{initial['id']}/pairs/1"), 409, "pair_not_ready")
        paths[1].write_bytes(b"%PDF-now-invalid-on-disk")
        repeated = create(client, body, headers=key)
        assert repeated["id"] == initial["id"]
        changed_body = {**body, "settings": {"dpi": 150, "ink_threshold": 220}}
        problem(client.post(BASE, json=changed_body, headers=key), 409, "idempotency_conflict")
        problem(client.post(BASE, json=body), 409, "comparison_busy")
        cancelled = client.post(f"{BASE}/{initial['id']}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["cancel_requested"] is True
    finally:
        release.set()
    status = terminal(client, initial["id"])
    assert status["status"] == "cancelled"
    assert status["progress"]["processed_pairs"] == 1
    report = report_for(client, initial["id"])
    assert report["files"]["candidate"]["sha256"] == hashlib.sha256(original_candidate).hexdigest()
    assert report["coverage"]["unprocessed_pairs"] == 1
    assert report["coverage"]["all_pairs_compared"] is False
    pair = client.get(f"{BASE}/{initial['id']}/pairs/1").json()
    assert pair["report"]["text"]["median_dx_mm"] == pytest.approx(1, abs=0.001)
    assert client.get(pair["images"]["candidate"]).status_code == 200
    assert create(client, body, headers=key)["id"] == initial["id"]
    paths[1].write_bytes(pdf_bytes(dx_mm=2))
    fresh = create(client, body)
    terminal(client, fresh["id"])
    fresh_report = report_for(client, fresh["id"])
    assert fresh_report["files"]["candidate"]["sha256"] != report["files"]["candidate"]["sha256"]
    assert fresh_report["pairs"][0]["report"]["text"]["median_dx_mm"] == pytest.approx(2, abs=0.001)


@pytest.mark.parametrize("value", ["", "contains spaces", "x" * 129, "slash/not-valid"])
def test_invalid_idempotency_keys_have_structured_errors(client, tmp_path, value):
    body = request_body(source_paths(tmp_path))
    problem(client.post(BASE, json=body, headers={"Idempotency-Key": value}), 422, "invalid_request")


def test_page_failures_continue_and_are_distinct_from_unprocessed_pairs(client, tmp_path, monkeypatch):
    paths = source_paths(tmp_path, pdf_bytes(["first", "second", "third"]))
    original = app_module.compare_pages

    def fail_middle(*args, **kwargs):
        if kwargs["reference_page"] == 2:
            raise RuntimeError("secret internal filesystem detail")
        return original(*args, **kwargs)

    monkeypatch.setattr(app_module, "compare_pages", fail_middle)
    initial = create(client, request_body(paths))
    status = terminal(client, initial["id"])
    assert status["status"] == "completed"
    assert status["progress"]["processed_pairs"] == 3
    assert status["progress"]["succeeded_pairs"] == 2
    assert status["progress"]["failed_pairs"] == 1
    assert status["summary"]["error_pairs"] == 1
    report = report_for(client, initial["id"])
    assert report["coverage"]["unprocessed_pairs"] == 0
    assert report["coverage"]["all_pairs_compared"] is False
    assert report["pairs"][1]["status"] == "error"
    assert report["pairs"][1]["report"] is None
    assert "secret internal" not in str(report)
    failed_pair = client.get(f"{BASE}/{initial['id']}/pairs/2")
    assert failed_pair.status_code == 200
    assert failed_pair.json()["status"] == "error"
    problem(client.get(f"{BASE}/{initial['id']}/pairs/2/images/reference"), 409, "pair_comparison_failed")
    problem(client.get(f"{BASE}/{initial['id']}/pairs/99"), 404, "pair_not_found")


def test_diagnostic_truncation_does_not_claim_report_completeness(client, tmp_path, monkeypatch):
    diagnostics = importlib.import_module("pdfdiff.diagnostics")
    monkeypatch.setattr(diagnostics, "MAX_FINDINGS", 1)
    paths = source_paths(tmp_path, pdf_bytes(["Unique anchor"]), pdf_bytes(["Unique anchor"], dx_mm=1))
    initial = create(client, request_body(paths))
    terminal(client, initial["id"])
    report = report_for(client, initial["id"])
    assert report["coverage"]["all_pairs_compared"] is True
    assert report["coverage"]["diagnostics_truncated"] is True
    assert report["coverage"]["truncated"] is True
    assert report["coverage"]["diagnostics_truncated_pairs"]
    assert report["pairs"][0]["report"]["diagnostics"]["truncated"] is True


@pytest.mark.parametrize("start_with_v1", [True, False])
def test_new_and_existing_document_jobs_share_one_busy_slot(client, tmp_path, monkeypatch, start_with_v1):
    data = pdf_bytes()
    paths = source_paths(tmp_path, data, data)
    ids = [client.post("/api/documents", content=data).json()["id"] for _ in range(2)]
    legacy = {"reference_id": ids[0], "candidate_id": ids[1], "dpi": 96}
    entered, release = threading.Event(), threading.Event()
    original = app_module.compare_pages

    def controlled(*args, **kwargs):
        entered.set()
        assert release.wait(8)
        return original(*args, **kwargs)

    monkeypatch.setattr(app_module, "compare_pages", controlled)
    initial = create(client, request_body(paths)) if start_with_v1 else client.post("/api/batches", json=legacy).json()
    try:
        assert entered.wait(3)
        if start_with_v1:
            assert client.post("/api/batches", json=legacy).status_code == 409
            assert client.post(f"{BASE}/{initial['id']}/cancel").status_code == 200
        else:
            problem(client.post(BASE, json=request_body(paths)), 409, "comparison_busy")
            assert client.delete(f"/api/batches/{initial['id']}").status_code == 200
    finally:
        release.set()
    if start_with_v1:
        terminal(client, initial["id"])

