"""导出不依赖网络、可离线保存和复核的比较报告。"""

from __future__ import annotations

import base64
from html import escape
from io import BytesIO
import json
import math
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile


IMAGE_LABELS = {
    "reference": "参考页",
    "candidate": "待比较页",
    "overlay": "红蓝叠加",
    "diff": "差异图",
}


DIAGNOSTIC_STYLES = """
.diagnostics { margin-top:24px; }
.diagnostics-intro { color:#64748b; font-size:12px; margin-bottom:16px; }
.diagnostic { border:1px solid #dfe6ef; border-left:3px solid #5b7fb7; border-radius:9px; padding:18px 20px; margin-top:13px; background:#fff; overflow-wrap:anywhere; }
.diagnostic-heading { display:flex; align-items:center; gap:9px; flex-wrap:wrap; margin-bottom:9px; }
.diagnostic-heading strong { font-size:14px; }
.diagnostic-heading code { color:#718096; font-size:10px; }
.diagnostic-status { display:inline-block; background:#eaf5ef; color:#28765b; border-radius:4px; padding:2px 7px; font-size:10px; }
.diagnostic-status.review { background:#fff5dc; color:#876517; }
.diagnostic-observation { font-weight:600; margin-bottom:13px; }
.diagnostic-locations { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin:12px 0; }
.diagnostic-location { min-width:0; border-radius:6px; background:#f5f7fb; padding:11px 13px; }
.diagnostic-label { display:block; color:#64748b; font-size:11px; margin-bottom:5px; }
.diagnostic-text { margin:4px 0; white-space:pre-wrap; }
.diagnostic-context,.diagnostic-coordinates { color:#6a7c92; font-size:11px; }
.diagnostic-measurements { overflow:auto; margin:12px 0; }
.diagnostic-measurements table { min-width:540px; font-size:11px; }
.diagnostic-measurements th,.diagnostic-measurements td { padding:8px 10px; white-space:normal; }
.diagnostic-target { background:#eef4fd; border-radius:6px; padding:11px 13px; margin-top:12px; }
.diagnostic-target p { margin:2px 0; }
.diagnostic-reasons { color:#687b92; font-size:11px; padding-left:19px; margin:10px 0 0; }
.diagnostic-evidence { border:0; padding:0; margin-top:11px; background:transparent; }
.diagnostic-evidence summary { font-size:11px; font-weight:500; }
.diagnostic-evidence pre { max-height:240px; }
.diagnostic-crops { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; margin:13px 0; }
.diagnostic-crop { margin:0; min-width:0; border:1px solid #dfe6ef; border-radius:6px; overflow:hidden; }
.diagnostic-crop figcaption { padding:5px 8px; font-size:10px; background:#f5f7fb; }
.diagnostic-crop svg { display:block; width:100%; max-height:220px; background:white; }
.diagnostic-definitions { position:absolute; width:0; height:0; overflow:hidden; }
.diagnostic-limits { color:#795c22; font-size:12px; padding-left:19px; margin:14px 0; }
@media(max-width:650px) { .diagnostic { padding:14px; } .diagnostic-locations { grid-template-columns:1fr; } .diagnostic-crops { grid-template-columns:1fr; } }
@media print { .diagnostic { break-inside:avoid; } .diagnostic-evidence { display:none; } }
"""


DIAGNOSTIC_LABELS = {
    "page.geometry": "页面几何变化",
    "text.content": "文字内容变化",
    "text.position": "文字位置变化",
    "text.font_size": "字号变化",
    "text.font_name": "字体名变化",
    "text.reflow": "换行变化",
    "text.line_spacing": "相邻行距变化",
    "text.line_pattern": "连续行偏移规律",
    "visual.region": "局部视觉差异",
}


MEASUREMENT_LABELS = {
    "x_mm": "横向位置", "y_mm": "纵向位置", "dx_mm": "横向偏移", "dy_mm": "纵向偏移",
    "font_size_pt": "字号", "font_name": "字体名", "fonts": "字体名", "width_mm": "宽度",
    "height_mm": "高度", "rotation": "旋转", "cropbox_pt": "裁切框", "line_spacing_mm": "基线行距",
    "initial_dx_mm": "初始横向偏移", "initial_dy_mm": "初始纵向偏移",
    "dx_per_line_mm": "逐行横向增量", "dy_per_line_mm": "逐行纵向增量",
    "per_line_dx_mm": "逐行横向增量", "per_line_dy_mm": "逐行纵向增量",
    "baseline_spacing_mm": "基线行距", "extracted_text": "提取文字", "changed_pixels": "RGB 变化像素",
    "line_break_relations": "换行关系变化", "mediabox_pt": "原始页面框",
}


def _text(value: Any) -> str:
    return escape(str(value), quote=True)


def _number(value: Any, digits: int = 3, signed: bool = False) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return "不可测"
    # 防止浮点误差使零位移显示为 -0.000。
    if abs(value) < 0.5 * 10 ** -digits:
        value = 0
    return format(value, f"{'+' if signed else ''}.{digits}f")


def _count(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else "不可测"


def _ratio(value: Any) -> str:
    return f"{_number(value * 100, 2)} %" if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else "不可测"


def _bounds(value: Any) -> str:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return "不可测"
    return "(" + ", ".join(_number(item, 2) for item in value) + ") mm"


def _card(label: str, value: str, unit: str, detail: str) -> str:
    return (
        '<article class="metric"><span class="metric-label">'
        f'{_text(label)}</span><div class="metric-value">{_text(value)} '
        f'<small>{_text(unit)}</small></div><p>{_text(detail)}</p></article>'
    )


def _table(headers: list[str], rows: list[list[Any]], *, numeric_columns: set[int] | None = None) -> str:
    numeric_columns = numeric_columns or set()
    header = "".join(f"<th>{_text(label)}</th>" for label in headers)
    body = []
    for row in rows:
        cells = "".join(
            f'<td class="{"num" if index in numeric_columns else ""}">{_text(value)}</td>'
            for index, value in enumerate(row)
        )
        body.append(f"<tr>{cells}</tr>")
    if not body:
        body.append(f'<tr><td class="empty" colspan="{len(headers)}">没有可可靠匹配的记录。</td></tr>')
    table_class = ' class="line-table"' if len(headers) > 3 else ""
    return f'<div class="table-wrap"><table{table_class}><thead><tr>{header}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _file_card(side: str, report: dict[str, Any]) -> str:
    info = report.get(side) or {}
    file_info = (report.get("files") or {}).get(side) or {}
    size = f'{_number(info.get("width_mm"), 2)} × {_number(info.get("height_mm"), 2)} mm'
    return (
        f'<article class="file-card {side}"><span class="eyebrow">'
        f'{"REFERENCE / 参考" if side == "reference" else "CANDIDATE / 待比较"}</span>'
        f'<h3>{_text(file_info.get("filename", "未提供文件名"))}</h3>'
        f'<p>第 {_text(info.get("page", "—"))} 页 · {_text(size)} · 旋转 {_text(info.get("rotation", 0))}°</p>'
        f'<p class="hash-label">SHA-256</p><code class="hash">{_text(file_info.get("sha256", "未提供"))}</code></article>'
    )


def _region_marker(report: dict[str, Any], region: Any) -> str:
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        return ""
    reference, candidate = report.get("reference") or {}, report.get("candidate") or {}
    canvas = report.get("canvas") or {}
    width = canvas.get("width_mm")
    height = canvas.get("height_mm")
    if width is None or height is None:
        widths = [page.get("width_mm", 0) for page in (reference, candidate)]
        heights = [page.get("height_mm", 0) for page in (reference, candidate)]
        if any(not isinstance(value, (int, float)) for value in widths + heights):
            return ""
        width, height = max(widths), max(heights)
    values = [*region, width, height]
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) for value in values):
        return ""
    x0, y0, x1, y1 = region
    if not (0 <= x0 < x1 <= width + 1e-6 and 0 <= y0 < y1 <= height + 1e-6):
        return ""
    # 百分比相对于整个共同画布，而非各文档单独尺寸或带留白的图框。
    style = (
        f"left:{x0 / width * 100:.8f}%;top:{y0 / height * 100:.8f}%;"
        f"width:{(min(x1, width) - x0) / width * 100:.8f}%;"
        f"height:{(min(y1, height) - y0) / height * 100:.8f}%;"
    )
    return f'<span class="roi-box" role="img" aria-label="测量区域" style="{style}"></span>'


def _diagnostic_value(value: Any) -> str:
    if value is None:
        return "不可测"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _number(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    return str(value)


def _diagnostic_location(value: Any, label: str) -> str:
    location = value if isinstance(value, dict) else {}
    text = location.get("text")
    text_html = f'<p class="diagnostic-text">{_text(text if text else "（空文本）")}</p>' if text is not None else ""
    context = []
    for key, heading in (("context_before", "前文"), ("context_after", "后文")):
        if location.get(key):
            context.append(f'<p class="diagnostic-context">{heading} {_text(location[key])}</p>')
    page = location.get("page")
    page_label = f"第 {page} 页" if page is not None else "页码未提供"
    anchor = location.get("anchor_bbox_mm")
    anchor_html = f'<p class="diagnostic-coordinates">相邻文字锚点 {_text(_bounds(anchor))}</p>' if anchor else ""
    return (
        '<div class="diagnostic-location">'
        f'<span class="diagnostic-label">{label} · {_text(page_label)}</span>{text_html}'
        f'<p class="diagnostic-coordinates">边界 {_text(_bounds(location.get("bbox_mm")))}</p>'
        f'{anchor_html}{"".join(context)}</div>'
    )


def _diagnostic_crop_bounds(finding: dict, report: dict) -> tuple[float, float, float, float] | None:
    canvas = report.get("canvas") or {}
    width, height = canvas.get("width_px"), canvas.get("height_px")
    dpi = (report.get("settings") or {}).get("dpi")
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0 for value in (width, height, dpi)):
        return None
    boxes = []
    for side in ("reference", "candidate"):
        location = finding.get(side)
        bounds = location.get("bbox_mm") if isinstance(location, dict) else None
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            continue
        if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) for value in bounds):
            continue
        if bounds[0] < bounds[2] and bounds[1] < bounds[3]:
            boxes.append(bounds)
    if not boxes:
        return None
    scale = dpi / 25.4
    padding = 2 * scale
    left = max(0, min(box[0] for box in boxes) * scale - padding)
    top = max(0, min(box[1] for box in boxes) * scale - padding)
    right = min(width, max(box[2] for box in boxes) * scale + padding)
    bottom = min(height, max(box[3] for box in boxes) * scale + padding)
    if right <= left or bottom <= top:
        return None
    return left, top, right - left, bottom - top


def _diagnostics_html(report: dict, images: dict[str, bytes] | None = None, *, prefix: str = "diagnostics") -> str:
    """单页与整本报告共用诊断展示，来源文本始终作为纯文本转义。"""
    diagnostics = report.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return ""
    raw_findings = diagnostics.get("findings")
    findings = raw_findings if isinstance(raw_findings, list) else []
    cards, crop_sources = [], {}
    preview_count = 0
    images = images or {}
    for index, raw in enumerate(findings, start=1):
        finding = raw if isinstance(raw, dict) else {}
        kind = str(finding.get("type", "未分类差异"))
        reliability = finding.get("reliability") or {}
        reliability = reliability if isinstance(reliability, dict) else {}
        reliable = reliability.get("level") == "high"
        reliability_label = "可靠" if reliable else "需要复核"
        reasons = reliability.get("reasons")
        reasons = reasons if isinstance(reasons, list) else []
        reason_html = '<ul class="diagnostic-reasons">' + "".join(f'<li>{_text(reason)}</li>' for reason in reasons) + '</ul>' if reasons else ""
        measurements = finding.get("measurements") or {}
        rows = []
        if isinstance(measurements, dict):
            for name, value in measurements.items():
                if not isinstance(value, dict):
                    continue
                rows.append([
                    MEASUREMENT_LABELS.get(name, name),
                    _diagnostic_value(value.get("reference")),
                    _diagnostic_value(value.get("candidate")),
                    _diagnostic_value(value.get("delta")),
                    value.get("unit", "—"),
                ])
        measurement_html = '<div class="diagnostic-measurements">' + _table(["测量项", "参考值", "当前值", "当前减参考", "单位"], rows) + '</div>' if rows else ""
        target = finding.get("target") or {}
        target = target if isinstance(target, dict) else {}
        target_html = ""
        if target:
            adjustment = target.get("candidate_adjustment")
            adjustment_html = f'<p class="diagnostic-coordinates">调整量 {_text(_diagnostic_value(adjustment))}</p>' if adjustment else ""
            target_html = (
                '<div class="diagnostic-target"><span class="diagnostic-label">输出调整目标</span>'
                f'<p>{_text(target.get("description", "结合定位与参考值核查"))}</p>{adjustment_html}</div>'
            )
        crops = []
        bounds = _diagnostic_crop_bounds(finding, report) if kind == "visual.region" and preview_count < 12 else None
        if bounds:
            viewbox = " ".join(f"{value:.6f}" for value in bounds)
            for key in ("reference", "candidate", "diff"):
                if isinstance(images.get(key), bytes):
                    crop_sources[key] = images[key]
                    crops.append(
                        f'<figure class="diagnostic-crop"><figcaption>{IMAGE_LABELS[key]}</figcaption>'
                        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}" role="img" aria-label="{IMAGE_LABELS[key]}局部对照">'
                        f'<use href="#{prefix}-image-{key}"/></svg></figure>'
                    )
            if crops:
                preview_count += 1
        crop_html = f'<div class="diagnostic-crops">{"".join(crops)}</div>' if crops else ""
        evidence = reliability.get("evidence")
        evidence_html = (
            '<details class="diagnostic-evidence"><summary>匹配与测量依据</summary>'
            f'<pre>{_text(json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False))}</pre></details>'
            if evidence else ""
        )
        cards.append(
            f'<article class="diagnostic" id="{prefix}-{index:04d}"><div class="diagnostic-heading">'
            f'<strong>{_text(DIAGNOSTIC_LABELS.get(kind, kind))}</strong>'
            f'<span class="diagnostic-status{"" if reliable else " review"}">{reliability_label}</span>'
            f'<code>{_text(finding.get("id", index))}</code></div>'
            f'<p class="diagnostic-observation">{_text(finding.get("observation", "观测未提供"))}</p>'
            f'<div class="diagnostic-locations">{_diagnostic_location(finding.get("reference"), "参考")}'
            f'{_diagnostic_location(finding.get("candidate"), "当前")}</div>'
            f'{measurement_html}{crop_html}{target_html}{reason_html}{evidence_html}</article>'
        )
    definitions = ""
    if crop_sources:
        canvas = report["canvas"]
        source_images = "".join(
            f'<image id="{prefix}-image-{key}" width="{canvas["width_px"]}" height="{canvas["height_px"]}" '
            f'href="data:image/png;base64,{base64.b64encode(data).decode("ascii")}"/>'
            for key, data in crop_sources.items()
        )
        definitions = f'<svg xmlns="http://www.w3.org/2000/svg" class="diagnostic-definitions" aria-hidden="true"><defs>{source_images}</defs></svg>'
    limitations = diagnostics.get("limitations")
    limitations = limitations if isinstance(limitations, list) else []
    if diagnostics.get("truncated"):
        limitations = ["诊断清单已达到容量限制，需结合覆盖信息检查未列出的差异。", *limitations]
    limits_html = '<ul class="diagnostic-limits">' + "".join(f'<li>{_text(item)}</li>' for item in limitations) + '</ul>' if limitations else ""
    metadata = {key: diagnostics[key] for key in ("conventions", "tolerances", "coverage", "summary") if key in diagnostics}
    metadata_html = (
        '<details class="diagnostic-evidence"><summary>诊断口径、阈值与覆盖信息</summary>'
        f'<pre>{_text(json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False))}</pre></details>'
        if metadata else ""
    )
    cards_html = "".join(cards) if cards else '<p class="empty">当前阈值与测量覆盖内未列出诊断项。</p>'
    return (
        '<section class="diagnostics"><h2>差异诊断</h2>'
        f'<p class="diagnostics-intro">共列出 {len(findings)} 项观测。差值为当前减参考，调整量针对当前文档。'
        '位置以可见页左上角为原点，向右、向下为正。位置与边界单位 mm，字号单位 PDF pt。'
        '输出目标需结合源码定位后应用。局部图最多预览前 12 个视觉差异区域。</p>'
        f'{definitions}{cards_html}'
        f'{limits_html}{metadata_html}</section>'
    )


def build_html(report: dict[str, Any], images: dict[str, bytes]) -> str:
    """生成自包含 HTML；所有来自文件和测量报告的文本均转义。"""
    settings = report.get("settings") or {}
    visual = report.get("visual") or {}
    text = report.get("text") or {}
    warnings = report.get("warnings") or []
    region = settings.get("region_mm")
    if isinstance(region, (list, tuple)) and len(region) == 4:
        region_text = "区域坐标 (x₀, y₀, x₁, y₁)：" + ", ".join(_number(v, 2) for v in region) + " mm"
    else:
        region_text = "全页测量"
    cards = "".join([
        _card("墨迹不重合率", _ratio(visual.get("foreground_mismatch_ratio")), "", "二值前景异或面积 ÷ 双方前景并集面积，越小越接近。"),
        _card("横向中位偏移", _number(text.get("median_dx_mm"), signed=True), "mm", "仅统计匹配文字；正值表示待比较内容向右偏移。"),
        _card("纵向中位偏移", _number(text.get("median_dy_mm"), signed=True), "mm", "仅统计匹配文字；正值表示待比较内容向下偏移。"),
        _card("行距中位差", _number(text.get("line_spacing_delta_mm"), signed=True), "mm", "待比较减参考；只统计符合条件的相邻匹配行。"),
    ])
    rows = [
        ["匹配字符", _count(text.get("matched_characters")), "按文本顺序匹配，不代表所有字符都可可靠定位"],
        ["参考 / 待比较字符数", f'{_count(text.get("reference_characters"))} / {_count(text.get("candidate_characters"))}', "当前测量范围内可提取的非空白字符"],
        ["未匹配参考 / 待比较字符数", f'{_count(text.get("unmatched_reference_characters"))} / {_count(text.get("unmatched_candidate_characters"))}', "缺字、增字、断行或提取异常需要结合原图复核"],
        ["匹配行", _count(text.get("matched_lines")), "满足匹配条件的文本行"],
        ["P95 字符位置误差", f'{_number(text.get("p95_position_error_mm"))} mm', "匹配字符二维位移长度的第 95 百分位"],
        ["参考 / 待比较基线行距", f'{_number(text.get("reference_line_spacing_mm"))} / {_number(text.get("candidate_line_spacing_mm"))} mm', "合格相邻匹配行的基线间距中位数"],
        ["字号中位差", f'{_number(text.get("font_size_delta_pt"), signed=True)} PDF pt', "待比较减参考；PDF pt 为 1/72 英寸，字号数值不等于字形墨迹高度"],
        ["字体名发生变化的匹配字符", _count(text.get("changed_font_name_characters")), "字体名仅作线索，不足以证明两个嵌入字体完全一致"],
        ["平均 / P95 轮廓距离", f'{_number(visual.get("edge_distance_mean_mm"))} / {_number(visual.get("edge_distance_p95_mm"))} mm', "双方前景轮廓到另一侧最近轮廓的对称距离，不能代替字符匹配"],
    ]
    if visual.get("ssim") is not None:
        rows.append(["SSIM", _number(visual.get("ssim"), 5), "图像结构相似性；不是整体一致程度的百分比"])
    if "rgb_mean_absolute_error" in visual:
        rows.extend([
            ["RGB 平均绝对差", _number(visual.get("rgb_mean_absolute_error"), 6), "当前区域全部像素的 RGB 通道差归一化到 0–1；会受白色背景面积影响"],
            ["RGB 变化像素数", _count(visual.get("rgb_changed_pixels")), "至少一个 RGB 通道发生变化的像素，不使用墨迹阈值过滤"],
        ])
    if "reference_foreground_bbox_mm" in visual:
        rows.extend([
            ["参考墨迹边界", _bounds(visual.get("reference_foreground_bbox_mm")), "当前区域内前景边界 (x₀, y₀, x₁, y₁)，采用整页绝对坐标"],
            ["待比较墨迹边界", _bounds(visual.get("candidate_foreground_bbox_mm")), "墨迹边界不能直接还原源文档的页面或段落设置"],
        ])
    if "line_break_change_count" in text:
        rows.append(["检测到的换行关系变化", _count(text.get("line_break_change_count")), "基于共同字符的行归属变化；提取异常可能影响结果"])
    warning_html = (
        '<section class="notice"><h2>测量提示</h2><ul>'
        + "".join(f"<li>{_text(warning)}</li>" for warning in warnings)
        + "</ul></section>"
        if warnings else ""
    )
    line_rows = []
    for index, line in enumerate(report.get("line_differences") or [], start=1):
        line_rows.append([
            index,
            line.get("text", ""),
            line.get("candidate_text", line.get("text", "")),
            _number(line.get("dx_mm"), signed=True),
            _number(line.get("dy_mm"), signed=True),
            _number(line.get("reference_font_size_pt"), 2),
            _number(line.get("candidate_font_size_pt"), 2),
        ])
    figures = []
    marker = _region_marker(report, region)
    for key, label in IMAGE_LABELS.items():
        data = images.get(key)
        image = (
            '<div class="paper-image">'
            f'<img alt="{label}" src="data:image/png;base64,{base64.b64encode(data).decode("ascii")}">'
            f'{marker}</div>'
            if data else '<p class="empty">此图未生成。</p>'
        )
        figures.append(f'<figure><figcaption>{label}</figcaption><div class="paper">{image}</div></figure>')
    report_json = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    generated_at = report.get("generated_at", "未提供")
    diagnostics_html = _diagnostics_html(report, images)
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>PDF 比较报告 · PDF Layout Diff</title>
<style>
{DIAGNOSTIC_STYLES}
:root {{ color-scheme: light; --ink:#1c293b; --muted:#64748b; --line:#e0e7ef; --accent:#215bbf; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:#edf1f6; color:var(--ink); font:14px/1.65 "Segoe UI","Microsoft YaHei",sans-serif; }}
main {{ max-width:1280px; margin:auto; padding:52px 40px 40px; }}
.eyebrow {{ font-size:11px; font-weight:700; letter-spacing:1.6px; color:var(--accent); }}
header {{ padding-bottom:28px; border-bottom:1px solid #cdd7e4; margin-bottom:28px; }}
h1 {{ margin:8px 0 10px; font-size:34px; line-height:1.3; letter-spacing:-.7px; }}
h2 {{ font-size:18px; margin:0 0 16px; }}
h3 {{ font-size:16px; margin:10px 0; word-break:break-word; }}
p {{ margin:5px 0; }}
.subtle {{ color:var(--muted); }}
.tag {{ display:inline-block; border:1px solid #cbd6e5; border-radius:5px; padding:3px 10px; margin-top:12px; margin-right:6px; color:#43556d; font-size:12px; }}
.files,.metrics,.figures {{ display:grid; gap:16px; }}
.files,.figures {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
.metrics {{ grid-template-columns:repeat(4,minmax(0,1fr)); margin:24px 0; }}
.file-card,.metric,section.sheet {{ background:#fff; border:1px solid var(--line); border-radius:12px; }}
.file-card {{ padding:20px 22px; border-top:3px solid #c94450; }}
.candidate {{ border-top-color:#397bd8; }}
.reference .eyebrow {{ color:#b8424d; }}
.candidate .eyebrow {{ color:#306dbf; }}
.file-card p {{ font-size:12px; color:var(--muted); }}
.hash-label {{ margin-top:15px; font-size:10px!important; letter-spacing:1px; }}
.hash {{ display:block; overflow-wrap:anywhere; font-size:10px; color:#77859a; line-height:1.5; }}
.metric {{ padding:20px; }}
.metric-label {{ color:#4d617a; font-size:12px; font-weight:600; }}
.metric-value {{ margin:8px 0; font-size:26px; font-variant-numeric:tabular-nums; font-weight:700; line-height:1.2; }}
.metric-value small {{ font-size:12px; font-weight:500; color:var(--muted); }}
.metric p {{ font-size:11px; color:var(--muted); }}
.sheet {{ padding:24px; margin-top:20px; }}
.notice {{ margin-top:24px; border:1px solid #e7d59e; background:#fff9e9; padding:20px 24px; border-radius:12px; color:#775a20; }}
.notice h2 {{ font-size:15px; margin-bottom:8px; }}
.notice ul {{ padding-left:20px; margin:0; }}
.notice li+li {{ margin-top:5px; }}
.table-wrap {{ overflow:auto; }}
table {{ width:100%; min-width:680px; border-collapse:collapse; font-size:12px; }}
.line-table {{ min-width:1040px; }}
th {{ text-align:left; background:#f3f6fa; color:#50627a; font-weight:600; }}
th,td {{ border-bottom:1px solid #e9edf3; padding:11px 13px; vertical-align:top; }}
td {{ overflow-wrap:anywhere; }}
td.num {{ font-variant-numeric:tabular-nums; white-space:nowrap; }}
.empty {{ padding:24px; text-align:center; color:var(--muted); }}
.legend {{ display:flex; gap:18px; font-size:12px; color:var(--muted); margin-bottom:16px; flex-wrap:wrap; }}
.dot {{ display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; }}
.red {{ background:#ce4956; }} .blue {{ background:#3a79cd; }} .dark {{ background:#263247; }}
figure {{ margin:0; border:1px solid var(--line); border-radius:10px; overflow:hidden; background:#f3f5f8; }}
figcaption {{ padding:11px 16px; font-weight:600; color:#495e78; background:#fafbfd; border-bottom:1px solid var(--line); }}
.paper {{ padding:16px; }}
.paper-image {{ position:relative; display:block; width:100%; overflow:hidden; background:#fff; box-shadow:0 3px 12px #25344712; }}
.paper img {{ display:block; width:100%; height:auto; background:#fff; }}
.roi-box {{ position:absolute; display:block; pointer-events:none; border:2px dashed #078774; box-shadow:inset 0 0 0 1px #ffffffbf; }}
.roi-legend {{ display:inline-block; width:16px; height:10px; margin-right:6px; border:2px dashed #078774; vertical-align:middle; }}
.method {{ color:#596c84; font-size:12px; }}
.method p+p {{ margin-top:8px; }}
details {{ margin-top:20px; border:1px solid var(--line); background:#fff; border-radius:10px; padding:15px 20px; }}
summary {{ cursor:pointer; font-weight:600; color:#526983; }}
pre {{ overflow:auto; max-height:600px; font:11px/1.7 Consolas,monospace; white-space:pre-wrap; overflow-wrap:anywhere; }}
footer {{ padding-top:24px; text-align:center; color:#8995a5; font-size:11px; }}
@media(max-width:850px) {{ main {{ padding:24px 16px; }} .metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .files,.figures {{ grid-template-columns:1fr; }} h1 {{ font-size:28px; }} }}
@media print {{ body {{ background:#fff; }} main {{ max-width:none; padding:0; }} .sheet,.metric,.file-card,figure {{ break-inside:avoid; }} .figures {{ grid-template-columns:1fr; }} .paper-image {{ max-width:160mm; margin:auto; box-shadow:none; }} .table-wrap {{ overflow:visible; }} table,.line-table {{ min-width:0; }} .line-table {{ font-size:10px; }} details {{ display:none; }} }}
</style>
</head>
<body><main>
<header><div class="eyebrow">PDF LAYOUT DIFF · 比较记录</div>
<h1>PDF 比较报告</h1>
<p class="subtle">记录两份 PDF 的视觉差异、文字位置和排版变化，便于逐项核查。</p>
<span class="tag">{_text(settings.get('dpi', '—'))} DPI</span>
<span class="tag">墨迹阈值 {_text(settings.get('ink_threshold', '—'))}</span>
<span class="tag">{_text(region_text)}</span>
<p class="subtle" style="font-size:11px;margin-top:12px">生成时间：{_text(generated_at)}</p></header>
<div class="files">{_file_card('reference', report)}{_file_card('candidate', report)}</div>
<div class="metrics">{cards}</div>
{warning_html}
{diagnostics_html}
<section class="sheet"><h2>测量明细</h2>{_table(['指标', '测量值', '说明'], rows, numeric_columns={1})}</section>
<section class="sheet"><h2>逐行位置与字号</h2>
<p class="subtle" style="font-size:12px;margin-bottom:14px">Δx、Δy 为待比较减参考，单位 mm；正值分别表示向右、向下。字号单位为 PDF pt。数值仅适用于已匹配的文字。</p>
{_table(['#', '参考文本', '待比较文本', 'Δx (mm)', 'Δy (mm)', '参考字号', '待比较字号'], line_rows, numeric_columns={0, 3, 4, 5, 6})}</section>
<section class="sheet"><h2>页面与差异</h2>
<div class="legend"><span><i class="dot red"></i>参考内容</span><span><i class="dot blue"></i>待比较内容</span><span><i class="dot dark"></i>叠加重合内容</span>{'<span><i class="roi-legend"></i>测量区域</span>' if marker else ''}</div>
<p class="subtle" style="font-size:12px;margin-bottom:16px">图片保留整页，{'绿色虚线框为测量区域。' if marker else '当前为全页测量。'}具体范围见上方区域坐标。未进行内容配准或自动拉伸；PNG 原图不含区域标记。</p>
<div class="figures">{''.join(figures)}</div></section>
<section class="sheet method"><h2>方法与解释边界</h2>
<p>坐标以旋转后可见页（CropBox）左上角为原点，两页按相同物理尺度放入公共白色画布。页面大小与裁切不同应单独检查；可见内容距边缘的距离不能直接还原源文档的页面或段落设置。</p>
<p>墨迹不重合率受 DPI、抗锯齿和二值化阈值影响，不能解释为整体一致程度的百分比。轮廓距离可能匹配到邻近的不同字符，仅作视觉辅助。原生文字坐标的显示小数位不代表同等实际准确度。</p>
<p>文字测量不运行 OCR。未匹配内容、重复文字、提取顺序异常、断行差异和字体替换都可能影响定位可信度。空值表示无法测量，不能当作零误差；字体边界框高度也不等于字号。</p>
<p>选区按字符边界框中心选择文字；像素范围左上角向下取整、右下角向上取整，实际像素范围保存在 JSON 的 effective_region_px 中。</p>
</section>
<details><summary>完整测量数据与版本信息（JSON）</summary><pre>{escape(report_json)}</pre></details>
<footer>PDF Layout Diff · 本报告包含内嵌图片，可离线保存 · 原始 PDF 未包含在报告中</footer>
</main></body></html>'''


def build_bundle(report: dict[str, Any], images: dict[str, bytes]) -> bytes:
    """打包原始测量 JSON、离线 HTML 与四张图片，不打包源 PDF。"""
    stream = BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("report.json", json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))
        archive.writestr("report.html", build_html(report, images).encode("utf-8"))
        for key in IMAGE_LABELS:
            if key in images:
                archive.writestr(f"{key}.png", images[key])
    return stream.getvalue()
