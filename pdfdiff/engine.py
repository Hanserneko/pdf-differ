"""在可见页绝对坐标中比较 PDF，不进行配准、裁白或缩放补偿。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from io import BytesIO
import math
from typing import Any

import cv2
import numpy as np
from PIL import Image
import pymupdf

from .diagnostics import build_diagnostics


MM_PER_PT = 25.4 / 72
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_PAGES = 2000
MAX_RENDER_PIXELS = 40_000_000
MAX_RENDER_SIDE = 10000
MAX_TEXT_CHARACTERS = 8000


@dataclass
class ComparisonResult:
    report: dict[str, Any]
    images: dict[str, bytes]


def _open_pdf(data: bytes) -> pymupdf.Document:
    if not isinstance(data, bytes) or not data:
        raise ValueError("PDF 内容不能为空。")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("单个 PDF 不能超过 50 MiB。")
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError("无法读取 PDF，请检查文件格式或文件是否损坏。") from exc
    if document.needs_pass:
        document.close()
        raise ValueError("暂不支持需要密码的 PDF，请先提供可直接打开的副本。")
    if document.page_count < 1 or document.page_count > MAX_PAGES:
        document.close()
        raise ValueError(f"PDF 页数必须为 1 至 {MAX_PAGES}。")
    return document


def _page_info(page: pymupdf.Page) -> dict[str, Any]:
    return {
        "width_mm": page.rect.width * MM_PER_PT,
        "height_mm": page.rect.height * MM_PER_PT,
        "rotation": page.rotation,
        "page_box": "cropbox",
        "cropbox_pt": list(page.cropbox),
        "mediabox_pt": list(page.mediabox),
    }


def _page_warnings(page: pymupdf.Page) -> list[str]:
    warnings = []
    if any(abs(a - b) > 0.01 for a, b in zip(page.cropbox, page.mediabox)):
        warnings.append("页面存在 CropBox 裁切：以可见页左上角为原点，不能将该原点解释为原始纸张边缘。")
    if page.rotation:
        warnings.append(f"页面旋转 {page.rotation}°：图像和文字位置均已转换到显示后的可见页坐标。")
    return warnings


def inspect_pdf(data: bytes) -> dict[str, Any]:
    """只提取页数和页面几何信息，不进行渲染或 OCR。"""
    with _open_pdf(data) as document:
        warnings = []
        if document.is_repaired:
            warnings.append("解析器修复过 PDF；建议检查源文件和页面显示。")
        pages = []
        for page in document:
            pages.append(_page_info(page))
            warnings.extend(_page_warnings(page))
        return {
            "page_count": len(pages),
            "pages": pages,
            "warnings": list(dict.fromkeys(warnings)),
        }


def _render(page: pymupdf.Page, dpi: int, shape: tuple[int, int]) -> np.ndarray:
    pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False, annots=True)
    actual = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, 3)
    canvas = np.full((*shape, 3), 255, dtype=np.uint8)
    if actual.shape[0] > shape[0] or actual.shape[1] > shape[1]:
        raise ValueError("页面渲染尺寸超出预期，无法安全比较该 PDF。")
    canvas[: actual.shape[0], : actual.shape[1]] = actual
    return canvas


def _encode_png(pixels: np.ndarray) -> bytes:
    stream = BytesIO()
    Image.fromarray(pixels).save(stream, format="PNG")
    return stream.getvalue()


def _visual_metrics(reference: np.ndarray, candidate: np.ndarray, dpi: int, threshold: int, origin_px: tuple[int, int]) -> dict[str, Any]:
    mask_a = reference < threshold
    mask_b = candidate < threshold
    union_count = int(np.count_nonzero(mask_a | mask_b))
    result = {
        "foreground_mismatch_ratio": (
            float(np.count_nonzero(mask_a ^ mask_b) / union_count) if union_count else None
        ),
        "foreground_union_pixels": union_count,
        "reference_foreground_pixels": int(np.count_nonzero(mask_a)),
        "candidate_foreground_pixels": int(np.count_nonzero(mask_b)),
        "ssim": None,
        "edge_distance_mean_mm": None,
        "edge_distance_p95_mm": None,
        "reference_foreground_bbox_mm": _ink_bbox(mask_a, dpi, origin_px),
        "candidate_foreground_bbox_mm": _ink_bbox(mask_b, dpi, origin_px),
    }
    if not np.any(mask_a) or not np.any(mask_b):
        return result
    # 只测前景轮廓，避免整页白色像素稀释指标；双方都参与距离统计。
    kernel = np.ones((3, 3), dtype=np.uint8)
    edge_a = mask_a & (cv2.erode(mask_a.astype(np.uint8), kernel, borderType=cv2.BORDER_CONSTANT, borderValue=0) == 0)
    edge_b = mask_b & (cv2.erode(mask_b.astype(np.uint8), kernel, borderType=cv2.BORDER_CONSTANT, borderValue=0) == 0)
    distances = []
    for source, target in ((edge_a, edge_b), (edge_b, edge_a)):
        distance_map = cv2.distanceTransform((~target).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
        distances.append(distance_map[source])
        del distance_map
    values = np.concatenate(distances)
    result["edge_distance_mean_mm"] = float(np.mean(values)) * 25.4 / dpi
    result["edge_distance_p95_mm"] = float(np.percentile(values, 95)) * 25.4 / dpi
    return result


def _ink_bbox(mask: np.ndarray, dpi: int, origin_px: tuple[int, int]) -> list[float] | None:
    rows = np.flatnonzero(np.any(mask, axis=1))
    columns = np.flatnonzero(np.any(mask, axis=0))
    if not len(rows) or not len(columns):
        return None
    return [
        (int(columns[0]) + origin_px[0]) * 25.4 / dpi,
        (int(rows[0]) + origin_px[1]) * 25.4 / dpi,
        (int(columns[-1]) + 1 + origin_px[0]) * 25.4 / dpi,
        (int(rows[-1]) + 1 + origin_px[1]) * 25.4 / dpi,
    ]


def _extract_text(page: pymupdf.Page, region: tuple[float, float, float, float] | None) -> tuple[list[dict], list[dict], list[str]]:
    warnings = []
    raw = page.get_text("rawdict", sort=True)
    rotation = page.rotation_matrix
    lines = []
    has_non_horizontal = False
    for block in raw["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            direction = line.get("dir", (1.0, 0.0))
            direction_y = direction[0] * rotation.b + direction[1] * rotation.d
            horizontal = abs(direction_y) < 0.01
            chars = []
            raw_chars = []
            for span in line["spans"]:
                for char in span.get("chars", []):
                    value = char.get("c", "")
                    if not value:
                        continue
                    point = pymupdf.Point(char["origin"]) * rotation
                    bbox = pymupdf.Rect(char["bbox"]) * rotation
                    center_x = (bbox.x0 + bbox.x1) * MM_PER_PT / 2
                    center_y = (bbox.y0 + bbox.y1) * MM_PER_PT / 2
                    if region and not (region[0] <= center_x < region[2] and region[1] <= center_y < region[3]):
                        continue
                    item = {
                        "c": value,
                        "x_mm": point.x * MM_PER_PT,
                        "y_mm": point.y * MM_PER_PT,
                        "size_pt": float(span["size"]),
                        "font": span.get("font", ""),
                        "visible": span.get("alpha", 255) > 0 and bool(span.get("char_flags", 16) & 48),
                        "bbox_mm": [v * MM_PER_PT for v in bbox],
                    }
                    raw_chars.extend({**item, "c": codepoint} for codepoint in value)
                    if not value.isspace():
                        chars.append(item)
            if chars:
                has_non_horizontal |= not horizontal
                lines.append({
                    "text": "".join(char["c"] for char in chars),
                    "chars": chars,
                    "raw_chars": raw_chars,
                    "horizontal": horizontal,
                    "x_mm": float(np.median([char["x_mm"] for char in chars])),
                    "y_mm": float(np.median([char["y_mm"] for char in chars])),
                    "size_pt": float(np.median([char["size_pt"] for char in chars])),
                })
    # 统一按显示后位置排序；复杂多栏、交叠文字仍不能可靠推断阅读顺序。
    lines.sort(key=lambda line: (round(line["y_mm"], 3), line["x_mm"]))
    characters = []
    for index, line in enumerate(lines):
        line["id"] = index
        for char in line["raw_chars"]:
            char["line_id"] = index
        for char in line["chars"]:
            char["line_id"] = index
            characters.append(char)
    if has_non_horizontal:
        warnings.append("区域包含非水平文字：仍比较字符位置，但这些行不参与水平行距统计。")
    if any(char["c"] == "\ufffd" for char in characters):
        warnings.append("部分文字无法映射到 Unicode，文字测量可能不可靠；请结合图像结果。")
    return characters, lines, warnings


def _text_metrics(reference_page: pymupdf.Page, candidate_page: pymupdf.Page, region: tuple | None) -> tuple[dict, list[dict], list[str], dict]:
    chars_a, lines_a, warnings_a = _extract_text(reference_page, region)
    chars_b, lines_b, warnings_b = _extract_text(candidate_page, region)
    warnings = warnings_a + warnings_b
    context = {
        "reference_chars": chars_a, "candidate_chars": chars_b,
        "reference_lines": lines_a, "candidate_lines": lines_b, "line_pairs": [],
    }
    result = {
        "matched_characters": 0,
        "reference_characters": len(chars_a),
        "candidate_characters": len(chars_b),
        "matched_lines": 0,
        "median_dx_mm": None,
        "median_dy_mm": None,
        "p95_position_error_mm": None,
        "reference_line_spacing_mm": None,
        "candidate_line_spacing_mm": None,
        "line_spacing_delta_mm": None,
        "font_size_delta_pt": None,
        "match_ratio": None,
        "position_measurement_available": False,
        "unmatched_reference_characters": len(chars_a),
        "unmatched_candidate_characters": len(chars_b),
        "changed_font_name_characters": None,
        "line_spacing_samples": 0,
        "line_break_change_count": None,
    }
    if not chars_a or not chars_b:
        warnings.append("一侧或双方区域没有可提取文字：不运行 OCR，文字位置和行距指标留空。")
        return result, [], warnings, context
    if max(len(chars_a), len(chars_b)) > MAX_TEXT_CHARACTERS:
        warnings.append(f"区域非空白文字超过 {MAX_TEXT_CHARACTERS} 字符，跳过文字位置、字号和行距匹配；内容诊断受独立字符上限约束，可缩小选区。")
        return result, [], warnings, context
    text_a = "".join(char["c"] for char in chars_a)
    text_b = "".join(char["c"] for char in chars_b)
    if text_a == text_b:
        pairs = list(zip(range(len(chars_a)), range(len(chars_b))))
    else:
        # 关闭 autojunk，避免长篇中文里的常见字被自动忽略。
        matcher = SequenceMatcher(a=text_a, b=text_b, autojunk=False)
        pairs = [(block.a + i, block.b + i) for block in matcher.get_matching_blocks() for i in range(block.size)]
    matched = len(pairs)
    context["pairs"] = pairs
    ratio = 2 * matched / (len(chars_a) + len(chars_b))
    result.update({
        "matched_characters": matched,
        "unmatched_reference_characters": len(chars_a) - matched,
        "unmatched_candidate_characters": len(chars_b) - matched,
        "match_ratio": ratio,
        "line_break_change_count": sum(
            (chars_a[i0]["line_id"] != chars_a[i1]["line_id"]) != (chars_b[j0]["line_id"] != chars_b[j1]["line_id"])
            for (i0, j0), (i1, j1) in zip(pairs, pairs[1:])
            if i1 == i0 + 1 and j1 == j0 + 1
        ),
    })
    if matched != len(chars_a) or matched != len(chars_b):
        warnings.append(
            f"存在未匹配文字：参考 {len(chars_a) - matched} 字符、待比较 {len(chars_b) - matched} 字符；"
            "位置与字号指标仅统计匹配部分，请复核内容增删与提取顺序。"
        )
    if ratio < 0.6 or not pairs or (text_a != text_b and matched < 4):
        warnings.append("共同文字过少或文本顺序差异较大：仅报告匹配数量，不据此计算排版位置。")
        return result, [], warnings, context
    if ratio < 0.9:
        warnings.append("两侧文本内容差异较多：文字位置仅针对按顺序匹配到的共同字符，重复文本可能存在歧义。")
    if result["line_break_change_count"]:
        warnings.append("存在换行位置变化；逐行匹配和行距统计仅覆盖稳定对应行。")
    offsets = np.asarray([
        (chars_b[j]["x_mm"] - chars_a[i]["x_mm"], chars_b[j]["y_mm"] - chars_a[i]["y_mm"])
        for i, j in pairs
    ])
    size_delta = [chars_b[j]["size_pt"] - chars_a[i]["size_pt"] for i, j in pairs]
    result.update({
        "median_dx_mm": float(np.median(offsets[:, 0])),
        "median_dy_mm": float(np.median(offsets[:, 1])),
        "p95_position_error_mm": float(np.percentile(np.linalg.norm(offsets, axis=1), 95)),
        "font_size_delta_pt": float(np.median(size_delta)),
        "position_measurement_available": True,
        "changed_font_name_characters": sum(
            _font_name(chars_a[i]["font"]) != _font_name(chars_b[j]["font"]) for i, j in pairs
        ),
    })
    grouped = defaultdict(list)
    for i, j in pairs:
        grouped[(chars_a[i]["line_id"], chars_b[j]["line_id"])].append((i, j))
    best_a = defaultdict(list)
    best_b = defaultdict(list)
    for (a, b), values in grouped.items():
        best_a[a].append((len(values), b))
        best_b[b].append((len(values), a))
    line_pairs = []
    for (a, b), values in grouped.items():
        # 换行变化可能产生一对多关系，只保留双方唯一的最大匹配行。
        if len([v for v in best_a[a] if v[0] == max(best_a[a])[0]]) != 1:
            continue
        if len([v for v in best_b[b] if v[0] == max(best_b[b])[0]]) != 1:
            continue
        if max(best_a[a])[1] != b or max(best_b[b])[1] != a:
            continue
        if len(values) / max(len(lines_a[a]["chars"]), len(lines_b[b]["chars"])) < 0.6:
            continue
        line_pairs.append((a, b, values))
    line_pairs.sort()
    context["line_pairs"] = line_pairs
    differences = []
    for a, b, values in line_pairs:
        line_a, line_b = lines_a[a], lines_b[b]
        difference = {
            "text": line_a["text"],
            "reference_line": a + 1,
            "candidate_line": b + 1,
            "candidate_text": line_b["text"],
            "dx_mm": float(np.median([chars_b[j]["x_mm"] - chars_a[i]["x_mm"] for i, j in values])),
            "dy_mm": float(np.median([chars_b[j]["y_mm"] - chars_a[i]["y_mm"] for i, j in values])),
            "reference_font_size_pt": line_a["size_pt"],
            "candidate_font_size_pt": line_b["size_pt"],
            "reference_fonts": sorted({_font_name(char["font"]) for char in line_a["chars"]}),
            "candidate_fonts": sorted({_font_name(char["font"]) for char in line_b["chars"]}),
            "reference_bbox_mm": _line_bbox(line_a),
            "candidate_bbox_mm": _line_bbox(line_b),
        }
        differences.append(difference)
    result["matched_lines"] = len(differences)
    spacing_a, spacing_b = [], []
    for previous, current in zip(line_pairs, line_pairs[1:]):
        a0, b0, _ = previous
        a1, b1, _ = current
        if a1 != a0 + 1 or b1 != b0 + 1:
            continue
        four_lines = (lines_a[a0], lines_a[a1], lines_b[b0], lines_b[b1])
        if not all(line["horizontal"] for line in four_lines):
            continue
        # 不把明显不同字号的标题、同一行碎片或大段空白当作正文行距。
        if max(line["size_pt"] for line in four_lines) - min(line["size_pt"] for line in four_lines) > 0.5:
            continue
        if not _lines_overlap(lines_a[a0], lines_a[a1]) or not _lines_overlap(lines_b[b0], lines_b[b1]):
            continue
        gap_a = lines_a[a1]["y_mm"] - lines_a[a0]["y_mm"]
        gap_b = lines_b[b1]["y_mm"] - lines_b[b0]["y_mm"]
        size_mm = float(np.median([line["size_pt"] for line in four_lines])) * MM_PER_PT
        if not all(size_mm * 0.5 <= gap <= size_mm * 3 for gap in (gap_a, gap_b)):
            continue
        spacing_a.append(gap_a)
        spacing_b.append(gap_b)
    if spacing_a:
        result["reference_line_spacing_mm"] = float(np.median(spacing_a))
        result["candidate_line_spacing_mm"] = float(np.median(spacing_b))
        result["line_spacing_delta_mm"] = float(np.median(np.asarray(spacing_b) - np.asarray(spacing_a)))
    result["line_spacing_samples"] = len(spacing_a)
    return result, differences, warnings, context


def _font_name(value: str) -> str:
    # PDF 字体子集前缀不同，不足以说明字形或字体不同。
    prefix, separator, rest = value.partition("+")
    return rest if separator and len(prefix) == 6 and prefix.isalpha() and prefix.isupper() else value


def _line_bbox(line: dict) -> list[float]:
    boxes = np.asarray([char["bbox_mm"] for char in line["chars"]])
    return [float(boxes[:, 0].min()), float(boxes[:, 1].min()), float(boxes[:, 2].max()), float(boxes[:, 3].max())]


def _lines_overlap(a: dict, b: dict) -> bool:
    box_a, box_b = _line_bbox(a), _line_bbox(b)
    overlap = min(box_a[2], box_b[2]) - max(box_a[0], box_b[0])
    return overlap >= 0.2 * min(box_a[2] - box_a[0], box_b[2] - box_b[0])


def compare_pages(
    reference_data: bytes,
    candidate_data: bytes,
    reference_page: int = 1,
    candidate_page: int = 1,
    dpi: int = 150,
    region_mm: tuple[float, float, float, float] | None = None,
    ink_threshold: int = 220,
) -> ComparisonResult:
    """页码从 1 开始，选区为显示后可见页左上角起算的毫米坐标。"""
    if type(dpi) is not int or not 72 <= dpi <= 600:
        raise ValueError("DPI 必须是 72 至 600 的整数。")
    if type(ink_threshold) is not int or not 1 <= ink_threshold <= 254:
        raise ValueError("前景阈值必须是 1 至 254 的整数。")
    with _open_pdf(reference_data) as doc_a, _open_pdf(candidate_data) as doc_b:
        for number, document, label in ((reference_page, doc_a, "参考"), (candidate_page, doc_b, "待比较")):
            if type(number) is not int or not 1 <= number <= document.page_count:
                raise ValueError(f"{label}页码必须为 1 至 {document.page_count}。")
        page_a, page_b = doc_a[reference_page - 1], doc_b[candidate_page - 1]
        width_mm = max(page_a.rect.width, page_b.rect.width) * MM_PER_PT
        height_mm = max(page_a.rect.height, page_b.rect.height) * MM_PER_PT
        width = math.ceil(max(page_a.rect.width, page_b.rect.width) * dpi / 72)
        height = math.ceil(max(page_a.rect.height, page_b.rect.height) * dpi / 72)
        if max(width, height) > MAX_RENDER_SIDE or width * height > MAX_RENDER_PIXELS:
            raise ValueError("页面在该 DPI 下过大，请降低 DPI；最大画布为 4000 万像素，长边不超过 10000 像素。")
        if region_mm is not None:
            if len(region_mm) != 4 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in region_mm):
                raise ValueError("选区必须是四个有限数值：x0、y0、x1、y1（毫米）。")
            region_mm = tuple(float(v) for v in region_mm)
            x0, y0, x1, y1 = region_mm
            if not (0 <= x0 < x1 <= width_mm + 1e-6 and 0 <= y0 < y1 <= height_mm + 1e-6):
                raise ValueError("选区必须位于共同页面画布内，且宽高均大于零。")
        region = region_mm or (0, 0, width_mm, height_mm)
        x0_px = max(0, math.floor(region[0] * dpi / 25.4))
        y0_px = max(0, math.floor(region[1] * dpi / 25.4))
        x1_px = min(width, math.ceil(region[2] * dpi / 25.4))
        y1_px = min(height, math.ceil(region[3] * dpi / 25.4))
        rgb_a = _render(page_a, dpi, (height, width))
        images = {"reference": _encode_png(rgb_a)}
        image_a = cv2.cvtColor(rgb_a, cv2.COLOR_RGB2GRAY)
        rgb_b = _render(page_b, dpi, (height, width))
        images["candidate"] = _encode_png(rgb_b)
        image_b = cv2.cvtColor(rgb_b, cv2.COLOR_RGB2GRAY)
        color_delta = cv2.absdiff(rgb_a, rgb_b)
        color_mean = sum(cv2.mean(color_delta[y0_px:y1_px, x0_px:x1_px])[:3]) / (3 * 255)
        delta = np.max(color_delta, axis=2)
        del color_delta, rgb_a, rgb_b
        visual = _visual_metrics(image_a[y0_px:y1_px, x0_px:x1_px], image_b[y0_px:y1_px, x0_px:x1_px], dpi, ink_threshold, (x0_px, y0_px))
        visual.update({
            "rgb_mean_absolute_error": color_mean,
            "rgb_changed_pixels": int(np.count_nonzero(delta[y0_px:y1_px, x0_px:x1_px])),
            "foreground_color_space": "RGB-to-grayscale",
        })
        text, line_differences, text_warnings, text_context = _text_metrics(page_a, page_b, region_mm)
        warnings = _page_warnings(page_a) + _page_warnings(page_b) + text_warnings
        if doc_a.is_repaired or doc_b.is_repaired:
            warnings.append("解析器修复过输入 PDF，建议检查源文件和页面显示。")
        if abs(page_a.rect.width - page_b.rect.width) > 0.01 or abs(page_a.rect.height - page_b.rect.height) > 0.01:
            warnings.append("两侧可见页尺寸不同：按左上角原点放入共同白色画布，没有缩放或对齐补偿。")
        if not visual["foreground_union_pixels"]:
            warnings.append("当前阈值下测量区域双方均无前景；前景差异率和轮廓距离留空，仍需查看 RGB 差异，不能解释为页面完全一致。")
        elif not visual["reference_foreground_pixels"] or not visual["candidate_foreground_pixels"]:
            warnings.append("当前测量区域只有一侧存在前景，无法计算双方轮廓距离。")
        report = {
            "schema_version": 1,
            "settings": {
                "dpi": dpi,
                "ink_threshold": ink_threshold,
                "region_mm": list(region_mm) if region_mm else None,
                "effective_region_px": [x0_px, y0_px, x1_px, y1_px],
                "pixel_pitch_mm": 25.4 / dpi,
                "text_region_selection": "character-bbox-center",
            },
            "reference": {"page": reference_page, **_page_info(page_a)},
            "candidate": {"page": candidate_page, **_page_info(page_b)},
            "canvas": {"width_px": width, "height_px": height, "width_mm": width_mm, "height_mm": height_mm, "origin": "visible-page-top-left"},
            "visual": visual,
            "text": text,
            "line_differences": line_differences,
            "warnings": list(dict.fromkeys(warnings)),
        }
        # 参考单独出现为红色，待比较单独出现为蓝色，重合墨迹为深色。
        report["diagnostics"] = build_diagnostics(report, text_context, delta[y0_px:y1_px, x0_px:x1_px], (x0_px, y0_px))
        overlay = np.stack((image_b, np.minimum(image_a, image_b), image_a), axis=2)
        images["overlay"] = _encode_png(overlay)
        del overlay
        # 分步编码和复用 uint8 缓冲，避免 600 DPI 时同时持有多个大 RGB / float 数组。
        base = np.minimum(image_a, image_b)
        np.floor_divide(base, 5, out=base)
        np.add(base, 204, out=base)
        faded = cv2.multiply(base, 255 - delta, scale=1 / 255)
        heatmap = np.stack((np.maximum(base, delta), faded, faded), axis=2)
        images["diff"] = _encode_png(heatmap)
        return ComparisonResult(report=report, images=images)
