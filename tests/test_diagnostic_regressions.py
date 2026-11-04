"""通过已知几何与内容的 PDF，独立检查诊断能否安全用于后续调整。"""

from io import BytesIO

from PIL import Image, ImageDraw
import pymupdf
import pytest

from pdfdiff.engine import MM_PER_PT, compare_pages


def text_pdf(lines, *, dx_mm=0, rotation=0, cropbox=None, width=320, height=620):
    with pymupdf.open() as document:
        page = document.new_page(width=width, height=height)
        for index, text in enumerate(lines):
            page.insert_text((40 + dx_mm / MM_PER_PT, 60 + index * 18), text, fontsize=12)
        if cropbox is not None:
            page.set_cropbox(pymupdf.Rect(cropbox))
        if rotation:
            page.set_rotation(rotation)
        return document.tobytes()


def character_pdf(*, sizes=None, fonts=None, dx_mm=None):
    sizes = sizes or [12] * 6
    fonts = fonts or ["helv"] * 6
    dx_mm = dx_mm or [0] * 6
    with pymupdf.open() as document:
        page = document.new_page(width=240, height=140)
        for index, char in enumerate("ABCDEF"):
            page.insert_text((40 + index * 10 + dx_mm[index] / MM_PER_PT, 60), char,
                             fontsize=sizes[index], fontname=fonts[index])
        return document.tobytes()


def compare(reference, candidate, **settings):
    return compare_pages(reference, candidate, dpi=96, **settings).report


def findings(report, kind):
    return [item for item in report["diagnostics"]["findings"] if item["type"] == kind]


def assert_reviewed_movement(records):
    for item in records:
        assert item["reliability"]["level"] == "review"
        assert "candidate_adjustment" not in item["target"]
        assert item["source_location"] is None


def test_deleted_duplicate_line_does_not_become_a_confident_translation():
    with pymupdf.open() as document:
        page = document.new_page(width=250, height=200)
        for y in (60, 80, 100):
            page.insert_text((40, y), "Repeated label", fontsize=12)
        reference = document.tobytes()
    with pymupdf.open() as document:
        page = document.new_page(width=250, height=200)
        for y in (60, 100):
            page.insert_text((40, y), "Repeated label", fontsize=12)
        candidate = document.tobytes()
    report = compare(reference, candidate)
    assert report["text"]["median_dy_mm"] > 3
    movement = findings(report, "text.position")
    assert movement
    assert_reviewed_movement(movement)
    assert not findings(report, "text.line_pattern")


def test_repeated_content_context_is_not_high_reliability():
    lines = ["Repeated phrase value"] * 20
    changed = list(lines)
    changed[10] = "Repeated phrase VALUE"
    report = compare(text_pdf(lines), text_pdf(changed))
    assert report["text"]["match_ratio"] > 0.9
    content = findings(report, "text.content")
    assert content
    assert all(item["reliability"]["level"] == "review" for item in content)


def test_overlapping_repeated_anchors_are_not_counted_as_unique():
    reference = text_pdf(["A" * 70 + "B"], width=800)
    candidate = text_pdf(["A" * 35 + "C" + "A" * 34 + "B"], width=800)
    report = compare(reference, candidate)
    content = findings(report, "text.content")
    assert content
    assert all(item["reliability"]["level"] == "review" for item in content)
    assert any(item["reliability"]["evidence"]["reference_anchor_occurrences"] > 1 for item in content)


def test_small_reordering_on_an_otherwise_matching_page_needs_review():
    prefix = [f"stable entry {index:02d}" for index in range(7)]
    suffix = [f"following entry {index:02d}" for index in range(7)]
    reference = prefix + ["ABCDEFGH", "QRSTUVWX"] + suffix
    candidate = prefix + ["QRSTUVWX", "ABCDEFGH"] + suffix
    report = compare(text_pdf(reference), text_pdf(candidate))
    assert report["text"]["match_ratio"] > 0.9
    assert findings(report, "text.content")
    assert_reviewed_movement(findings(report, "text.position"))


def test_opposite_character_displacements_survive_a_zero_median():
    report = compare(character_pdf(), character_pdf(dx_mm=[-1, -1, -1, 1, 1, 1]))
    assert report["text"]["median_dx_mm"] == pytest.approx(0, abs=0.001)
    assert report["text"]["p95_position_error_mm"] == pytest.approx(1, abs=0.001)
    movement = findings(report, "text.position")
    assert movement
    assert any(item["reliability"]["evidence"]["displacement_residual_p95_mm"] > 0.9 for item in movement)
    assert_reviewed_movement(movement)


def test_opposite_font_size_changes_survive_a_zero_median():
    report = compare(character_pdf(), character_pdf(sizes=[10, 10, 10, 14, 14, 14]))
    assert report["text"]["font_size_delta_pt"] == pytest.approx(0)
    sizes = findings(report, "text.font_size")
    assert sizes
    deltas = [item["measurements"]["font_size_pt"]["delta"] for item in sizes]
    assert min(deltas) == pytest.approx(-2)
    assert max(deltas) == pytest.approx(2)
    assert all(item["reference"]["text"] and item["candidate"]["text"] for item in sizes)


def test_font_swaps_are_located_even_when_line_font_sets_match():
    reference = character_pdf(fonts=["helv"] * 3 + ["cour"] * 3)
    candidate = character_pdf(fonts=["cour"] * 3 + ["helv"] * 3)
    report = compare(reference, candidate)
    font_changes = findings(report, "text.font_name")
    assert font_changes
    assert all(item["measurements"]["font_name"]["reference"] != item["measurements"]["font_name"]["candidate"] for item in font_changes)


def test_removed_spaces_have_content_evidence_and_no_uniform_move_target():
    report = compare(text_pdf(["A B C D"]), text_pdf(["ABCD"]))
    content = findings(report, "text.content")
    assert content
    assert any(" " in item["reference"]["text"] for item in content)
    assert all(item["reliability"]["evidence"]["whitespace_preserved"] for item in content)
    assert_reviewed_movement(findings(report, "text.position"))


def test_unbalanced_reflow_is_not_a_high_reliability_line_move():
    report = compare(text_pdf(["ABCDEFGHIJKL"]), text_pdf(["ABCDEFGH", "IJKL"], dx_mm=1))
    assert findings(report, "text.reflow")
    assert_reviewed_movement(findings(report, "text.position"))
    assert not findings(report, "text.line_pattern")
    assert not findings(report, "text.line_spacing")


def raster_pdf(changed=False):
    image = Image.new("RGB", (80, 80), "white")
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((15, 15, 65, 25), fill="black")
    if changed:
        drawing.rectangle((15, 45, 65, 55), fill="black")
    payload = BytesIO()
    image.save(payload, format="PNG")
    with pymupdf.open() as document:
        page = document.new_page(width=200, height=200)
        page.insert_image(page.rect, stream=payload.getvalue())
        return document.tobytes()


@pytest.mark.parametrize("blank", [True, False])
def test_missing_text_layer_keeps_text_measurement_unavailable(blank):
    reference = text_pdf([]) if blank else raster_pdf()
    candidate = reference if blank else raster_pdf(changed=True)
    report = compare(reference, candidate)
    assert report["text"]["median_dx_mm"] is None
    assert report["diagnostics"]["coverage"]["position_measurement_available"] is False
    assert not any(item["type"].startswith("text.") for item in report["diagnostics"]["findings"])
    if not blank:
        assert findings(report, "visual.region")


def test_explicitly_invisible_ocr_layer_has_no_confident_visual_move_target():
    def hidden_layer(dx_mm):
        with pymupdf.open() as document:
            page = document.new_page(width=250, height=160)
            page.insert_text((40 + dx_mm / MM_PER_PT, 60), "Hidden OCR text", fontsize=12, render_mode=3)
            return document.tobytes()

    report = compare(hidden_layer(0), hidden_layer(1))
    assert report["visual"]["rgb_changed_pixels"] == 0
    assert_reviewed_movement(findings(report, "text.position"))
    assert not findings(report, "text.line_pattern")


def rectangles_pdf(*, change_inside=False, change_outside=False):
    with pymupdf.open() as document:
        page = document.new_page(width=200, height=200)
        for box, changed in (((20, 20, 60, 60), change_inside), ((120, 20, 160, 60), change_outside)):
            color = (0, 130 / 255, 0) if changed else (1, 0, 0)
            page.draw_rect(pymupdf.Rect(box), color=None, fill=color)
        return document.tobytes()


def test_rgb_regions_cover_changed_pixels_and_keep_global_roi_coordinates():
    roi = (5, 5, 30, 30)
    report = compare(rectangles_pdf(), rectangles_pdf(change_inside=True, change_outside=True), region_mm=roi)
    assert report["visual"]["foreground_mismatch_ratio"] == 0
    assert report["visual"]["rgb_changed_pixels"] > 0
    regions = findings(report, "visual.region")
    assert regions
    assert sum(item["reliability"]["evidence"]["changed_pixels"] for item in regions) == report["visual"]["rgb_changed_pixels"]
    assert report["diagnostics"]["coverage"]["omitted_visual_changed_pixels"] == 0
    for item in regions:
        x0, y0, x1, y1 = item["reference"]["bbox_mm"]
        assert roi[0] <= x0 < x1 <= roi[2]
        assert roi[1] <= y0 < y1 <= roi[3]
        assert 6 <= x0 <= 8
        assert 6 <= y0 <= 8


def test_changes_outside_the_roi_have_no_local_visual_finding():
    report = compare(rectangles_pdf(), rectangles_pdf(change_outside=True), region_mm=(5, 5, 30, 30))
    assert report["visual"]["rgb_changed_pixels"] == 0
    assert not findings(report, "visual.region")


@pytest.mark.parametrize("kind", ["size", "rotation", "cropbox"])
def test_blank_page_geometry_is_reported_without_text_inferences(kind):
    reference_options = {"width": 200, "height": 200}
    candidate_options = dict(reference_options)
    if kind == "size":
        candidate_options["width"] = 210
    elif kind == "rotation":
        candidate_options["rotation"] = 90
    else:
        reference_options = {"width": 220, "height": 220, "cropbox": (0, 0, 200, 200)}
        candidate_options = {"width": 220, "height": 220, "cropbox": (10, 0, 210, 200)}
    report = compare(text_pdf([], **reference_options), text_pdf([], **candidate_options))
    assert report["visual"]["rgb_changed_pixels"] == 0
    geometry = findings(report, "page.geometry")
    assert geometry
    assert all(item["reliability"]["level"] == "high" for item in geometry)
    assert not any(item["type"].startswith("text.") for item in report["diagnostics"]["findings"])


@pytest.mark.parametrize("rotation,expected,roi", [
    (0, (1, 0), (5, 5, 40, 25)),
    (90, (0, 1), (75, 5, 90, 40)),
    (180, (-1, 0), (60, 75, 95, 90)),
    (270, (0, -1), (5, 60, 25, 95)),
])
def test_rotation_crop_and_roi_share_the_same_display_coordinates(rotation, expected, roi):
    options = {"width": 300, "height": 300, "cropbox": (10, 10, 290, 290), "rotation": rotation}
    reference = text_pdf(["Visible line"], **options)
    candidate = text_pdf(["Visible line"], dx_mm=1, **options)
    report = compare(reference, candidate, region_mm=roi)
    movement = findings(report, "text.position")
    assert len(movement) == 1
    item = movement[0]
    assert item["measurements"]["dx_mm"]["candidate"] == pytest.approx(expected[0], abs=0.001)
    assert item["measurements"]["dy_mm"]["candidate"] == pytest.approx(expected[1], abs=0.001)
    x0, y0, x1, y1 = item["reference"]["bbox_mm"]
    assert roi[0] < x0 < x1 < roi[2]
    assert roi[1] < y0 < y1 < roi[3]
    assert findings(report, "visual.region")
    if rotation in (90, 270):
        assert_reviewed_movement(movement)


def test_roi_boundary_character_loss_is_reviewed_as_a_selection_effect():
    text = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    reference = text_pdf([text])
    candidate = text_pdf([text], dx_mm=1)
    with pymupdf.open(stream=reference, filetype="pdf") as document:
        raw = document[0].get_text("rawdict")
        chars = [char for block in raw["blocks"] if block["type"] == 0 for line in block["lines"] for span in line["spans"] for char in span["chars"]]
        last = chars[-1]["bbox"]
        right = (last[0] + last[2]) * MM_PER_PT / 2 + 0.5
    report = compare(reference, candidate, region_mm=(0, 0, right, 40))
    assert report["text"]["match_ratio"] > 0.9
    content = findings(report, "text.content")
    assert content
    assert all(item["reliability"]["level"] == "review" for item in content)
