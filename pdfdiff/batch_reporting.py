"""导出整份文档的逐页摘要，不把未完成或错误页当作一致页。"""

from __future__ import annotations

from collections import Counter
from html import escape
from io import BytesIO
import json
import math
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from .reporting import DIAGNOSTIC_STYLES, _diagnostics_html


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return escape(str(value), quote=True)


def _integer(value: Any, minimum: int = 0) -> int | None:
    return value if type(value) is int and minimum <= value <= 1_000_000 else None


def _number(value: Any, digits: int = 3, *, signed: bool = False) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return "不可测"
    if abs(value) < 0.5 * 10 ** -digits:
        value = 0
    return format(value, f"{'+' if signed else ''}.{digits}f")


def _count(value: Any) -> str:
    return f"{value:,}" if type(value) is int and value >= 0 else "不可用"


def _measurement_count(value: Any) -> str:
    return _count(value) if type(value) is int and value >= 0 else "不可测"


def _percentage(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1:
        return "不可测"
    return f"{value * 100:.2f} %"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def _page_number(value: Any, total: int | None) -> int | None:
    page = _integer(value, 1)
    return page if page is not None and (total is None or page <= total) else None


def _records(report: dict) -> tuple[list[tuple[int, Any, int | None, bool]], int | None]:
    total = _integer(report.get("total_pages"))
    source = report.get("pages")
    entries = source if isinstance(source, list) else []
    numbers = [_page_number(_mapping(entry).get("page"), total) for entry in entries]
    frequencies = Counter(number for number in numbers if number is not None)
    records = [
        (index, entry, number, number is not None and frequencies[number] == 1)
        for index, (entry, number) in enumerate(zip(entries, numbers), start=1)
    ]
    return records, total


def _statistics(records: list[tuple], total: int | None) -> dict:
    compared = changed = unchanged = errors = unknown = 0
    for _, raw, _, accepted in records:
        if not accepted:
            continue
        entry = _mapping(raw)
        if entry.get("status") == "error":
            errors += 1
        elif entry.get("status") == "done":
            compared += 1
            flag = entry.get("has_visual_difference")
            if flag is True:
                changed += 1
            elif flag is False:
                unchanged += 1
            else:
                unknown += 1
    processed = compared + errors
    return {
        "compared_pages": compared,
        "changed_pages": changed,
        "unchanged_pages": unchanged,
        "error_pages": errors,
        "processed_pages": processed,
        "unfinished_pages": max(0, total - processed) if total is not None else None,
        "unknown_visual_pages": unknown,
    }


def _record_state(raw: Any, accepted: bool) -> tuple[str, str]:
    if not accepted:
        return "review", "记录需复核"
    entry = _mapping(raw)
    if entry.get("status") == "error":
        return "error", "处理错误"
    if entry.get("status") != "done":
        return "pending", "尚未完成"
    if entry.get("has_visual_difference") is True:
        return "changed", "有视觉差异"
    if entry.get("has_visual_difference") is False:
        return "unchanged", "无视觉差异"
    return "review", "视觉状态未知"


def _status(report: dict, stats: dict) -> tuple[str, str]:
    status = report.get("status")
    if status == "completed":
        if stats["unfinished_pages"] is None:
            return "review", "已结束，页数待核查"
        if stats["unfinished_pages"] or stats["unknown_visual_pages"]:
            return "review", "已结束，仍有待核查页"
        if stats["error_pages"]:
            return "review", "已完成，含错误页"
        return "completed", "已完成"
    return {
        "queued": ("pending", "等待开始"),
        "running": ("running", "正在比较"),
        "cancelled": ("review", "已取消"),
        "failed": ("error", "任务失败"),
    }.get(status, ("review", "状态未知"))


def _pill(kind: str, label: str) -> str:
    return f'<span class="pill {kind}">{_text(label)}</span>'


def _file(side: str, report: dict) -> str:
    file = _mapping(_mapping(report.get("files")).get(side))
    label = "参考文档" if side == "reference" else "待比较文档"
    return (
        f'<article class="file {side}"><span class="eyebrow">{label}</span>'
        f'<h3>{_text(file.get("filename", "未提供文件名"))}</h3>'
        f'<p class="hash-label">SHA-256</p><code>{_text(file.get("sha256", "未提供"))}</code></article>'
    )


def _metric(key: str, label: str, value: Any, description: str) -> str:
    return (
        f'<article class="metric"><p>{_text(label)}</p>'
        f'<strong data-stat="{key}">{_count(value)}</strong>'
        f'<span>{_text(description)}</span></article>'
    )


def _entry_label(index: int, page: int | None) -> str:
    return f"第 {page} 页" if page is not None else f"条目 {index}（页码无效）"


def build_batch_html(report: dict) -> str:
    """生成自包含整本摘要；计数只采用合法、唯一页码的明确结果。"""
    records, total = _records(report)
    stats = _statistics(records, total)
    state_class, state_label = _status(report, stats)
    settings = _mapping(report.get("settings"))
    engine = _mapping(report.get("engine"))
    version_tags = "".join(
        f'<span class="tag">{_text(key)} {_text(value)}</span>' for key, value in engine.items()
    ) or '<span class="tag">版本未提供</span>'
    total_label = _count(total)
    progress = min(100, stats["processed_pages"] / total * 100) if total else 0
    notices = []
    if any(not accepted for _, _, _, accepted in records):
        notices.append("存在非法或重复页码的记录，这些记录未计入页数统计；原始条目保留在下方详情与导出数据中。")
    if total is None:
        notices.append("总页数不可用，不能据此确定未完成页数。")
    if stats["unknown_visual_pages"]:
        notices.append(f'另有 {stats["unknown_visual_pages"]} 个已处理页的视觉状态未确定，不计入有视觉差异或无视觉差异。')
    declared_summary = _mapping(report.get("summary"))
    if any(key in declared_summary and declared_summary[key] != stats[key] for key in ("compared_pages", "changed_pages", "unchanged_pages", "error_pages")):
        notices.append("任务概要计数与可核查的逐页记录不一致；本报告按逐页记录显示计数，原始概要保留在完整 JSON 中。")
    declared_completed = _integer(report.get("completed_pages"))
    if declared_completed is not None and declared_completed != stats["processed_pages"]:
        notices.append("任务声明的已处理页数与有效逐页记录不一致，需结合原始数据核查。")
    if stats["unfinished_pages"]:
        notices.append(f'尚有 {stats["unfinished_pages"]} 页未完成处理，不能判断这些页是否存在视觉差异。')
    if report.get("error") is not None:
        notices.append(f'任务错误：{report["error"]}')
    notices_html = (
        '<section class="notice"><h2>任务提示</h2><ul>'
        + "".join(f"<li>{_text(item)}</li>" for item in notices)
        + "</ul></section>"
        if notices else ""
    )
    cards = "".join([
        _metric("total_pages", "总页数", total, "待比较的整份文档"),
        _metric("processed_pages", "已处理页", stats["processed_pages"], "包括已比较页和错误页"),
        _metric("changed_pages", "有视觉差异", stats["changed_pages"], "明确检测到像素或页面几何差异"),
        _metric("unchanged_pages", "无视觉差异", stats["unchanged_pages"], "只计入明确完成且无视觉差异的页"),
        _metric("error_pages", "处理错误", stats["error_pages"], "错误页没有比较结论"),
        _metric("unfinished_pages", "未完成页", stats["unfinished_pages"], "等待、取消或失败后未处理的页"),
    ])
    table_rows, details = [], []
    has_diagnostics = False
    for index, raw, page, accepted in records:
        entry = _mapping(raw)
        kind, label = _record_state(raw, accepted)
        page_report = _mapping(entry.get("report")) if accepted and entry.get("status") == "done" else {}
        visual, text = _mapping(page_report.get("visual")), _mapping(page_report.get("text"))
        # 错误或未完成记录即使带有旧数据，也不显示为已完成测量。
        cells = [
            f'<a href="#entry-{index:04d}">{_text(page if page is not None else f"条目 {index}")}</a>',
            _pill(kind, label),
            _text(_percentage(visual.get("foreground_mismatch_ratio"))),
            _text(_measurement_count(visual.get("rgb_changed_pixels"))),
            _text(_number(text.get("median_dx_mm"), signed=True)),
            _text(_number(text.get("median_dy_mm"), signed=True)),
            _text(_number(text.get("p95_position_error_mm"))),
            _text(_number(text.get("line_spacing_delta_mm"), signed=True)),
            _text(_number(text.get("font_size_delta_pt"), signed=True)),
            _text(_measurement_count(text.get("matched_characters"))),
        ]
        table_rows.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
        raw_page_report = _mapping(entry.get("report"))
        warnings = raw_page_report.get("warnings") or []
        if not isinstance(warnings, list):
            warnings = [warnings]
        warning_html = '<ul class="page-warnings">' + "".join(f"<li>{_text(item)}</li>" for item in warnings) + "</ul>" if warnings else ""
        error_html = f'<p class="page-error">处理错误：{_text(entry["error"])}</p>' if entry.get("error") is not None else ""
        diagnostics_html = _diagnostics_html(page_report, prefix=f"entry-{index:04d}-diagnostics")
        has_diagnostics |= bool(diagnostics_html)
        detail_note = "此条目保留测量摘要、差异诊断与处理状态；页面图像可在应用中查看。" if diagnostics_html else "此条目保留该页摘要与处理状态；没有页图或逐行记录。"
        details.append(
            f'<details class="page-detail" id="entry-{index:04d}"><summary>'
            f'<span>{_text(_entry_label(index, page))}</span>{_pill(kind, label)}</summary>'
            f'{error_html}{warning_html}{diagnostics_html}<p class="subtle detail-note">{detail_note}</p>'
            f'<pre>{escape(_json(raw))}</pre></details>'
        )
    if not table_rows:
        table_rows = ['<tr><td colspan="10" class="empty">尚无已处理的逐页记录。</td></tr>']
    headers = ["页码", "视觉状态", "墨迹不重合率", "RGB 变化像素", "Δx (mm)", "Δy (mm)", "位置 P95 (mm)", "行距差 (mm)", "字号差 (PDF pt)", "匹配字符"]
    header_html = "".join(f"<th>{_text(label)}</th>" for label in headers)
    details_intro = "本报告保留逐页测量摘要与差异诊断，包含定位、调整目标和可靠性依据。查看页面图像，请在应用中选择对应页。" if has_diagnostics else "本报告仅保存摘要，不包含页图或逐行详情。查看图像和逐行详情，请在应用中选择对应页。"
    footer_label = "自包含离线报告 · 不包含原始 PDF 或页图" if has_diagnostics else "自包含离线摘要 · 不包含原始 PDF、页图或逐行详情"
    return f'''<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>文档比较摘要 · PDF Layout Diff</title>
<style>
{DIAGNOSTIC_STYLES}
:root {{ color-scheme:light; --ink:#1d2b40; --muted:#66778e; --line:#dfe6ef; --blue:#265fb8; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:#edf1f6; color:var(--ink); font:14px/1.65 "Segoe UI","Microsoft YaHei",sans-serif; }}
main {{ max-width:1440px; padding:48px 40px; margin:auto; }}
h1 {{ margin:8px 0 10px; font-size:32px; line-height:1.3; letter-spacing:-.6px; }}
h2 {{ margin:0 0 15px; font-size:18px; }}
h3 {{ margin:8px 0 13px; font-size:16px; overflow-wrap:anywhere; }}
p {{ margin:5px 0; }}
.eyebrow {{ font-size:11px; letter-spacing:1.6px; font-weight:700; color:var(--blue); }}
.subtle {{ color:var(--muted); }}
header {{ border-bottom:1px solid #ccd7e4; padding-bottom:25px; margin-bottom:25px; }}
.heading {{ display:flex; align-items:flex-start; justify-content:space-between; gap:20px; }}
.pill {{ display:inline-block; padding:3px 9px; border-radius:5px; font-size:11px; font-weight:600; white-space:nowrap; background:#edf1f7; color:#617187; }}
.pill.changed,.pill.error {{ background:#fff0ef; color:#b74943; }}
.pill.unchanged,.pill.completed {{ background:#eaf5f0; color:#27785e; }}
.pill.review {{ background:#fff5dc; color:#876517; }}
.pill.running {{ background:#eaf1ff; color:#315fa8; }}
.status {{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-top:17px; font-size:12px; color:var(--muted); }}
.progress {{ width:240px; max-width:100%; height:5px; border-radius:6px; overflow:hidden; background:#dbe4f0; }}
.progress span {{ display:block; height:100%; background:#4e7bbd; width:{progress:.6f}%; }}
.metadata {{ display:flex; flex-wrap:wrap; gap:7px; margin-top:15px; }}
.tag {{ border:1px solid #ccd8e7; border-radius:5px; padding:3px 9px; color:#596c84; font-size:11px; overflow-wrap:anywhere; max-width:100%; }}
.files,.metrics {{ display:grid; gap:14px; }}
.files {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
.file,.metric,.sheet {{ background:#fff; border:1px solid var(--line); border-radius:11px; }}
.file {{ padding:19px 22px; border-top:3px solid #c94a55; }}
.file.candidate {{ border-top-color:#3c7bc8; }}
.file.reference .eyebrow {{ color:#b1424c; }}
.file.candidate .eyebrow {{ color:#386cb0; }}
.hash-label {{ color:#7b899b; font-size:10px; letter-spacing:1px; }}
.file code {{ display:block; font-size:10px; overflow-wrap:anywhere; line-height:1.6; color:#77869a; }}
.metrics {{ grid-template-columns:repeat(6,minmax(0,1fr)); margin:23px 0; }}
.metric {{ padding:17px 19px; }}
.metric p {{ color:#536981; font-size:12px; }}
.metric strong {{ display:block; font-size:28px; line-height:1.3; margin:7px 0; font-variant-numeric:tabular-nums; }}
.metric span {{ display:block; font-size:10px; color:#78879a; }}
.notice {{ border:1px solid #ead6a4; background:#fff9e9; color:#795c22; padding:20px 23px; border-radius:11px; margin-bottom:22px; }}
.notice h2 {{ font-size:15px; margin-bottom:8px; }}
.notice ul,.page-warnings {{ margin:0; padding-left:20px; }}
.notice li+li,.page-warnings li+li {{ margin-top:5px; }}
.sheet {{ padding:24px; margin-top:20px; }}
.sheet-intro {{ font-size:12px; color:var(--muted); margin-bottom:18px; }}
.table-wrap {{ overflow:auto; }}
table {{ width:100%; min-width:1160px; border-collapse:collapse; font-size:12px; }}
th {{ padding:11px 12px; text-align:left; background:#f3f6fa; color:#506780; font-weight:600; white-space:nowrap; }}
td {{ padding:12px; border-bottom:1px solid #e8edf3; white-space:nowrap; font-variant-numeric:tabular-nums; vertical-align:top; }}
td a {{ color:#275fae; text-decoration:none; font-weight:600; }}
td a:hover {{ text-decoration:underline; }}
.empty {{ text-align:center; color:var(--muted); padding:28px; }}
.page-detail {{ border:1px solid var(--line); border-radius:8px; padding:13px 16px; margin-top:11px; }}
summary {{ cursor:pointer; color:#4b6079; font-weight:600; }}
summary span:first-child {{ margin-right:12px; }}
summary:focus-visible {{ outline:2px solid #3877c5; outline-offset:4px; }}
.page-warnings {{ padding-top:15px; color:#8a661e; font-size:12px; }}
.page-error {{ font-size:12px; color:#ad4943; margin-top:15px; overflow-wrap:anywhere; }}
.detail-note {{ margin-top:14px; font-size:11px; }}
pre {{ font:11px/1.75 Consolas,monospace; white-space:pre-wrap; overflow-wrap:anywhere; overflow:auto; max-height:560px; margin:15px 0 0; }}
.method {{ font-size:12px; color:#5c7088; }}
.method p+p {{ margin-top:8px; }}
footer {{ color:#8995a6; font-size:11px; text-align:center; padding-top:25px; }}
@media(max-width:1100px) {{ .metrics {{ grid-template-columns:repeat(3,minmax(0,1fr)); }} }}
@media(max-width:650px) {{ main {{ padding:25px 16px; }} h1 {{ font-size:27px; }} .heading {{ display:block; }} .heading>.pill {{ margin-top:8px; }} .files {{ grid-template-columns:1fr; }} .metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .metric {{ padding:15px; }} .sheet {{ padding:18px; }} }}
@media print {{ body {{ background:#fff; }} main {{ max-width:none; padding:0; }} .table-wrap {{ overflow:visible; }} table {{ min-width:0; font-size:9px; }} th,td {{ padding:6px; white-space:normal; }} .file,.metric,tr {{ break-inside:avoid; }} .page-detail:not([open]) {{ display:none; }} .page-detail pre {{ max-height:none; }} }}
</style></head><body><main>
<header><div class="heading"><div><span class="eyebrow">PDF LAYOUT DIFF · DOCUMENT COMPARISON</span>
<h1>文档比较摘要</h1><p class="subtle">逐页记录视觉差异与可用的文字测量，完整保留处理状态。</p></div>{_pill(state_class, state_label)}</div>
<div class="status"><span>已处理 {_count(stats['processed_pages'])} / {total_label} 页（含错误页）</span><div class="progress" aria-hidden="true"><span></span></div></div>
<div class="metadata"><span class="tag">{_text(settings.get('dpi', '未提供'))} DPI</span><span class="tag">墨迹阈值 {_text(settings.get('ink_threshold', '未提供'))}</span><span class="tag">全页比较</span><span class="tag">生成时间 {_text(report.get('generated_at', '未提供'))}</span></div>
<div class="metadata">{version_tags}</div></header>
<div class="files">{_file('reference', report)}{_file('candidate', report)}</div>
<div class="metrics">{cards}</div>{notices_html}
<section class="sheet"><h2>逐页差异摘要</h2>
<p class="sheet-intro">“有视觉差异”依据严格像素或页面几何差异判定，不是整体相似程度的百分比。空值表示不可测，未完成页和错误页没有一致性结论。宽表可横向滚动。</p>
<div class="table-wrap"><table><thead><tr>{header_html}</tr></thead><tbody>{''.join(table_rows)}</tbody></table></div></section>
<section class="sheet"><h2>逐页记录与提示</h2><p class="sheet-intro">{details_intro}</p>
{''.join(details) if details else '<p class="empty">尚无逐页详情。</p>'}</section>
<section class="sheet method"><h2>方法与解释边界</h2>
<p>Δx、Δy、行距差和字号差均为待比较减参考；位置和行距单位 mm，字号单位 PDF pt。正 Δx 表示向右，正 Δy 表示向下；文字位置仅针对可靠匹配到的字符。</p>
<p>墨迹不重合率为二值前景异或面积除以双方前景并集面积，受 DPI、阈值与抗锯齿影响。RGB 变化像素还会反映颜色变化；页面几何变化也可单独构成视觉差异。</p>
<p>文字测量为空表示没有可用的文字定位结果，不能据此断言缺字。重复文字、内容变化和提取顺序异常可能影响匹配，应结合应用中的图像与逐行详情核查。</p>
<p>统计只采用合法、唯一页码的明确处理结果。“无视觉差异”仅表示该页在本次比较条件下完成视觉检查，不代表所有语义、隐藏内容或文件结构相同。</p></section>
<section class="sheet"><h2>参数与版本</h2><p class="sheet-intro">供复核比较条件及运行环境。</p><pre>{escape(_json({'settings': settings, 'engine': engine}))}</pre></section>
<details class="page-detail"><summary>完整任务数据（JSON）</summary><pre>{escape(_json(report))}</pre></details>
<footer>PDF Layout Diff · {footer_label}</footer>
</main></body></html>'''


def build_batch_bundle(report: dict) -> bytes:
    """打包整本摘要和所有已处理条目；页文件名不使用任意输入路径。"""
    records, _ = _records(report)
    stream = BytesIO()
    used_pages = set()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("report.json", _json(report).encode("utf-8"))
        archive.writestr("report.html", build_batch_html(report).encode("utf-8"))
        for index, entry, page, _ in records:
            if page is not None and page not in used_pages:
                filename = f"pages/page-{page:04d}.json"
                used_pages.add(page)
            else:
                # 异常或重复页码仍按枚举保留，避免覆盖及目录穿越。
                filename = f"pages/entry-{index:04d}.json"
            archive.writestr(filename, _json(entry).encode("utf-8"))
    return stream.getvalue()
