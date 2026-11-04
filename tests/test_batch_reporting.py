"""整份文档摘要的状态语义、离线安全及 ZIP 数据完整性。"""

from copy import deepcopy
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import PurePosixPath
from zipfile import ZipFile

from pdfdiff.batch_reporting import build_batch_bundle, build_batch_html


class _StatsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "strong":
            self.current = dict(attrs).get("data-stat")

    def handle_data(self, data):
        if self.current:
            self.values[self.current] = data

    def handle_endtag(self, tag):
        if tag == "strong":
            self.current = None


def _stats(html):
    parser = _StatsParser()
    parser.feed(html)
    return parser.values


def _entry(page, changed=False):
    return {
        "page": page,
        "status": "done",
        "has_visual_difference": changed,
        "error": None,
        "report": {
            "schema_version": 1,
            "detail_level": "summary",
            "settings": {"dpi": 150, "ink_threshold": 220, "region_mm": None},
            "reference": {"page": page, "width_mm": 210, "height_mm": 297},
            "candidate": {"page": page, "width_mm": 210, "height_mm": 297},
            "visual": {"foreground_mismatch_ratio": 0.25 if changed else 0, "rgb_changed_pixels": 12 if changed else 0},
            "text": {"median_dx_mm": 1, "median_dy_mm": 0.5, "p95_position_error_mm": 1.2, "line_spacing_delta_mm": 0.12, "font_size_delta_pt": 0, "matched_characters": 80},
            "warnings": [],
        },
    }


def _report():
    return {
        "schema_version": 1,
        "kind": "document",
        "status": "completed",
        "total_pages": 2,
        "completed_pages": 2,
        "summary": {"compared_pages": 2, "changed_pages": 1, "unchanged_pages": 1, "error_pages": 0},
        "pages": [_entry(1, True), _entry(2)],
        "settings": {"dpi": 150, "ink_threshold": 220, "region_mm": None},
        "files": {"reference": {"filename": "reference.pdf", "sha256": "a" * 64}, "candidate": {"filename": "candidate.pdf", "sha256": "b" * 64}},
        "generated_at": "2026-11-03T12:00:00+00:00",
        "engine": {"app": "0.2.0", "pdf": "1.28"},
        "error": None,
    }


def test_completed_report_shows_strict_visual_counts_and_measurements():
    report = _report()
    html = build_batch_html(report)
    assert _stats(html) == {
        "total_pages": "2", "processed_pages": "2", "changed_pages": "1",
        "unchanged_pages": "1", "error_pages": "0", "unfinished_pages": "0",
    }
    assert "25.00 %" in html
    assert "+1.000" in html
    assert "+0.120" in html
    assert "参考文档" in html and "待比较文档" in html
    assert "PDF Layout Diff" in html
    assert "像素或页面几何差异" in html
    assert "data:image" not in html and "<img" not in html
    assert "不包含页图或逐行详情" in html
    assert "请在应用中选择对应页" in html


def test_cancelled_partial_report_does_not_count_unprocessed_pages_as_unchanged():
    report = _report()
    report.update(status="cancelled", total_pages=8, completed_pages=1)
    report["pages"] = [_entry(1, True)]
    report["summary"] = {"compared_pages": 1, "changed_pages": 1, "unchanged_pages": 0, "error_pages": 0}
    html = build_batch_html(report)
    stats = _stats(html)
    assert stats["processed_pages"] == "1"
    assert stats["unfinished_pages"] == "7"
    assert stats["unchanged_pages"] == "0"
    assert "已取消" in html
    assert "不能判断这些页是否存在视觉差异" in html


def test_error_entries_ignore_any_old_metrics_and_false_visual_flag():
    report = _report()
    report.update(status="failed", total_pages=3, completed_pages=2)
    bad_entry = _entry(2)
    bad_entry.update(status="error", error="无法渲染该页")
    report["pages"] = [_entry(1, True), bad_entry]
    report["summary"] = {"compared_pages": 1, "changed_pages": 1, "unchanged_pages": 0, "error_pages": 1}
    report["error"] = "任务提前结束"
    html = build_batch_html(report)
    stats = _stats(html)
    assert stats["processed_pages"] == "2"
    assert stats["unchanged_pages"] == "0"
    assert stats["error_pages"] == "1"
    assert stats["unfinished_pages"] == "1"
    assert "任务失败" in html
    assert "处理错误：无法渲染该页" in html
    assert "任务错误：任务提前结束" in html


def test_blank_metrics_and_unknown_visual_status_are_not_zero_or_unchanged():
    report = _report()
    entry = _entry(1)
    entry["has_visual_difference"] = None
    entry["report"]["visual"] = {"foreground_mismatch_ratio": None, "rgb_changed_pixels": None}
    entry["report"]["text"] = None
    report.update(total_pages=1, completed_pages=1, pages=[entry], summary={})
    html = build_batch_html(report)
    assert _stats(html)["unchanged_pages"] == "0"
    assert "视觉状态未知" in html
    assert "不可测" in html
    assert "0.00 %" not in html
    assert "没有可用的文字定位结果" in html


def test_untrusted_file_names_errors_warnings_and_json_are_escaped():
    report = _report()
    payload = '<script>alert("unsafe")</script>'
    report["files"]["reference"]["filename"] = payload
    report["files"]["candidate"]["sha256"] = '<img src=x onerror="alert(1)">'
    report["pages"][0]["report"]["warnings"] = ['<svg onload="alert(2)">']
    report["pages"][1]["error"] = payload
    report["error"] = payload
    report["settings"]["dpi"] = payload
    report["engine"] = {"value": "</pre><iframe src=x>"}
    report["generated_at"] = payload
    html = build_batch_html(report)
    assert "<script" not in html
    assert "<img" not in html
    assert "<svg" not in html
    assert "<iframe" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;svg" in html
    assert "&lt;iframe" in html
    assert "https://" not in html and "http://" not in html


def test_bundle_preserves_all_json_and_does_not_include_source_or_page_images():
    report = _report()
    report["pages"][1].update(status="error", error="读取失败", report=None, has_visual_difference=None)
    before = deepcopy(report)
    with ZipFile(BytesIO(build_batch_bundle(report))) as archive:
        assert set(archive.namelist()) == {"report.json", "report.html", "pages/page-0001.json", "pages/page-0002.json"}
        assert json.loads(archive.read("report.json")) == report
        assert archive.read("report.html").decode("utf-8") == build_batch_html(report)
        assert json.loads(archive.read("pages/page-0001.json")) == report["pages"][0]
        assert json.loads(archive.read("pages/page-0002.json")) == report["pages"][1]
    assert report == before


def test_duplicate_and_invalid_page_numbers_use_safe_unique_archive_names():
    report = _report()
    report["pages"] = [_entry(1), _entry(1), _entry("../../outside"), _entry(True), _entry(0), _entry(99)]
    with ZipFile(BytesIO(build_batch_bundle(report))) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)) == 8
        assert "pages/page-0001.json" in names
        assert all(not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts for name in names)
        for index, entry in enumerate(report["pages"], start=1):
            filename = "pages/page-0001.json" if index == 1 else f"pages/entry-{index:04d}.json"
            assert json.loads(archive.read(filename)) == entry
    html = build_batch_html(report)
    assert _stats(html)["unchanged_pages"] == "0"
    assert _stats(html)["processed_pages"] == "0"
    assert "非法或重复页码" in html


def test_queued_and_inconsistent_completed_reports_never_invent_completed_pages():
    report = _report()
    report.update(status="queued", total_pages=4, completed_pages=0, summary={}, pages=[])
    html = build_batch_html(report)
    assert _stats(html)["unfinished_pages"] == "4"
    assert "等待开始" in html
    assert "尚无已处理的逐页记录" in html
    report.update(status="completed", completed_pages=4, summary={"unchanged_pages": 4})
    html = build_batch_html(report)
    assert _stats(html)["unchanged_pages"] == "0"
    assert "仍有待核查页" in html
    assert "概要计数与可核查的逐页记录不一致" in html


def test_unknown_total_stays_unavailable_instead_of_zero_unfinished_pages():
    report = _report()
    report["total_pages"] = None
    html = build_batch_html(report)
    assert _stats(html)["total_pages"] == "不可用"
    assert _stats(html)["unfinished_pages"] == "不可用"
    assert "总页数不可用" in html
    assert "页数待核查" in html
