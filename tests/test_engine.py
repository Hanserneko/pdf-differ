"""用已知位置的原生 PDF 验证测量，避免依赖本机字体和 OCR。"""

import unittest
from io import BytesIO

from PIL import Image
import numpy as np
import pymupdf

from pdfdiff.engine import MM_PER_PT, compare_pages, inspect_pdf


def make_pdf(lines=None, *, dx_mm=0, dy_mm=0, spacing_pt=18, width=300, height=400, rotation=0, cropbox=None, fontsize=12, fontname="helv"):
    if lines is None:
        lines = ["First sample line", "Second sample line", "Third sample line"]
    document = pymupdf.open()
    page = document.new_page(width=width, height=height)
    for i, text in enumerate(lines):
        page.insert_text((40 + dx_mm / MM_PER_PT, 60 + i * spacing_pt + dy_mm / MM_PER_PT), text, fontsize=fontsize, fontname=fontname)
    if cropbox:
        page.set_cropbox(pymupdf.Rect(cropbox))
    if rotation:
        page.set_rotation(rotation)
    result = document.tobytes()
    document.close()
    return result


class EngineTests(unittest.TestCase):
    def test_inspection_and_same_pdf(self):
        data = make_pdf()
        info = inspect_pdf(data)
        self.assertEqual(info["page_count"], 1)
        self.assertAlmostEqual(info["pages"][0]["width_mm"], 300 * MM_PER_PT)
        result = compare_pages(data, data, dpi=96)
        self.assertEqual(result.report["visual"]["foreground_mismatch_ratio"], 0)
        self.assertEqual(result.report["visual"]["edge_distance_p95_mm"], 0)
        self.assertAlmostEqual(result.report["text"]["median_dx_mm"], 0)
        self.assertAlmostEqual(result.report["text"]["median_dy_mm"], 0)
        self.assertEqual(result.report["text"]["matched_lines"], 3)
        self.assertEqual(set(result.images), {"reference", "candidate", "overlay", "diff"})

    def test_known_one_millimeter_translation(self):
        report = compare_pages(make_pdf(), make_pdf(dx_mm=1, dy_mm=1), dpi=96).report
        self.assertAlmostEqual(report["text"]["median_dx_mm"], 1, places=4)
        self.assertAlmostEqual(report["text"]["median_dy_mm"], 1, places=4)
        self.assertAlmostEqual(report["text"]["p95_position_error_mm"], 2 ** 0.5, places=4)
        self.assertGreater(report["visual"]["foreground_mismatch_ratio"], 0)
        self.assertGreater(report["visual"]["edge_distance_mean_mm"], 0)

    def test_line_spacing_change(self):
        report = compare_pages(make_pdf(), make_pdf(spacing_pt=20), dpi=96).report
        self.assertAlmostEqual(report["text"]["reference_line_spacing_mm"], 18 * MM_PER_PT, places=4)
        self.assertAlmostEqual(report["text"]["candidate_line_spacing_mm"], 20 * MM_PER_PT, places=4)
        self.assertAlmostEqual(report["text"]["line_spacing_delta_mm"], 2 * MM_PER_PT, places=4)

    def test_missing_character_is_not_ignored(self):
        report = compare_pages(make_pdf(["ABCDEFGHIJK"]), make_pdf(["ABCDEGHIJK"]), dpi=96).report
        self.assertEqual(report["text"]["unmatched_reference_characters"], 1)
        self.assertEqual(report["text"]["unmatched_candidate_characters"], 0)
        self.assertGreater(report["visual"]["foreground_mismatch_ratio"], 0)
        self.assertTrue(any("未匹配文字" in warning for warning in report["warnings"]))

    def test_line_break_change_is_reported_without_invented_spacing(self):
        reference = make_pdf(["ABCDEFGHIJKL"])
        candidate = make_pdf(["ABCDEF", "GHIJKL"])
        report = compare_pages(reference, candidate, dpi=96).report
        self.assertEqual(report["text"]["matched_characters"], 12)
        self.assertEqual(report["text"]["line_break_change_count"], 1)
        self.assertIsNone(report["text"]["reference_line_spacing_mm"])
        self.assertTrue(any("换行" in warning for warning in report["warnings"]))

    def test_font_size_and_font_name_changes(self):
        report = compare_pages(make_pdf(), make_pdf(fontsize=13, fontname="cour"), dpi=96).report
        self.assertAlmostEqual(report["text"]["font_size_delta_pt"], 1)
        self.assertGreater(report["text"]["changed_font_name_characters"], 0)

    def test_chinese_characters_have_measurable_origins(self):
        reference = make_pdf(["文档版本比较", "中文行距比较", "精确量化差异"], fontname="china-s")
        candidate = make_pdf(["文档版本比较", "中文行距比较", "精确量化差异"], fontname="china-s", dx_mm=1)
        report = compare_pages(reference, candidate, dpi=96).report
        self.assertEqual(report["text"]["matched_characters"], 18)
        self.assertAlmostEqual(report["text"]["median_dx_mm"], 1, places=4)

    def test_color_only_changes_survive_in_preview_and_heatmap(self):
        def colored_rectangle(color):
            document = pymupdf.open()
            page = document.new_page(width=100, height=100)
            page.draw_rect(pymupdf.Rect(20, 20, 80, 80), color=None, fill=color)
            result = document.tobytes()
            document.close()
            return result

        # 红色与该绿色具有相同的 8 位灰度值，墨迹指标无法反映颜色差。
        result = compare_pages(colored_rectangle((1, 0, 0)), colored_rectangle((0, 130 / 255, 0)), dpi=72)
        self.assertEqual(result.report["visual"]["foreground_mismatch_ratio"], 0)
        self.assertGreater(result.report["visual"]["rgb_mean_absolute_error"], 0)
        self.assertGreater(result.report["visual"]["rgb_changed_pixels"], 0)
        with Image.open(BytesIO(result.images["reference"])) as picture:
            self.assertEqual(picture.mode, "RGB")
            self.assertEqual(picture.getpixel((50, 50)), (255, 0, 0))
        with Image.open(BytesIO(result.images["candidate"])) as picture:
            self.assertGreater(picture.getpixel((50, 50))[1], 100)
        with Image.open(BytesIO(result.images["diff"])) as picture:
            red, green, blue = picture.getpixel((50, 50))
            self.assertGreater(red, green)
            self.assertEqual(green, blue)

    def test_overlay_color_convention(self):
        reference = make_pdf(["Overlay"], dx_mm=0)
        candidate = make_pdf(["Overlay"], dx_mm=20)
        result = compare_pages(reference, candidate, dpi=150)
        with Image.open(BytesIO(result.images["overlay"])) as picture:
            pixels = np.asarray(picture)
            self.assertTrue(np.any(np.all(pixels == (255, 0, 0), axis=2)))
            self.assertTrue(np.any(np.all(pixels == (0, 0, 255), axis=2)))

    def test_blank_regions_have_no_fake_measurement(self):
        data = make_pdf([])
        report = compare_pages(data, data, dpi=96).report
        self.assertIsNone(report["visual"]["foreground_mismatch_ratio"])
        self.assertIsNone(report["visual"]["edge_distance_mean_mm"])
        self.assertIsNone(report["visual"]["reference_foreground_bbox_mm"])
        self.assertIsNone(report["text"]["median_dx_mm"])
        self.assertIsNone(report["text"]["reference_line_spacing_mm"])
        self.assertTrue(report["warnings"])

    def test_one_blank_side_detects_missing_foreground(self):
        report = compare_pages(make_pdf(), make_pdf([]), dpi=96).report
        self.assertEqual(report["visual"]["foreground_mismatch_ratio"], 1)
        self.assertIsNone(report["visual"]["edge_distance_p95_mm"])

    def test_selected_region_does_not_crop_output_images(self):
        reference = make_pdf(["Stable line", "Changing line"])
        candidate = make_pdf(["Stable line", "Different words"])
        region = (10, 15, 90, 23)
        result = compare_pages(reference, candidate, dpi=96, region_mm=region)
        self.assertEqual(result.report["visual"]["foreground_mismatch_ratio"], 0)
        self.assertEqual(result.report["text"]["reference_characters"], len("Stableline"))
        with Image.open(BytesIO(result.images["overlay"])) as picture:
            self.assertGreater(picture.height, 400)
        full_report = compare_pages(reference, candidate, dpi=96).report
        self.assertGreater(full_report["visual"]["foreground_mismatch_ratio"], 0)

    def test_page_size_difference_is_not_scaled_away(self):
        result = compare_pages(make_pdf(width=300), make_pdf(width=320), dpi=96)
        self.assertNotEqual(result.report["reference"]["width_mm"], result.report["candidate"]["width_mm"])
        self.assertAlmostEqual(result.report["text"]["median_dx_mm"], 0)
        self.assertTrue(any("尺寸不同" in message for message in result.report["warnings"]))
        with Image.open(BytesIO(result.images["reference"])) as a, Image.open(BytesIO(result.images["candidate"])) as b:
            self.assertEqual(a.size, b.size)

    def test_rotation_is_shared_between_text_and_rendering(self):
        reference = make_pdf(rotation=90)
        candidate = make_pdf(dx_mm=1, rotation=90)
        report = compare_pages(reference, candidate, dpi=96).report
        self.assertAlmostEqual(report["text"]["median_dx_mm"], 0, places=4)
        self.assertAlmostEqual(report["text"]["median_dy_mm"], 1, places=4)
        self.assertIsNone(report["text"]["reference_line_spacing_mm"])
        self.assertAlmostEqual(report["reference"]["width_mm"], 400 * MM_PER_PT)

    def test_cropbox_is_disclosed(self):
        data = make_pdf(cropbox=(10, 10, 290, 390))
        report = compare_pages(data, data, dpi=96).report
        self.assertEqual(report["reference"]["page_box"], "cropbox")
        self.assertAlmostEqual(report["reference"]["width_mm"], 280 * MM_PER_PT)
        self.assertTrue(any("CropBox" in message for message in report["warnings"]))

    def test_cropbox_origin_is_applied_to_character_positions(self):
        reference = make_pdf(cropbox=(10, 10, 290, 390))
        candidate = make_pdf(cropbox=(20, 20, 290, 390))
        report = compare_pages(reference, candidate, dpi=96).report
        self.assertAlmostEqual(report["text"]["median_dx_mm"], -10 * MM_PER_PT, places=4)
        self.assertAlmostEqual(report["text"]["median_dy_mm"], -10 * MM_PER_PT, places=4)

    def test_unrelated_text_has_no_position_claim(self):
        report = compare_pages(make_pdf(["AAAAAAAA"]), make_pdf(["ZZZZZZZZ"]), dpi=96).report
        self.assertEqual(report["text"]["matched_characters"], 0)
        self.assertIsNone(report["text"]["p95_position_error_mm"])

    def test_high_dpi_keeps_small_page_native_resolution(self):
        data = make_pdf(["Native 600 DPI"], width=150, height=150)
        result = compare_pages(data, data, dpi=600)
        self.assertEqual(result.report["canvas"]["width_px"], 1250)
        self.assertEqual(result.report["visual"]["foreground_mismatch_ratio"], 0)

    def test_invalid_inputs_and_render_limit(self):
        data = make_pdf()
        with self.assertRaises(ValueError):
            inspect_pdf(b"not a pdf")
        for options in ({"reference_page": 0}, {"candidate_page": 2}, {"dpi": 601}, {"ink_threshold": 255}, {"region_mm": (1, 1, 0, 2)}, {"region_mm": (0, 0, 1000, 1000)}, {"region_mm": (0, 0, float("nan"), 1)}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                compare_pages(data, data, **options)
        huge = make_pdf([], width=3000, height=3000)
        with self.assertRaisesRegex(ValueError, "过大"):
            compare_pages(huge, huge, dpi=300)


if __name__ == "__main__":
    unittest.main()
