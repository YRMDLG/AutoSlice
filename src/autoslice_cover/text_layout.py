"""AutoCover 编辑器与正式 renderer 共用的文字样式和基础排版规则。

这里不依赖 Qt 的绘制 API，确保桌面画布、预览和导出都能使用同一套
默认值以及手动换行规则。具体的 glyph 测量仍由各自的绘制后端完成。
"""

from __future__ import annotations

from typing import Any, Mapping


def _default_font_family() -> str:
    """返回正式 renderer 当前默认字体族，失败时保留稳定标签。"""

    try:
        from .fonts import get_default_font_status

        return get_default_font_status().family or "AutoCover Default"
    except Exception:  # pragma: no cover - 字体运行时不可用时仍需可迁移
        return "AutoCover Default"


DEFAULT_TEXT_STYLE: dict[str, object] = {
    # headline 模板的正式默认样式：黄字、黑色粗描边和阴影。
    "font_family": _default_font_family(),
    "font_weight": 900,
    "fill_color": "#FFE438",
    "stroke_color": "#111111",
    "stroke_width": 6,
    "shadow": True,
    "line_spacing": 1.12,
}

# CoverDocument v4 之前没有记录文字样式定义的版本。缺少该字段的 v4
# 草稿只能可靠地视为“编辑器尚未提供样式编辑时自动写入的默认值”。
TEXT_STYLE_REVISION = 1


def _text(value: Any, default: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _number(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result and abs(result) != float("inf") else default


def normalize_text_style(payload: Any = None) -> dict[str, object]:
    """把旧字段和缺失字段归一化为正式封面文字样式。"""

    source: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
    fill = source.get("fill_color", source.get("fill"))
    stroke = source.get("stroke_color", source.get("stroke"))
    return {
        "font_family": _text(source.get("font_family"), str(DEFAULT_TEXT_STYLE["font_family"])),
        "font_weight": min(1000, max(100, _integer(source.get("font_weight"), int(DEFAULT_TEXT_STYLE["font_weight"]))),),
        "fill_color": _text(fill, str(DEFAULT_TEXT_STYLE["fill_color"])),
        "stroke_color": _text(stroke, str(DEFAULT_TEXT_STYLE["stroke_color"])),
        "stroke_width": min(64, max(0, _integer(source.get("stroke_width"), int(DEFAULT_TEXT_STYLE["stroke_width"]))),),
        "shadow": bool(source.get("shadow", DEFAULT_TEXT_STYLE["shadow"])),
        "line_spacing": min(3.0, max(0.5, _number(source.get("line_spacing"), float(DEFAULT_TEXT_STYLE["line_spacing"]))),),
    }


def wrap_text_lines(
    text: str,
    font_size: int,
    *,
    max_width: float = 0.86,
    max_lines: int = 8,
    canvas_width: int = 1440,
) -> tuple[str, ...]:
    """按正式 renderer 使用的字符宽度规则拆分手动文字行。"""

    available_width = max(1, int(canvas_width)) * max(0.05, min(1.0, float(max_width)))
    limit = max(8.0, available_width / max(24.0, float(font_size)))
    lines: list[str] = []
    for source_line in (str(text).replace("\r\n", "\n").split("\n") or [""]):
        source_lines_start = len(lines)
        current = ""
        units = 0.0
        chars = list(source_line)
        for index, char in enumerate(chars):
            width = 1.18 if ord(char) > 0x2E80 or ord(char) > 0x1F000 else (0.65 if char.isascii() else 1.0)
            remaining = len(chars) - index - 1
            # 最后一字不单独孤行；允许这一字与上一行轻微超出近似阈值，
            # renderer 随后仍会用真实字体 bbox 做最终测量。
            if current and units + width > limit and remaining != 0:
                lines.append(current)
                current, units = "", 0.0
            current += char
            units += width
        lines.append(current or " ")
        # 重新平衡中间单字行。显式手动换行仍以 source_lines_start 为边界，
        # 不把用户已经写好的两行合并。
        end = len(lines)
        index = source_lines_start + 1
        while index < end:
            if len(lines[index]) != 1:
                index += 1
                continue
            if len(lines[index - 1]) > 1:
                moved = lines[index - 1][-1]
                previous = lines[index - 1][:-1]
                current = moved + lines[index]
                if len(previous) == 1:
                    # 不把“上一行最后一字 + 当前孤字”变成新的孤字行。
                    lines[index - 1] = previous + current
                    del lines[index]
                    end -= 1
                    continue
                lines[index - 1] = previous
                lines[index] = current
            elif index + 1 < end and len(lines[index + 1]) > 1:
                lines[index] += lines[index + 1][0]
                lines[index + 1] = lines[index + 1][1:]
            index += 1
    return tuple(lines[: max(1, int(max_lines))]) or (" ",)


def style_value(style: Any, key: str, default: Any = None) -> Any:
    """同时支持 TextStyle、字典和旧 fill/stroke 属性。"""

    if isinstance(style, Mapping):
        if key in style:
            return style[key]
        aliases = {"fill_color": "fill", "stroke_color": "stroke"}
        return style.get(aliases.get(key, key), default)
    if style is None:
        return default
    if hasattr(style, key):
        return getattr(style, key)
    aliases = {"fill_color": "fill", "stroke_color": "stroke"}
    return getattr(style, aliases.get(key, key), default)
