"""在内存中生成已知误差样页，供首次使用时验证交互和测量。"""

from __future__ import annotations

import pymupdf


PT_PER_MM = 72 / 25.4


def _point(x_mm: float, y_mm: float) -> pymupdf.Point:
    return pymupdf.Point(x_mm * PT_PER_MM, y_mm * PT_PER_MM)


def _write(page: pymupdf.Page, x: float, y: float, text: str, size: float = 12, font: str = "china-s") -> None:
    page.insert_text(_point(x, y), text, fontsize=size, fontname=font, color=(0.12, 0.16, 0.22))


def _first_page(document: pymupdf.Document, candidate: bool) -> None:
    page = document.new_page(width=210 * PT_PER_MM, height=297 * PT_PER_MM)
    dx, dy = (1.0, 0.5) if candidate else (0.0, 0.0)
    _write(page, 28 + dx, 19 + dy, "PDF Layout Diff · 演示样页", 9)
    _write(page, 28 + dx, 39 + dy, "文档版本比较", 22)
    _write(page, 28 + dx, 49 + dy, "Document version comparison", 11, "helv")
    page.draw_line(_point(28 + dx, 56 + dy), _point(182 + dx, 56 + dy), color=(0.3, 0.4, 0.52), width=0.6)
    _write(page, 28 + dx, 72 + dy, "一、文档内容与布局", 15)
    lines = [
        "文档版本更新时需要保持清晰且一致的视觉规范。",
        "本页用于观察文字位置、行距和内容边界的关系。",
        "两份文档采用相同文本，便于测量字符实际位置。",
        "参考内容与待比较内容分别显示为红色和蓝色。",
        "文字重合时颜色加深，细小错位会形成边缘重影。",
        "逐行增加的纵向误差可以帮助定位行距设置问题。",
        "页面的整体位移应与逐行累积的偏差分别分析。",
        "框选正文区域能够减少标题与页脚对统计的影响。",
        "测量结果用于辅助核查，请同时复核实际显示效果。",
    ]
    for index, line in enumerate(lines):
        _write(page, 28 + dx, 88 + dy + index * (7.5 + (0.12 if candidate else 0)), line, 12)
    _write(page, 28 + dx, 179 + dy, "二、如何使用这个示例", 15)
    _write(page, 28 + dx, 194 + dy, "第一步：使用红蓝叠加观察整体位移与逐行偏差。", 11)
    _write(page, 28 + dx, 202 + dy, "第二步：框选上方正文后重新比较，检查行距差。", 11)
    _write(page, 28 + dx, 210 + dy, "第三步：切换第二页，观察字号与内容变化。", 11)
    _write(page, 28 + dx, 277 + dy, "人工生成的原生 PDF 演示，包含预设的页面差异。", 8)
    _write(page, 179 + dx, 277 + dy, "1", 9, "helv")


def _second_page(document: pymupdf.Document, candidate: bool) -> None:
    page = document.new_page(width=210 * PT_PER_MM, height=297 * PT_PER_MM)
    _write(page, 28, 19, "PDF Layout Diff · 演示样页", 9)
    _write(page, 28, 40, "字号、字体与内容差异", 22 if candidate else 20)
    _write(page, 28, 53, "Font and content differences", 12, "tiro" if candidate else "helv")
    _write(page, 28, 76, "三、检查可解释的排版指标", 15)
    _write(page, 28, 92, "本页包含有意设置的字号、字体和内容变化。", 12.5 if candidate else 12)
    _write(page, 28, 102, "相同内容可以匹配字符位置，但不能忽略缺字。", 12)
    _write(page, 28, 112, "这一行用于检查文字的增删变化。" if candidate else "这一行包含原始参考文本，用于检查文字的增删变化。", 12)
    _write(page, 28, 129, "表格与线条的变化主要由视觉比较显示。", 11)
    left = 29 if candidate else 28
    right = 183 if candidate else 182
    rect = pymupdf.Rect(_point(left, 140), _point(right, 181))
    page.draw_rect(rect, color=(0.3, 0.4, 0.52), width=0.9 if candidate else 0.6)
    for y in (154, 168):
        page.draw_line(_point(left, y), _point(right, y), color=(0.45, 0.5, 0.58), width=0.5)
    page.draw_line(_point(85, 140), _point(85, 181), color=(0.45, 0.5, 0.58), width=0.5)
    for y, values in ((149, ("比较对象", "建议观察")), (163, ("文字排版", "位置、字号与匹配情况")), (177, ("非文字元素", "线宽、边框与几何位置"))):
        _write(page, 33, y, values[0], 10)
        _write(page, 91, y, values[1], 10)
    _write(page, 28, 277, "人工生成的原生 PDF 演示，包含预设的页面差异。", 8)
    _write(page, 179, 277, "2", 9, "helv")


def make_demo_pair() -> tuple[bytes, bytes]:
    """返回两份双页 PDF；第一页预设整体右移 1 mm、下移 0.5 mm，正文行距加 0.12 mm。"""
    result = []
    for candidate in (False, True):
        with pymupdf.open() as document:
            document.set_metadata({"title": "PDF Layout Diff - 文档版本比较示例", "author": "PDF Layout Diff"})
            _first_page(document, candidate)
            _second_page(document, candidate)
            result.append(document.tobytes(garbage=4, deflate=True))
    return result[0], result[1]
