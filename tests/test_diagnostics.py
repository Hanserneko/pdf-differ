"""用已知几何和文字变化验证可定位的诊断证据。"""

import json

import pymupdf
import pytest

from pdfdiff import diagnostics
from pdfdiff.demo import make_demo_pair
from pdfdiff.engine import MM_PER_PT, compare_pages


def make_pdf(lines=None, *, dx=0, dy=0, gap=18, font_size=12, rotation=0, cropbox=None):
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=300)
        for index, text in enumerate(lines or []):
            page.insert_text((40 + dx / MM_PER_PT, 50 + dy / MM_PER_PT + index * gap), text, fontsize=font_size)
        if cropbox:
            page.set_cropbox(pymupdf.Rect(cropbox))
        if rotation:
            page.set_rotation(rotation)
        return document.tobytes()


def diagnose(a, b, **settings):
    return compare_pages(a, b, dpi=96, **settings).report["diagnostics"]


def findings(report, kind):
    return [finding for finding in report["findings"] if finding["type"] == kind]


def test_equal_documents_have_no_findings_and_stable_strict_json():
    data = make_pdf(["First line", "Second line", "Third line"])
    report = diagnose(data, data)
    assert report["findings"] == []
    assert report["summary"]["finding_count"] == 0
    assert report["coverage"]["content_comparison"] == "completed"
    assert not report["truncated"]
    assert json.loads(json.dumps(report, allow_nan=False)) == diagnose(data, data)


def test_known_uniform_translation_has_inverse_geometric_target_and_pattern():
    lines = ["First unique line", "Second unique line", "Third unique line"]
    report = diagnose(make_pdf(lines), make_pdf(lines, dx=1, dy=0.5))
    positions = findings(report, "text.position")
    assert len(positions) == 3
    for finding in positions:
        assert finding["reliability"]["level"] == "high"
        assert finding["target"]["candidate_adjustment"]["dx_mm"] == pytest.approx(-1, abs=1e-4)
        assert finding["target"]["candidate_adjustment"]["dy_mm"] == pytest.approx(-0.5, abs=1e-4)
        assert finding["source_location"] is None
        assert finding["reference"]["bbox_mm"]
        assert finding["candidate"]["page"] == 1
    patterns = findings(report, "text.line_pattern")
    assert len(patterns) == 1
    assert patterns[0]["reliability"]["evidence"]["sample_lines"] == 3
    assert set(patterns[0]["related_findings"]) == {finding["id"] for finding in positions}


def test_cumulative_line_spacing_is_measured_at_each_gap_and_in_fitted_pattern():
    lines = ["Unique Alpha", "Unique Beta", "Unique Gamma", "Unique Delta"]
    report = diagnose(make_pdf(lines), make_pdf(lines, dy=0.5, gap=19))
    patterns = findings(report, "text.line_pattern")
    assert len(patterns) == 1
    assert patterns[0]["measurements"]["initial_dy_mm"]["candidate"] == pytest.approx(0.5, abs=1e-4)
    assert patterns[0]["measurements"]["per_line_dy_mm"]["candidate"] == pytest.approx(MM_PER_PT, abs=1e-4)
    spacing = findings(report, "text.line_spacing")
    assert len(spacing) == 3
    assert all(item["measurements"]["baseline_spacing_mm"]["delta"] == pytest.approx(MM_PER_PT, abs=1e-4) for item in spacing)


def test_two_lines_report_single_gap_without_fitting_a_pattern():
    report = diagnose(make_pdf(["North", "South"]), make_pdf(["North", "South"], gap=20))
    assert len(findings(report, "text.line_spacing")) == 1
    assert findings(report, "text.line_pattern") == []


def test_demo_body_pattern_survives_unrelated_same_height_table_fragments():
    a, b = make_demo_pair()
    report = diagnose(a, b)
    patterns = findings(report, "text.line_pattern")
    assert patterns
    assert any(item["measurements"]["per_line_dy_mm"]["candidate"] == pytest.approx(0.12, abs=0.001) for item in patterns)
    assert findings(report, "text.line_spacing")
    assert any(item["reliability"]["level"] == "review" for item in findings(report, "text.position"))


@pytest.mark.parametrize("reference,candidate,operation,old,new", [
    ("Alpha beta text", "Alpha text", "delete", "beta ", ""),
    ("Alpha text", "Alpha new text", "insert", "", "new "),
    ("Item ABCD end", "Item ABXD end", "replace", "C", "X"),
    ("A B C D", "ABCD", "delete", " ", ""),
])
def test_content_changes_preserve_exact_fragments_and_context(reference, candidate, operation, old, new):
    report = diagnose(make_pdf([reference]), make_pdf([candidate]))
    matches = findings(report, "text.content")
    assert any(item["reference"]["text"] == old and item["candidate"]["text"] == new and
               item["reliability"]["evidence"]["operation"] == operation for item in matches)
    assert all("context_before" in item["reference"] and "context_after" in item["candidate"] for item in matches)
    assert all("candidate_adjustment" not in item["target"] for item in findings(report, "text.position"))


def test_reflow_identifies_the_exact_character_boundary_and_both_line_numbers():
    report = diagnose(make_pdf(["ABCDEFGHIJKL"]), make_pdf(["ABCDEF", "GHIJKL"]))
    item, = findings(report, "text.reflow")
    assert "'F'" in item["observation"] and "'G'" in item["observation"]
    assert item["measurements"]["line_break"]["reference"] == 0
    assert item["measurements"]["line_break"]["candidate"] == 1
    assert item["reliability"]["evidence"]["candidate_line_after"] == 2
    assert item["reliability"]["level"] == "review"
    assert not findings(report, "text.line_pattern")


def test_rotation_and_cropping_use_display_coordinates_for_text_and_crops():
    a = make_pdf(["Visible line"], rotation=90, cropbox=(10, 10, 290, 290))
    b = make_pdf(["Visible line"], dx=1, rotation=90, cropbox=(10, 10, 290, 290))
    report = diagnose(a, b)
    item, = findings(report, "text.position")
    assert item["measurements"]["dx_mm"]["candidate"] == pytest.approx(0, abs=1e-4)
    assert item["measurements"]["dy_mm"]["candidate"] == pytest.approx(1, abs=1e-4)
    assert item["reliability"]["level"] == "review"
    assert "candidate_adjustment" not in item["target"]
    assert item["reference"]["bbox_mm"]


def test_visual_regions_keep_all_pixels_accounted_and_limit_is_explicit(monkeypatch):
    def rectangles(color):
        with pymupdf.open() as document:
            page = document.new_page(width=200, height=200)
            for x, y in ((20, 20), (100, 20), (20, 100), (100, 100)):
                page.draw_rect(pymupdf.Rect(x, y, x + 10, y + 10), color=None, fill=color)
            return document.tobytes()
    monkeypatch.setattr(diagnostics, "MAX_VISUAL_REGIONS", 2)
    result = compare_pages(rectangles((1, 0, 0)), rectangles((0, 1, 0)), dpi=96)
    report = result.report["diagnostics"]
    regions = findings(report, "visual.region")
    assert len(regions) == 2
    assert report["truncated"]
    assert report["coverage"]["visual_regions_total"] == 4
    pixels = sum(item["reliability"]["evidence"]["changed_pixels"] for item in regions)
    assert pixels + report["coverage"]["omitted_visual_changed_pixels"] == result.report["visual"]["rgb_changed_pixels"]
    assert report["summary"]["detected_finding_count"] == 4
    assert all(len(item["reliability"]["evidence"]["crop_px"]) == 4 for item in regions)


def test_finding_limit_and_content_character_limit_are_disclosed(monkeypatch):
    lines = ["First unique line", "Second unique line", "Third unique line"]
    monkeypatch.setattr(diagnostics, "MAX_FINDINGS", 2)
    report = diagnose(make_pdf(lines), make_pdf(lines, dx=1))
    assert report["truncated"] and len(report["findings"]) == 2
    assert report["summary"]["detected_finding_count"] > 2
    assert report["coverage"]["omitted_visual_changed_pixels"] > 0
    monkeypatch.setattr(diagnostics, "MAX_CONTENT_CHARACTERS", 3)
    report = diagnose(make_pdf(["Reference text"]), make_pdf(["Candidate text"]))
    assert report["coverage"]["content_comparison"] == "character_limit"
    assert not findings(report, "text.content")
    assert any("3" in reason and "跳过" in reason for reason in report["limitations"])


def test_textless_pages_explicitly_disclose_unavailable_content_measurement():
    report = diagnose(make_pdf(), make_pdf())
    assert report["coverage"]["content_comparison"] == "no_extractable_text"
    assert not report["coverage"]["position_measurement_available"]
    assert report["findings"] == []
