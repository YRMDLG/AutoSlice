"""投稿标题导入和封面短文案生成。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .style import MELODY_STYLE, VisualStyleRecommendation, get_template

TITLE_PREFIX_RE = re.compile(r"^\s*[【\[]\s*泽音(?:Melody)?\s*[】\]]\s*", re.IGNORECASE)
ORIGINAL_FILE_RE = re.compile(r"原文件\s*[：:]\s*`([^`]+)`")
BOLD_TITLE_RE = re.compile(r"(?m)^\*\*(.+?)\*\*\s*$")
VIDEO_PREFIX_RE = re.compile(r"^\d{1,3}_\d+(?:\.\d+)?s_", re.IGNORECASE)
EMOJI_BOUNDARY_RE = re.compile(r"([\U0001F300-\U0001FAFF])(?=[\u3400-\u9fffA-Za-z0-9])")
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")
QUOTE_RE = re.compile(r"[“”‘’「」『』\"']")
CLAUSE_BOUNDARY_RE = re.compile(r"(?<=[，,。！？!?；;：:])")
PREFERRED_BREAK_AFTER = frozenset("，,。！？!?；;：:的了呢吧啊呀啦嘛和但却又还就也再去来在给把被让说问看想是当要会能连")
COVER_COPY_REPLACEMENTS = (
    ("时守星沙", "SSXS"),
    ("建模设计师", "建模师"),
    ("被司机回头盯上", "被司机盯上"),
    ("保安都认识音音了", "保安都认识音音"),
)
LOW_INFORMATION_CLAUSES = frozenset({"别搞笑了", "不是姐们何意味", "不是姐们你在说啥"})


@dataclass(frozen=True, slots=True)
class CoverLine:
    """一行封面文字及其语义颜色角色。"""

    text: str
    role: str


@dataclass(frozen=True, slots=True)
class LayoutVariant:
    """一套可供用户快速切换的标题排版候选。"""

    key: str
    label: str
    template_key: str
    palette_key: str
    reason: str
    lines: tuple[CoverLine, ...]

    def to_dict(self) -> dict[str, str | list[dict[str, str]]]:
        """返回适合 API 和前端使用的结构。"""

        return {
            "key": self.key,
            "label": self.label,
            "template_key": self.template_key,
            "palette_key": self.palette_key,
            "reason": self.reason,
            "lines": [{"text": line.text, "role": line.role} for line in self.lines],
        }


def read_text(path: str | Path) -> str:
    """以常见中文编码读取文本文件。"""

    source = Path(path)
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return source.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return ""


def parse_title_markdown(content: str) -> dict[str, str]:
    """解析 AutoSlice 生成的投稿标题 Markdown。

    返回值使用原视频文件名作为 key，投稿标题作为 value。格式不完整的段落会被忽略。
    """

    result: dict[str, str] = {}
    matches = list(ORIGINAL_FILE_RE.finditer(content))
    for index, match in enumerate(matches):
        section_end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        section = content[match.end() : section_end]
        title_match = BOLD_TITLE_RE.search(section)
        if title_match is None:
            continue
        filename = Path(match.group(1).strip()).name
        title = title_match.group(1).strip()
        if filename and title:
            result[filename] = title
    return result


def load_title_map(path: str | Path | None) -> dict[str, str]:
    """从 Markdown 加载视频标题映射；未提供路径时返回空映射。"""

    if path is None or not str(path).strip():
        return {}
    source = Path(path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"标题文件不存在：{source}")
    return parse_title_markdown(read_text(source))


def strip_title_prefix(title: str) -> str:
    """移除账号统一前缀并整理空白。"""

    cleaned = TITLE_PREFIX_RE.sub("", title, count=1)
    return re.sub(r"\s+", " ", cleaned).strip()


def title_from_filename(video_name: str) -> str:
    """在没有投稿标题文件时，从切片文件名提取可编辑标题。"""

    stem = Path(video_name).stem
    stem = VIDEO_PREFIX_RE.sub("", stem)
    return re.sub(r"[_\s]+", " ", stem).strip() or "未命名切片"


def match_title(video_name: str, title_map: dict[str, str]) -> str:
    """为视频匹配投稿标题，匹配失败时回退到文件名。"""

    normalized = {Path(name).name.casefold(): title for name, title in title_map.items()}
    filename = Path(video_name).name
    exact = normalized.get(filename.casefold())
    if exact:
        return exact

    video_stem = Path(filename).stem.casefold()
    for mapped_name, title in normalized.items():
        if Path(mapped_name).stem.casefold() == video_stem:
            return title

    index_match = re.match(r"^(\d{1,3})_", filename)
    if index_match:
        prefix = f"{index_match.group(1)}_".casefold()
        candidates = [title for name, title in normalized.items() if name.startswith(prefix)]
        if len(candidates) == 1:
            return candidates[0]
    return title_from_filename(filename)


def visual_units(text: str) -> int:
    """估算一段文字在中文粗体字体中的横向占用。"""

    units = 0
    for character in text:
        if unicodedata.combining(character):
            continue
        if ord(character) >= 0x1F000 or unicodedata.east_asian_width(character) in {"W", "F"}:
            units += 2
        else:
            units += 1
    return units


def _wrap_visual(text: str, max_units: int) -> list[str]:
    lines: list[str] = []
    current: list[str] = []
    current_units = 0
    for character in text.strip():
        character_units = visual_units(character)
        if current and current_units + character_units > max_units:
            lines.append("".join(current).strip())
            current = []
            current_units = 0
        current.append(character)
        current_units += character_units
    if current:
        lines.append("".join(current).strip())
    lines = [line for line in lines if line]
    if len(lines) < 2 or visual_units(lines[-1]) > max(4, max_units // 4):
        return lines

    combined = lines[-2] + lines[-1]
    candidates: list[tuple[int, int, int]] = []
    for index in range(1, len(combined)):
        left_units = visual_units(combined[:index])
        right_units = visual_units(combined[index:])
        if left_units > max_units or right_units > max_units:
            continue
        balance = abs(left_units - right_units)
        boundary_bonus = 8 if combined[index - 1] in PREFERRED_BREAK_AFTER else 0
        splits_ascii_word = (
            combined[index - 1].isascii()
            and combined[index - 1].isalnum()
            and combined[index].isascii()
            and combined[index].isalnum()
        )
        ascii_penalty = 100 if splits_ascii_word else 0
        candidates.append((balance - boundary_bonus + ascii_penalty, balance, index))
    if not candidates:
        return lines
    split_at = min(candidates)[2]
    left, right = combined[:split_at].strip(), combined[split_at:].strip()
    if left and right and visual_units(left) <= max_units and visual_units(right) <= max_units:
        lines[-2:] = [left, right]
    return lines


def _split_clauses(title: str) -> list[str]:
    text = strip_title_prefix(title)
    for source, replacement in COVER_COPY_REPLACEMENTS:
        text = text.replace(source, replacement)
    text = QUOTE_RE.sub("", text)
    text = EMOJI_BOUNDARY_RE.sub(r"\1|", text)
    text = CLAUSE_BOUNDARY_RE.sub("|", text)
    clauses: list[str] = []
    for part in text.split("|"):
        clause = EMOJI_RE.sub("", part).strip().strip("，,。；;：: ")
        if clause and clause not in clauses:
            clauses.append(clause)
    merged: list[str] = []
    index = 0
    while index < len(clauses):
        current = clauses[index]
        if index + 1 < len(clauses):
            following = clauses[index + 1]
            current_text = current.rstrip("！？!?")
            following_text = following.rstrip("！？!?")
            is_short_question = current.endswith(("？", "?")) and len(current_text) <= 4
            is_short_answer = (
                len(following_text) <= 4
                and following_text not in LOW_INFORMATION_CLAUSES
                and visual_units(current + following) <= 16
            )
            if is_short_question and is_short_answer:
                merged.append(current + following)
                index += 2
                continue
        merged.append(current)
        index += 1
    return merged


def recommend_visual_style(title: str) -> VisualStyleRecommendation:
    """根据历史封面规律推荐构图模板与调色板。"""

    cleaned = strip_title_prefix(title)
    if any(keyword in cleaned for keyword in ("晚安小音音", "小音的一晚", "歌回点评音")):
        return VisualStyleRecommendation("night", "night_purple", "命中晚安系列固定主题封面")

    if any(keyword in cleaned for keyword in ("警告", "请勿外放", "太隐晦", "禁止外放")):
        return VisualStyleRecommendation("warning", "warning", "命中警告或整活关键词")

    conflict_keywords = (
        "生气",
        "大骂",
        "流氓",
        "下头",
        "破产",
        "地狱",
        "退钱",
        "枪毙",
        "红SC",
        "失败",
        "爆哭",
        "犯罪",
        "盯上",
        "身份靠自己",
    )
    if any(keyword in cleaned for keyword in ("秘密", "长度", "多少", "为什么")):
        palette_key = "latest_secret"
    elif any(keyword in cleaned for keyword in conflict_keywords):
        palette_key = "latest_conflict"
    elif any(keyword in cleaned for keyword in ("润喉糖", "本子", "礼物是谁送")):
        palette_key = "latest_yellow"
    elif any(
        keyword in cleaned
        for keyword in ("生日", "朋友", "开心", "可爱", "唱歌", "温柔", "保安", "新衣", "萤火虫")
    ):
        palette_key = "latest_soft"
    elif len(_split_clauses(cleaned)) <= 2 and visual_units(cleaned) <= 22:
        palette_key = "latest_yellow"
    else:
        palette_key = "latest_cyan"

    if any(
        keyword in cleaned
        for keyword in ("SC", "私信", "评论", "动态", "邮件", "聊天记录", "截图", "群发", "投稿")
    ):
        return VisualStyleRecommendation("evidence", palette_key, "标题包含需要在画面中保留的文字证据")

    if any(
        keyword in cleaned
        for keyword in ("游戏", "通关", "按键", "手柄", "BOSS", "关卡", "节奏天国", "PVZ", "马车", "开一把")
    ):
        return VisualStyleRecommendation("gameplay", palette_key, "标题描述游戏内事件，应保留玩法画面")

    performance_hit = any(
        keyword in cleaned for keyword in ("翻唱", "合唱", "演唱", "上车舞", "舞台", "歌回")
    ) or bool(re.search(r"(?:3D(?:首场|演出|直播|回)|(?:首场|演出).*3D)", cleaned, re.IGNORECASE))
    if performance_hit:
        return VisualStyleRecommendation("performance", "dark_stage", "标题命中唱歌、3D 或舞台内容")

    if any(keyword in cleaned for keyword in ("周年纪念", "纪念回", "生日会", "谢幕", "活动海报")):
        return VisualStyleRecommendation("poster", "dark_stage", "标题命中纪念或活动海报内容")

    if any(keyword in cleaned for keyword in ("看二创", "看视频", "看AI", "看《", "锐评", "复盘", "采访")):
        return VisualStyleRecommendation("reaction", "media", "标题以外部视频或二创内容为主要视觉证据")

    clauses = _split_clauses(cleaned)
    if len(clauses) <= 2 and visual_units(cleaned) <= 38:
        return VisualStyleRecommendation("headline", palette_key, "标题较短，适合两行头条构图")
    return VisualStyleRecommendation("dialog", palette_key, "多段事件与原话，适合直播对话构图")


def _assign_line_roles(lines: list[str], template_key: str) -> list[CoverLine]:
    """按历史封面语义为文案行分配颜色角色。"""

    if len(lines) == 1:
        return [CoverLine(lines[0], "emphasis")]
    if template_key == "night":
        return [CoverLine(lines[0], "emphasis")] + [
            CoverLine(line, "context") for line in lines[1:]
        ]
    if template_key == "dialog" and len(lines) >= 4:
        return [
            CoverLine(line, ("context", "context", "quote", "emphasis")[index])
            for index, line in enumerate(lines[:4])
        ]

    result: list[CoverLine] = []
    for index, line in enumerate(lines):
        if index == 0:
            role = "context"
        elif index == len(lines) - 1:
            role = "emphasis"
        else:
            role = "quote"
        result.append(CoverLine(line, role))
    return result


def create_cover_copy(
    title: str,
    *,
    max_lines: int | None = None,
    max_line_units: int | None = None,
    template_key: str | None = None,
) -> list[CoverLine]:
    """生成带语义角色的封面文案，供渲染器按含义配色。"""

    lines = create_cover_lines(
        title,
        max_lines=max_lines,
        max_line_units=max_line_units,
        template_key=template_key,
    )
    effective_template_key = template_key or recommend_visual_style(title).template_key
    return _assign_line_roles(lines, effective_template_key)


def create_cover_lines(
    title: str,
    *,
    max_lines: int | None = None,
    max_line_units: int | None = None,
    template_key: str | None = None,
) -> list[str]:
    """将投稿标题压缩成适合封面的大字行。

    文案优先保留标题开头的事件钩子和结尾的反差或原话。所有行都会限制视觉宽度，避免渲染时截字。
    """

    recommendation = recommend_visual_style(title)
    template = get_template(template_key or recommendation.template_key)
    effective_max_lines = template.max_lines if max_lines is None else max_lines
    effective_max_units = template.max_line_units if max_line_units is None else max_line_units

    if effective_max_lines < 1 or effective_max_units < 4:
        raise ValueError("封面行数和单行宽度必须为正数")

    clauses = _split_clauses(title)
    if not clauses:
        return ["未命名切片"]

    lines: list[str] = []
    for clause in clauses:
        lines.extend(_wrap_visual(clause, effective_max_units))

    deduplicated: list[str] = []
    for line in lines:
        normalized = line.rstrip("！？!?")
        existing_normalized = [existing.rstrip("！？!?") for existing in deduplicated]
        is_short_repeat = len(normalized) <= 4 and any(
            normalized in existing for existing in existing_normalized
        )
        if normalized and normalized not in existing_normalized and not is_short_repeat:
            deduplicated.append(line)

    informative = [
        line
        for line in deduplicated
        if line.rstrip("！？!?") not in LOW_INFORMATION_CLAUSES
    ]
    if informative:
        deduplicated = informative
    if len(deduplicated) <= effective_max_lines:
        return deduplicated
    if effective_max_lines == 1:
        return [deduplicated[0]]
    if effective_max_lines == 2:
        return [deduplicated[0], deduplicated[-1]]

    head_count = (effective_max_lines + 1) // 2
    tail_count = effective_max_lines - head_count
    return deduplicated[:head_count] + deduplicated[-tail_count:]


def recommend_layout_variants(title: str) -> list[LayoutVariant]:
    """为当前完整标题生成三种不重复、可直接渲染的排版候选。"""

    primary = recommend_visual_style(title)
    candidates = [
        (
            primary.template_key,
            primary.palette_key,
            "智能推荐",
            primary.reason,
        ),
        (
            "dialog",
            primary.palette_key,
            "对话四行",
            "保留事件背景、原话和结尾爆点",
        ),
        (
            "headline",
            "latest_yellow",
            "头条双行",
            "只保留最强钩子和结尾反差",
        ),
        (
            "evidence",
            primary.palette_key,
            "保留画面",
            "缩小文字占用，为截图或人物留出空间",
        ),
    ]

    variants: list[LayoutVariant] = []
    seen_templates: set[str] = set()
    for template_key, palette_key, label, reason in candidates:
        if template_key in seen_templates:
            continue
        seen_templates.add(template_key)
        lines = tuple(create_cover_copy(title, template_key=template_key))
        variants.append(
            LayoutVariant(
                key=f"{template_key}:{palette_key}",
                label=label,
                template_key=template_key,
                palette_key=palette_key,
                reason=reason,
                lines=lines,
            )
        )
        if len(variants) == 3:
            break
    return variants
