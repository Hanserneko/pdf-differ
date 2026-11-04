"""离线报告的数据完整性与转义边界。"""

import base64
from html.parser import HTMLParser
from io import BytesIO
import json
from zipfile import ZipFile

from pdfdiff.demo import make_demo_pair
from pdfdiff.reporting import build_bundle, build_html
import pymupdf


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aE7cAAAAASUVORK5CYII=")


class _MarkerParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.markers = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "span" and attributes.get("class") == "roi-box":
            self.markers.append(attributes)


def _report():
    return {
        "schema_version": 1,
        "settings": {"dpi": 150, "ink_threshold": 220, "region_mm": [20, 30, 180, 190]},
        "reference": {"page": 1, "width_mm": 210, "height_mm": 297},
        "candidate": {"page": 2, "width_mm": 210, "height_mm": 297},
        "files": {"reference": {"filename": '<script>alert("file")</script>.pdf', "sha256": "abc"}},
        "generated_at": "2026-11-03T12:00:00+00:00",
        "visual": {"foreground_mismatch_ratio": 0.25, "ssim": None},
        "text": {"matched_characters": 10, "median_dx_mm": 0, "median_dy_mm": -0.000001, "line_spacing_delta_mm": None},
        "line_differences": [{"text": '<img src=x onerror="alert(1)">', "candidate_text": "变更后的文字", "dx_mm": 0.5, "dy_mm": -0.2}],
        "warnings": ['<svg onload="alert(2)">'],
        "engine": {"version": '<script>alert("version")</script>'},
    }


def test_html_escapes_untrusted_report_text():
    html = build_html(_report(), {"overlay": PNG})
    assert "<script>" not in html
    assert "<svg onload=" not in html
    assert '<img src=x onerror="' not in html
    assert "&lt;script&gt;" in html
    assert "&lt;svg onload=" in html
    assert "&lt;img src=x onerror=" in html
    assert "data:image/png;base64," in html
    assert "https://" not in html
    assert "<script" not in html


def test_html_keeps_unmeasurable_values_distinct_from_zero():
    html = build_html(_report(), {})
    assert "不可测" in html
    assert "+0.000" in html
    assert "-0.000" not in html
    assert "25.00 %" in html
    assert "20.00, 30.00, 180.00, 190.00 mm" in html
    assert "不能解释为整体一致程度的百分比" in html


def test_bundle_preserves_json_and_only_includes_expected_files():
    report = _report()
    images = {key: PNG for key in ("reference", "candidate", "overlay", "diff")}
    with ZipFile(BytesIO(build_bundle(report, images))) as archive:
        assert set(archive.namelist()) == {"report.json", "report.html", "reference.png", "candidate.png", "overlay.png", "diff.png"}
        assert json.loads(archive.read("report.json")) == report
        assert archive.read("report.html").decode("utf-8") == build_html(report, images)
        assert archive.read("diff.png") == PNG


def test_empty_report_is_still_readable_and_self_contained():
    html = build_html({}, {})
    assert "PDF 比较报告" in html
    assert "PDF Layout Diff" in html
    assert "没有可可靠匹配的记录" in html
    assert "此图未生成" in html


def test_region_marker_uses_common_canvas_dimensions_on_all_images():
    report = _report()
    report["reference"].update({"width_mm": 200, "height_mm": 100})
    report["candidate"].update({"width_mm": 100, "height_mm": 200})
    report["settings"]["region_mm"] = [50, 20, 150, 120]
    images = {key: PNG for key in ("reference", "candidate", "overlay", "diff")}
    parser = _MarkerParser()
    html = build_html(report, images)
    parser.feed(html)
    assert len(parser.markers) == 4
    for marker in parser.markers:
        values = dict(item.split(":") for item in marker["style"].rstrip(";").split(";"))
        assert {key: float(value.rstrip("%")) for key, value in values.items()} == {
            "left": 25.0, "top": 10.0, "width": 50.0, "height": 50.0,
        }
    assert "绿色虚线框为测量区域" in html


def test_full_page_report_does_not_mark_a_region():
    report = _report()
    report["settings"]["region_mm"] = None
    parser = _MarkerParser()
    html = build_html(report, {"overlay": PNG})
    parser.feed(html)
    assert parser.markers == []
    assert "当前为全页测量" in html


def test_declared_canvas_is_used_instead_of_individual_page_sizes():
    report = _report()
    report["canvas"] = {"width_mm": 400, "height_mm": 400}
    report["settings"]["region_mm"] = [40, 80, 200, 280]
    parser = _MarkerParser()
    parser.feed(build_html(report, {"overlay": PNG}))
    assert len(parser.markers) == 1
    values = dict(item.split(":") for item in parser.markers[0]["style"].rstrip(";").split(";"))
    assert {key: float(value.rstrip("%")) for key, value in values.items()} == {
        "left": 10.0, "top": 20.0, "width": 40.0, "height": 50.0,
    }


def test_demo_has_two_extractable_pages_with_known_first_line_shift():
    reference, candidate = make_demo_pair()
    with pymupdf.open(stream=reference, filetype="pdf") as first, pymupdf.open(stream=candidate, filetype="pdf") as second:
        assert len(first) == len(second) == 2
        assert first[0].get_text() == second[0].get_text()
        assert first[1].get_text() != second[1].get_text()
        line = "文档版本比较"
        a = first[0].search_for(line)[0]
        b = second[0].search_for(line)[0]
        assert abs((b.x0 - a.x0) * 25.4 / 72 - 1) < 0.001
        assert abs((b.y0 - a.y0) * 25.4 / 72 - 0.5) < 0.001


def _diagnostics():
    return {
        "schema_version": 1,
        "conventions": {"origin": "visible-page-top-left", "delta": "candidate-reference"},
        "tolerances": {"position_mm": 0.02},
        "summary": {"finding_count": 1, "by_type": {"text.position": 1}},
        "coverage": {"matched_lines": 1},
        "findings": [{
            "id": "finding-0001", "type": "text.position", "scope": "line",
            "reference": {"page": 1, "text": "定位锚点", "bbox_mm": [25, 30, 80, 35], "context_before": "前方内容", "context_after": "后方内容"},
            "candidate": {"page": 2, "text": "定位锚点", "bbox_mm": [26, 30, 81, 35]},
            "observation": "匹配文字向右偏移 1.000 mm。",
            "measurements": {"x_mm": {"reference": 25, "candidate": 26, "delta": 1, "unit": "mm"}},
            "target": {"description": "将当前文字向左移动 1.000 mm。", "candidate_adjustment": {"dx_mm": -1}, "requires_source_mapping": True},
            "reliability": {"level": "high", "reasons": ["文字完整匹配"], "evidence": {"matched_characters": 4}},
            "source_location": None,
        }],
        "limitations": ["源码位置需要结合工程定位。"],
        "truncated": False,
    }


def test_diagnostics_show_location_values_target_and_matching_evidence():
    report = _report()
    report["diagnostics"] = _diagnostics()
    html = build_html(report, {})
    assert "差异诊断" in html
    assert "文字位置变化" in html
    assert "匹配文字向右偏移 1.000 mm。" in html
    assert "将当前文字向左移动 1.000 mm。" in html
    assert "定位锚点" in html and "前方内容" in html and "后方内容" in html
    assert "(25.00, 30.00, 80.00, 35.00) mm" in html
    assert "25.000" in html and "26.000" in html and "1.000" in html
    assert "文字完整匹配" in html and "matched_characters" in html
    assert 'class="diagnostic-status">可靠' in html
    assert "诊断口径、阈值与覆盖信息" in html
    assert 'id="diagnostics-0001"' in html
    with ZipFile(BytesIO(build_bundle(report, {}))) as archive:
        assert json.loads(archive.read("report.json"))["diagnostics"] == report["diagnostics"]


def test_diagnostic_report_text_and_ids_cannot_inject_html_or_urls():
    report = _report()
    diagnostics = _diagnostics()
    finding = diagnostics["findings"][0]
    payload = '<img src=x onerror="alert(7)">'
    finding["id"] = '" onmouseover="alert(1)'
    finding["reference"]["text"] = payload
    finding["candidate"]["context_before"] = payload
    finding["observation"] = payload
    finding["measurements"]["x_mm"]["unit"] = payload
    finding["target"]["description"] = payload
    finding["reliability"] = {"level": "unexpected", "reasons": [payload], "evidence": {"source": payload}}
    diagnostics["limitations"] = [payload]
    report["diagnostics"] = diagnostics
    html = build_html(report, {})
    assert "<img src=x" not in html
    assert "&lt;img src=x" in html
    assert 'class="diagnostic-status review">需要复核' in html
    assert 'id="diagnostics-0001"' in html
    assert 'id="\" onmouseover=' not in html
    assert "<script" not in html


def test_visual_region_preview_uses_fixed_image_ids_and_clipped_physical_bounds():
    report = _report()
    report["canvas"] = {"width_px": 1000, "height_px": 2000}
    report["settings"]["dpi"] = 254
    report["diagnostics"] = _diagnostics()
    finding = report["diagnostics"]["findings"][0]
    finding.update(type="visual.region", id='"/><script>alert(8)</script>')
    finding["reference"]["bbox_mm"] = [10, 20, 20, 30]
    finding["candidate"]["bbox_mm"] = [11, 22, 21, 32]
    html = build_html(report, {key: PNG for key in ("reference", "candidate", "diff")})
    assert html.count('viewBox="80.000000 180.000000 150.000000 160.000000"') == 3
    assert 'href="#diagnostics-image-reference"' in html
    assert 'href="#diagnostics-image-candidate"' in html
    assert 'href="#diagnostics-image-diff"' in html
    assert '<image id="diagnostics-image-reference" width="1000" height="2000"' in html
    assert "<script" not in html


def test_diagnostics_distinguish_empty_and_truncated_results_and_limit_previews():
    report = _report()
    report["diagnostics"] = {"findings": [], "truncated": True}
    html = build_html(report, {})
    assert "当前阈值与测量覆盖内未列出诊断项" in html
    assert "诊断清单已达到容量限制" in html
    report["canvas"] = {"width_px": 1000, "height_px": 2000}
    finding = _diagnostics()["findings"][0]
    finding["type"] = "visual.region"
    report["diagnostics"]["findings"] = [finding] * 15
    html = build_html(report, {"diff": PNG})
    assert html.count('aria-label="差异图局部对照"') == 12
    assert html.count('<article class="diagnostic"') == 15


def test_content_insertion_keeps_empty_side_and_neighbor_anchor_explicit():
    report = _report()
    report["diagnostics"] = _diagnostics()
    finding = report["diagnostics"]["findings"][0]
    finding["type"] = "text.content"
    finding["reference"] = {"page": 1, "text": "", "bbox_mm": None, "anchor_bbox_mm": [20, 30, 22, 35]}
    html = build_html(report, {})
    assert "（空文本）" in html
    assert "相邻文字锚点 (20.00, 30.00, 22.00, 35.00) mm" in html
