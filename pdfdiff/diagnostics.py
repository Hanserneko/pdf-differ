"""将可复核的测量组织为带定位和输出目标的差异记录。"""

from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
from typing import Any

import cv2
import numpy as np


POSITION_TOLERANCE_MM = 0.05
FONT_SIZE_TOLERANCE_PT = 0.05
MAX_FINDINGS = 1000
MAX_VISUAL_REGIONS = 64
MAX_CONTENT_CHARACTERS = 12000


def _bounds(chars: list[dict]) -> list[float] | None:
    if not chars:
        return None
    return [min(char["bbox_mm"][0] for char in chars),
            min(char["bbox_mm"][1] for char in chars),
            max(char["bbox_mm"][2] for char in chars),
            max(char["bbox_mm"][3] for char in chars)]


def _font_name(value: str) -> str:
    prefix, separator, rest = value.partition("+")
    return rest if separator and len(prefix) == 6 and prefix.isalpha() and prefix.isupper() else value


def _location(report: dict, side: str, chars: list[dict], **extra) -> dict:
    return {"page": report[side]["page"], "text": "".join(char["c"] for char in chars),
            "bbox_mm": _bounds(chars), **extra}


def _measurement(reference: Any, candidate: Any, unit: str, method: str | None = None) -> dict:
    delta = candidate - reference if isinstance(reference, (int, float)) and isinstance(candidate, (int, float)) else None
    if (isinstance(reference, list) and isinstance(candidate, list) and len(reference) == len(candidate)
            and all(type(value) in (int, float) for value in reference + candidate)):
        delta = [b - a for a, b in zip(reference, candidate)]
        method = method or "按相同坐标分量逐项相减"
    result = {"reference": reference, "candidate": candidate, "delta": delta, "unit": unit}
    if method:
        result["method"] = method
    return result


def _add(state: dict, kind: str, scope: str, reference: dict, candidate: dict,
         observation: str, measurements: dict, description: str, *, reliable: bool = False,
         reasons: list[str] | None = None, evidence: dict | None = None,
         adjustment: dict | None = None, related: list[str] | None = None) -> str:
    state["total"] += 1
    state["by_type"][kind] += 1
    key = f"{state['prefix']}-d{state['total']:05d}"
    if len(state["findings"]) >= MAX_FINDINGS:
        return key
    target = {"description": description, "requires_source_mapping": True}
    if adjustment is not None:
        target["candidate_adjustment"] = adjustment
    finding = {
        "id": key, "type": kind, "scope": scope,
        "reference": reference, "candidate": candidate, "observation": observation,
        "measurements": measurements, "target": target,
        "reliability": {"level": "high" if reliable else "review", "reasons": reasons or [], "evidence": evidence or {}},
        "source_location": None,
    }
    if related:
        finding["related_findings"] = related
    state["findings"].append(finding)
    return key


def _geometry(report: dict, state: dict):
    measurements = {}
    for name, unit in (("width_mm", "mm"), ("height_mm", "mm"), ("rotation", "degree"),
                       ("cropbox_pt", "PDF pt"), ("mediabox_pt", "PDF pt")):
        a, b = report["reference"][name], report["candidate"][name]
        if a != b:
            measurements[name] = _measurement(a, b, unit)
    if measurements:
        _add(state, "page.geometry", "page",
             {"page": report["reference"]["page"], "text": "", "bbox_mm": None},
             {"page": report["candidate"]["page"], "text": "", "bbox_mm": None},
             "两侧页面尺寸、旋转或页面框存在差异。", measurements,
             "使待比较页的尺寸、页面框和显示方向与参考页一致，再复核内容坐标。",
             reliable=True, reasons=["直接读取 PDF 页面几何信息。"])


def _reading_risks(lines: list[dict]) -> list[str]:
    reasons = []
    if any(not line["horizontal"] for line in lines):
        reasons.append("存在非水平文字，需复核提取顺序与对应关系。")
    if any("\ufffd" in line["text"] for line in lines):
        reasons.append("部分提取文字没有可靠的 Unicode 映射。")
    if any(not char.get("visible", True) for line in lines for char in line["chars"]):
        reasons.append("提取文字包含明确未绘制或完全透明的文字层，需复核可见图像后确定调整。")
    if any(abs(a["y_mm"] - b["y_mm"]) < 0.5 for a, b in zip(lines, lines[1:])):
        reasons.append("同一高度存在多行或文本片段，阅读顺序可能有歧义。")
    return reasons


def _anchor_occurrences(text: str, anchor: str) -> int:
    if not anchor:
        return 0
    count = 0
    index = text.find(anchor)
    while index >= 0:
        count += 1
        index = text.find(anchor, index + 1)
    return count


def _content(report: dict, context: dict, state: dict, risks: list[str]) -> dict:
    chars_a = [char for line in context["reference_lines"] for char in line["raw_chars"]]
    chars_b = [char for line in context["candidate_lines"] for char in line["raw_chars"]]
    coverage = {"reference_extracted_characters_including_spaces": len(chars_a),
                "candidate_extracted_characters_including_spaces": len(chars_b),
                "content_comparison": "completed"}
    a, b = "".join(char["c"] for char in chars_a), "".join(char["c"] for char in chars_b)
    coverage["extracted_content_equal"] = a == b
    if not chars_a or not chars_b:
        coverage["content_comparison"] = "one_sided_extraction" if chars_a or chars_b else "no_extractable_text"
    if max(len(chars_a), len(chars_b)) > MAX_CONTENT_CHARACTERS:
        coverage["content_comparison"] = "character_limit"
        return coverage
    if a == b:
        return coverage
    matcher = SequenceMatcher(a=a, b=b, autojunk=False)
    ratio = matcher.ratio()
    frequencies = (Counter(line["text"] for line in context["reference_lines"]),
                   Counter(line["text"] for line in context["candidate_lines"]))
    for operation, i0, i1, j0, j1 in matcher.get_opcodes():
        if operation == "equal":
            continue
        left = _location(report, "reference", chars_a[i0:i1],
                         context_before=a[max(0, i0 - 24):i0], context_after=a[i1:i1 + 24],
                         character_range=[i0, i1])
        right = _location(report, "candidate", chars_b[j0:j1],
                          context_before=b[max(0, j0 - 24):j0], context_after=b[j1:j1 + 24],
                          character_range=[j0, j1])
        # 空侧没有内容坐标，用相邻文字作插入锚点，避免捏造边界框。
        for location, chars, start, end in ((left, chars_a, i0, i1), (right, chars_b, j0, j1)):
            location["anchor_bbox_mm"] = _bounds(chars[max(0, start - 1):min(len(chars), end + 1)])
        labels = {"delete": "待比较侧缺少参考侧的提取文字。", "insert": "待比较侧增加了提取文字。",
                  "replace": "两侧提取文字存在替换。"}
        evidence = {"operation": operation, "content_match_ratio": ratio,
                    "whitespace_preserved": True, "line_separators_ignored": True}
        reasons = list(risks)
        region_selected = report["settings"]["region_mm"] is not None
        if region_selected:
            reasons.append("选区按两侧字符中心分别筛选，边界处的位置变化可能造成单侧文字入选。")
        ambiguous = False
        for side, text, chars, start, end, frequency in (
            ("reference", a, chars_a, i0, i1, frequencies[0]),
            ("candidate", b, chars_b, j0, j1, frequencies[1]),
        ):
            anchor = text[max(0, start - 24):min(len(text), end + 24)]
            occurrences = _anchor_occurrences(text, anchor)
            involved = {char["line_id"] for char in chars[max(0, start - 1):min(len(chars), max(end, start + 1))]}
            repeated = any(frequency[context[f"{side}_lines"][line_id]["text"]] > 1 for line_id in involved)
            evidence[f"{side}_anchor_occurrences"] = occurrences
            evidence[f"{side}_repeated_line_ambiguity"] = repeated
            ambiguous |= occurrences != 1 or repeated
        if ambiguous:
            reasons.append("变化片段所在行或上下文锚点重复，具体增删实例的定位存在歧义。")
        if not chars_a or not chars_b:
            reasons.append("一侧没有可提取文字，需结合图像区分内容变化与提取能力差异。")
        if ratio < 0.9:
            reasons.append("整体提取文字差异较多，需复核内容移动或阅读顺序变化。")
        if not reasons:
            reasons.append("相邻上下文按提取顺序匹配，保留空格、标点与大小写。")
        _add(state, "text.content", "text_span", left, right, labels[operation],
             {"extracted_text": _measurement(left["text"], right["text"], "text")},
             "核对上下文，使待比较侧的文字内容与参考片段一致；先复核内容移动和提取顺序。",
             reliable=not risks and not ambiguous and not region_selected and ratio >= 0.9 and bool(chars_a) and bool(chars_b),
             reasons=reasons, evidence=evidence)
    return coverage


def _line_reliability(report: dict, context: dict, a: int, b: int, pairs: list,
                      risks: list[str], frequencies: tuple[Counter, Counter]) -> tuple[list[str], dict, bool]:
    line_a, line_b = context["reference_lines"][a], context["candidate_lines"][b]
    chars_a, chars_b = context["reference_chars"], context["candidate_chars"]
    offsets = np.asarray([[chars_b[j][axis] - chars_a[i][axis] for axis in ("x_mm", "y_mm")] for i, j in pairs])
    median = np.median(offsets, axis=0)
    residual = np.linalg.norm(offsets - median, axis=1)
    p95 = float(np.percentile(residual, 95))
    absolute = np.linalg.norm(offsets, axis=1)
    evidence = {"matched_characters": len(pairs), "reference_line": a + 1, "candidate_line": b + 1,
                "reference_match_coverage": len(pairs) / len(line_a["chars"]),
                "candidate_match_coverage": len(pairs) / len(line_b["chars"]),
                "displacement_residual_p95_mm": p95,
                "displacement_residual_max_mm": float(np.max(residual)),
                "absolute_displacement_p95_mm": float(np.percentile(absolute, 95)),
                "absolute_displacement_max_mm": float(np.max(absolute)),
                "dx_range_mm": [float(np.min(offsets[:, 0])), float(np.max(offsets[:, 0]))],
                "dy_range_mm": [float(np.min(offsets[:, 1])), float(np.max(offsets[:, 1]))],
                "median_dx_mm": float(median[0]), "median_dy_mm": float(median[1])}
    reasons = list(risks)
    if frequencies[0][line_a["text"]] > 1 or frequencies[1][line_b["text"]] > 1:
        reasons.append("整行文本重复出现，按顺序匹配可能对应到不同实例。")
    if line_a["text"] != line_b["text"] or len(pairs) != len(line_a["chars"]) or len(pairs) != len(line_b["chars"]):
        reasons.append("本行仅部分字符对应，位置统计只覆盖共同字符。")
    if "".join(char["c"] for char in line_a["raw_chars"]) != "".join(char["c"] for char in line_b["raw_chars"]):
        reasons.append("本行的空格或文字内容发生变化。")
    if report["text"]["line_break_change_count"]:
        reasons.append("检测到换行关系变化，需复核行对应关系。")
    if (report["text"].get("match_ratio") or 0) < 0.9:
        reasons.append("整页共同文字覆盖不足，需复核匹配。")
    uniform = float(np.max(residual)) <= POSITION_TOLERANCE_MM
    if not uniform:
        reasons.append("匹配字符的位移不一致，单次整行平移不能解释本行差异。")
    if not reasons:
        reasons.append("整行文字唯一且完整对应，字符位移近似一致。")
    return reasons, evidence, not risks and len(reasons) == 1 and reasons[0].startswith("整行文字唯一")


def _local_reading_risks(lines: list[dict], index: int) -> list[str]:
    reasons = _reading_risks([lines[index]])
    neighbors = lines[max(0, index - 1):index] + lines[index + 1:index + 2]
    if any(abs(line["y_mm"] - lines[index]["y_mm"]) < 0.5 for line in neighbors):
        reasons.append("该行同一高度还有其他文本片段，需复核对应关系。")
    return reasons


def _style_runs(pairs: list, chars_a: list[dict], chars_b: list[dict], kind: str) -> list[list]:
    runs = []
    for i, j in pairs:
        if kind == "text.font_size":
            changed = abs(chars_b[j]["size_pt"] - chars_a[i]["size_pt"]) > FONT_SIZE_TOLERANCE_PT
            signature = (round(chars_a[i]["size_pt"], 4), round(chars_b[j]["size_pt"], 4))
        else:
            signature = (_font_name(chars_a[i]["font"]), _font_name(chars_b[j]["font"]))
            changed = signature[0] != signature[1]
        if not changed:
            continue
        if runs and runs[-1][0] == signature and i == runs[-1][1][-1][0] + 1 and j == runs[-1][1][-1][1] + 1:
            runs[-1][1].append((i, j))
        else:
            runs.append((signature, [(i, j)]))
    return runs


def _style_location(report: dict, side: str, selected: list[dict], line: dict) -> dict:
    raw = line["raw_chars"]
    first, last = selected[0], selected[-1]
    # 原始提取字符保留空格；非空白匹配记录与原始记录共用字符原点。
    start = next(index for index, char in enumerate(raw)
                 if char["x_mm"] == first["x_mm"] and char["y_mm"] == first["y_mm"] and char["c"] == first["c"][0])
    end = next(index for index in range(len(raw) - 1, start - 1, -1)
               if raw[index]["x_mm"] == last["x_mm"] and raw[index]["y_mm"] == last["y_mm"] and raw[index]["c"] == last["c"][-1])
    return _location(report, side, raw[start:end + 1],
                     context_before="".join(char["c"] for char in raw[max(0, start - 24):start]),
                     context_after="".join(char["c"] for char in raw[end + 1:end + 25]))


def _lines(report: dict, context: dict, state: dict, risks: list[str]) -> list[dict]:
    chars_a, chars_b = context["reference_chars"], context["candidate_chars"]
    lines_a, lines_b = context["reference_lines"], context["candidate_lines"]
    frequencies = (Counter(line["text"] for line in lines_a), Counter(line["text"] for line in lines_b))
    records = []
    for a, b, pairs in context["line_pairs"]:
        line_a, line_b = lines_a[a], lines_b[b]
        left, right = _location(report, "reference", line_a["raw_chars"]), _location(report, "candidate", line_b["raw_chars"])
        local_risks = list(dict.fromkeys(risks + _local_reading_risks(lines_a, a) + _local_reading_risks(lines_b, b)))
        reasons, evidence, reliable = _line_reliability(report, context, a, b, pairs, local_risks, frequencies)
        dx, dy = evidence["median_dx_mm"], evidence["median_dy_mm"]
        record = {"a": a, "b": b, "left": left, "right": right, "reliable": reliable,
                  "dx": dx, "dy": dy, "related": [], "evidence": evidence}
        if evidence["absolute_displacement_max_mm"] > POSITION_TOLERANCE_MM:
            movement = {"dx_mm": -dx, "dy_mm": -dy} if reliable else None
            target = ("使该行匹配字符回到参考位置；整体位移的几何修正量见 candidate_adjustment。" if reliable
                      else "复核该行共同字符与上下文，使字符坐标接近参考位置；结合内容、字形与换行变化确定修改范围。")
            key = _add(state, "text.position", "matched_line", left, right,
                       f"匹配字符横向位移中位数 {dx:+.3f} mm，纵向位移中位数 {dy:+.3f} mm。",
                       {"dx_mm": _measurement(0, dx, "mm", "匹配字符位移的中位数"),
                        "dy_mm": _measurement(0, dy, "mm", "匹配字符位移的中位数"),
                        "position_error_p95_mm": _measurement(0, evidence["absolute_displacement_p95_mm"], "mm", "匹配字符二维位移长度的第95百分位"),
                        "position_error_max_mm": _measurement(0, evidence["absolute_displacement_max_mm"], "mm", "匹配字符二维位移长度的最大值")}, target,
                       reliable=reliable, reasons=reasons, evidence=evidence, adjustment=movement)
            record["related"].append(key)
        for kind in ("text.font_size", "text.font_name"):
            for signature, run in _style_runs(pairs, chars_a, chars_b, kind):
                part_a, part_b = [chars_a[i] for i, _ in run], [chars_b[j] for _, j in run]
                reference = _style_location(report, "reference", part_a, line_a)
                candidate = _style_location(report, "candidate", part_b, line_b)
                if kind == "text.font_size":
                    measurement = {"font_size_pt": _measurement(signature[0], signature[1], "PDF pt")}
                    description = "使此匹配文字片段的提取字号达到参考值，并重新检查换行和位置。"
                    observation = f"匹配文字片段的提取字号由 {signature[0]:.3f} 变为 {signature[1]:.3f} PDF pt。"
                else:
                    measurement = {"font_name": _measurement(signature[0], signature[1], "font name")}
                    description = "核对字体资源与字形，使此文字片段的输出与参考一致，并复核字体替换后的尺寸。"
                    observation = "匹配文字片段的 PDF 字体名称发生变化。"
                _add(state, kind, "matched_text_span", reference, candidate, observation, measurement, description,
                     reliable=reliable, reasons=reasons, evidence={**evidence, "affected_characters": len(run)})
        records.append(record)
    return records


def _stable_neighbor(previous: dict, current: dict, context: dict) -> bool:
    if not previous["reliable"] or not current["reliable"]:
        return False
    if current["a"] != previous["a"] + 1 or current["b"] != previous["b"] + 1:
        return False
    four = [context[side][record[key]] for side, key in (("reference_lines", "a"), ("candidate_lines", "b"))
            for record in (previous, current)]
    sizes = [char["size_pt"] for line in four for char in line["chars"]]
    if max(sizes) - min(sizes) > FONT_SIZE_TOLERANCE_PT:
        return False
    fonts = {_font_name(char["font"]) for line in four for char in line["chars"]}
    if len(fonts) != 1 or not all(line["horizontal"] for line in four):
        return False
    for side, key in (("reference_lines", "a"), ("candidate_lines", "b")):
        first, second = context[side][previous[key]], context[side][current[key]]
        gap = second["y_mm"] - first["y_mm"]
        size_mm = first["size_pt"] * 25.4 / 72
        if not size_mm * 0.5 <= gap <= size_mm * 3:
            return False
        box_a, box_b = _bounds(first["chars"]), _bounds(second["chars"])
        overlap = min(box_a[2], box_b[2]) - max(box_a[0], box_b[0])
        if overlap <= 0.2 * min(box_a[2] - box_a[0], box_b[2] - box_b[0]):
            return False
    return True


def _patterns(report: dict, context: dict, records: list[dict], state: dict):
    groups = []
    for record in records:
        if groups and _stable_neighbor(groups[-1][-1], record, context):
            groups[-1].append(record)
        else:
            groups.append([record])
    for group in groups:
        for previous, current in zip(group, group[1:]):
            gaps = []
            for side, key in (("reference_lines", "a"), ("candidate_lines", "b")):
                gaps.append(context[side][current[key]]["y_mm"] - context[side][previous[key]]["y_mm"])
            if abs(gaps[1] - gaps[0]) <= POSITION_TOLERANCE_MM:
                continue
            reference = _location(report, "reference", [char for item in (previous, current) for char in context["reference_lines"][item["a"]]["raw_chars"]])
            candidate = _location(report, "candidate", [char for item in (previous, current) for char in context["candidate_lines"][item["b"]]["raw_chars"]])
            _add(state, "text.line_spacing", "adjacent_line_pair", reference, candidate,
                 "这两个相邻匹配行的基线间距发生变化。",
                 {"baseline_spacing_mm": _measurement(gaps[0], gaps[1], "mm", "相邻行基线纵坐标之差")},
                 "使这两个相邻行的基线间距达到参考值，同时复核起始位置和后续各行。",
                 reliable=True, reasons=["相邻行唯一完整对应，字体字号稳定，行内位移近似一致。"],
                 evidence={"sample_gaps": 1, "reference_lines": [previous["a"] + 1, current["a"] + 1],
                           "candidate_lines": [previous["b"] + 1, current["b"] + 1]})
        if len(group) < 3:
            continue
        index = np.arange(len(group), dtype=float)
        values = np.asarray([[record["dx"], record["dy"]] for record in group])
        slopes, intercepts = np.polyfit(index, values, 1)
        residual = np.linalg.norm(values - (intercepts + index[:, None] * slopes), axis=1)
        error = float(np.percentile(residual, 95))
        if error > POSITION_TOLERANCE_MM or np.max(np.abs(values)) <= POSITION_TOLERANCE_MM:
            continue
        reference = _location(report, "reference", [char for item in group for char in context["reference_lines"][item["a"]]["raw_chars"]])
        candidate = _location(report, "candidate", [char for item in group for char in context["candidate_lines"][item["b"]]["raw_chars"]])
        measurements = {
            "initial_dx_mm": _measurement(0, float(intercepts[0]), "mm", "相邻行组位移随行序号的最小二乘拟合截距"),
            "initial_dy_mm": _measurement(0, float(intercepts[1]), "mm", "相邻行组位移随行序号的最小二乘拟合截距"),
            "per_line_dx_mm": _measurement(0, float(slopes[0]), "mm/line", "相邻行组位移的拟合斜率"),
            "per_line_dy_mm": _measurement(0, float(slopes[1]), "mm/line", "相邻行组位移的拟合斜率"),
        }
        evidence = {"sample_lines": len(group), "fit_residual_p95_mm": error,
                    "reference_lines": [record["a"] + 1 for record in group],
                    "candidate_lines": [record["b"] + 1 for record in group],
                    "line_offsets_mm": [[record["dx"], record["dy"]] for record in group]}
        _add(state, "text.line_pattern", "adjacent_line_group", reference, candidate,
             f"连续 {len(group)} 个可靠对应行存在一致的初始位移或逐行累计位移。", measurements,
             "对照该相邻行组的初始位置及逐行位移目标调整输出，并复测组内各行；组别按几何关系形成。",
             reliable=True, reasons=["至少三个连续、唯一、完整匹配行，字号字体稳定且行内位移一致。", "拟合残差低于位置筛选阈值。"],
             evidence=evidence, related=[key for record in group for key in record["related"]],
             adjustment={"dx_mm": -float(intercepts[0]), "dy_mm": -float(intercepts[1]),
                         "per_line_dx_mm": -float(slopes[0]), "per_line_dy_mm": -float(slopes[1])})


def _reflow(report: dict, context: dict, state: dict):
    count = report["text"].get("line_break_change_count")
    if not count:
        return
    chars_a, chars_b = context["reference_chars"], context["candidate_chars"]
    pairs = context.get("pairs", [])
    for (i0, j0), (i1, j1) in zip(pairs, pairs[1:]):
        if i1 != i0 + 1 or j1 != j0 + 1:
            continue
        break_a = chars_a[i0]["line_id"] != chars_a[i1]["line_id"]
        break_b = chars_b[j0]["line_id"] != chars_b[j1]["line_id"]
        if break_a == break_b:
            continue
        reference = _location(report, "reference", chars_a[max(0, i0 - 12):i1 + 13])
        candidate = _location(report, "candidate", chars_b[max(0, j0 - 12):j1 + 13])
        _add(state, "text.reflow", "matched_character_boundary", reference, candidate,
             f"共同字符 {chars_a[i0]['c']!r} 与 {chars_a[i1]['c']!r} 之间的换行关系变化。",
             {"line_break": _measurement(int(break_a), int(break_b), "boolean")},
             "对照此字符边界的行分布恢复参考换行，再复测位置、字号和行距。",
             reasons=["换行关系基于提取行与共同字符，需复核分栏、内容变化和提取顺序。"],
             evidence={"reference_line_before": chars_a[i0]["line_id"] + 1,
                       "reference_line_after": chars_a[i1]["line_id"] + 1,
                       "candidate_line_before": chars_b[j0]["line_id"] + 1,
                       "candidate_line_after": chars_b[j1]["line_id"] + 1})


def _visual_regions(report: dict, delta: np.ndarray, origin: tuple[int, int], state: dict) -> dict:
    total_changed = report["visual"]["rgb_changed_pixels"]
    if not total_changed:
        return {"visual_regions_total": 0, "visual_regions_returned": 0, "omitted_visual_changed_pixels": 0}
    # 小范围膨胀把相邻像素差异归为定位区域，所有原始变化像素仍参与计数。
    radius = max(1, round(report["settings"]["dpi"] * 0.35 / 25.4))
    mask = (delta > 0).astype(np.uint8)
    cv2.dilate(mask, np.ones((2 * radius + 1, 2 * radius + 1), dtype=np.uint8), dst=mask)
    number, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    counts = np.zeros(number, dtype=np.int64)
    for row in range(0, len(delta), 256):
        changed = delta[row:row + 256] > 0
        counts += np.bincount(labels[row:row + 256][changed], minlength=number)
    del labels, mask
    order = sorted(range(1, number), key=lambda label: (-counts[label], stats[label, 1], stats[label, 0]))[:MAX_VISUAL_REGIONS]
    order.sort(key=lambda label: (stats[label, 1], stats[label, 0]))
    returned = covered = 0
    scale = 25.4 / report["settings"]["dpi"]
    for label in order:
        x, y, width, height = [int(value) for value in stats[label, :4]]
        crop = [x + origin[0], y + origin[1], x + width + origin[0], y + height + origin[1]]
        bbox = [value * scale for value in crop]
        if len(state["findings"]) < MAX_FINDINGS:
            returned += 1
            covered += int(counts[label])
        _add(state, "visual.region", "pixel_region",
             {"page": report["reference"]["page"], "text": "", "bbox_mm": bbox},
             {"page": report["candidate"]["page"], "text": "", "bbox_mm": bbox},
             f"此区域有 {int(counts[label])} 个 RGB 变化像素。",
             {"changed_pixels": _measurement(0, int(counts[label]), "pixel")},
             "对照此区域的两侧原图和差异图，使待比较侧输出接近参考；结合文字诊断判断关联变化。",
             reliable=True, reasons=["直接比较相同物理尺度下的渲染 RGB 像素。", "区域由相邻像素差异合并形成，可包含文字、图形或图片。"],
             evidence={"crop_px": crop, "image_keys": ["reference", "candidate", "overlay", "diff"],
                       "changed_pixels": int(counts[label]), "merge_radius_mm": radius * scale})
    omitted = number - 1 - len(order)
    state["total"] += omitted
    state["by_type"]["visual.region"] += omitted
    return {"visual_regions_total": number - 1, "visual_regions_returned": returned,
            "omitted_visual_changed_pixels": total_changed - covered,
            "visual_regions_truncated": number - 1 > returned}


def build_diagnostics(report: dict, context: dict, delta: np.ndarray, origin: tuple[int, int]) -> dict:
    """保留原测量，另外生成有界、确定性的差异清单。"""
    state = {"findings": [], "total": 0, "by_type": Counter(),
             "prefix": f"r{report['reference']['page']}-c{report['candidate']['page']}"}
    risks = list(dict.fromkeys(_reading_risks(context["reference_lines"]) + _reading_risks(context["candidate_lines"])))
    _geometry(report, state)
    coverage = _content(report, context, state, risks)
    _reflow(report, context, state)
    layout_risks = []
    if not coverage["extracted_content_equal"]:
        layout_risks.append("两侧提取内容或顺序不同，整体调整前需复核文本对应关系。")
    records = _lines(report, context, state, layout_risks)
    _patterns(report, context, records, state)
    coverage.update(_visual_regions(report, delta, origin, state))
    coverage.update({"matched_characters": report["text"]["matched_characters"],
                     "text_match_ratio": report["text"]["match_ratio"],
                     "position_measurement_available": report["text"]["position_measurement_available"],
                     "matched_lines": len(records), "region_mm": report["settings"]["region_mm"]})
    limitations = [
        "观测与调整目标描述 PDF 输出；源代码位置需要结合工程另行定位。",
        "位置差为匹配字符的统计值；整体平移目标仅用于完整唯一对应且字符位移近似一致的行。",
        "文字内容按提取顺序比较，保留空格、标点与大小写；忽略行分隔，换行另行记录。",
        "不执行 OCR；扫描图像、转曲文字和无法提取的内容只能提供视觉定位。",
        "视觉区域与文字记录可能描述同一变化，差异条目数不等于独立问题数。",
        "筛选阈值只用于组织诊断条目，原始测量继续保留；阈值与采样间距不代表实际准确度。",
    ]
    if coverage["content_comparison"] == "character_limit":
        limitations.append(f"含空格提取文字超过 {MAX_CONTENT_CHARACTERS} 字符，跳过具体内容比对。")
    if not report["text"]["position_measurement_available"]:
        limitations.append("当前没有可靠文字位置测量，结合 warnings 与视觉结果复核。")
    limitations.extend(risks)
    truncated = state["total"] > len(state["findings"]) or coverage.get("visual_regions_truncated", False)
    if truncated:
        limitations.append(f"差异条目最多保留 {MAX_FINDINGS} 条，视觉区域最多保留 {MAX_VISUAL_REGIONS} 处；覆盖与省略数量见 coverage。")
    return {
        "schema_version": 1,
        "conventions": {"origin": "visible-page-top-left", "position_unit": "mm", "font_size_unit": "PDF pt",
                        "delta": "candidate-minus-reference", "positive_x": "right", "positive_y": "down",
                        "bbox": "[x0,y0,x1,y1]", "target_adjustment": "apply-to-candidate",
                        "source_mapping": "unavailable"},
        "tolerances": {"position_mm": POSITION_TOLERANCE_MM, "font_size_pt": FONT_SIZE_TOLERANCE_PT,
                       "spacing_mm": POSITION_TOLERANCE_MM, "rgb_channel": 0,
                       "max_findings": MAX_FINDINGS, "max_visual_regions": MAX_VISUAL_REGIONS},
        "summary": {"finding_count": len(state["findings"]), "detected_finding_count": state["total"],
                    "by_type": dict(state["by_type"]), "review_count": sum(item["reliability"]["level"] == "review" for item in state["findings"])},
        "coverage": coverage, "findings": state["findings"], "limitations": limitations, "truncated": truncated,
    }
