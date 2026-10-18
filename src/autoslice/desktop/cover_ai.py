"""封面 AI：读标题和字幕写 A/B 文案并标强调词；在爆点前后挑表情最有戏的一帧；
看候选成品挑三张；看成品给能一键应用的修改。

AI 只在用户点击时运行。文案必须出自标题和字幕原文（逐字校验，允许删减、
调序和补少量虚词，不许加实词）。写法参考日更切片号：标题交代来龙去脉，封面放
标题爆点浓缩成的一两句，或主播自带情绪的一句原话，关键词换色。方案不让 AI 给坐标：本地
排版引擎先排出十几张候选（不同文案、排法位置、取景、配色）并渲染成缩略图总图，
看图模型像审稿一样从成品里挑——压脸、压住画面原有的字、看不懂的都淘汰。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image, ImageDraw, ImageFont

from autoslice.llm.transport import call_llm, extract_json_payload

from .ai_settings import AISettings, read_ai_settings
from .cover_copy import BasicCoverCopy
from .cover_fix_schema import AIObjectEdit, edit_steps, object_changes
from .cover_style import STYLE_PRESETS
from .cover_works import CoverWork
from .foundation import DesktopStorage

DIRECTIONS = ("稳妥", "换个构图", "大胆一点")
# 字幕太长时只取这么多字给模型；够覆盖一条切片。
_MAX_SUBTITLE_CHARS = 9000
_MAX_CONTEXT = 16
_MAX_HEADLINE = 24
_WORD_RE = re.compile(r"[一-鿿A-Za-z0-9]")
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF☀-➿️‍]")
# 压缩成封面文字时可以补的虚词：不带新信息，补了句子才通顺。
_FUNCTION_WORDS = frozenset("的地得了着过在把被让给和与跟是也都就还又才吗呢吧啊呀哦嘛到时后前里上下中这那个们")
# 配合标题看抓不抓眼、看不看得懂（模型自评 1~5），低于这个分的文案不要。
_MIN_CLARITY = 4
# 每段文案最多几个强调词、每个最多几个字（超长的不截断，直接不要）。
_MAX_EMPHASIS = 2
_MAX_EMPHASIS_LENGTH = 8
_PRESET_LABELS = {preset.key: preset.label for preset in STYLE_PRESETS}
# 候选总图：每行几张、每张多宽。
_SHEET_COLUMNS = 4
_SHEET_THUMB_WIDTH = 320


class CoverAIError(RuntimeError):
    """给用户看的 AI 失败原因。"""


@dataclass(frozen=True, slots=True)
class AIHighlight:
    quote: str
    start: float
    end: float
    reason: str


@dataclass(frozen=True, slots=True)
class AICopy:
    context: str
    headline: str
    # 写法：标题（浓缩标题爆点）/ 原话（旧缓存里可能是“讲事、结果、反差”等）。
    angle: str
    reason: str
    emphasis: tuple[str, ...] = ()

    def as_basic(self) -> BasicCoverCopy:
        return BasicCoverCopy(context=self.context, headline=self.headline, emphasis=self.emphasis)


@dataclass(frozen=True, slots=True)
class AIAnalysis:
    highlight: AIHighlight | None
    copies: tuple[AICopy, ...]
    # 标题提炼那句文案的强调词。
    title_emphasis: tuple[str, ...] = ()


# 源图比例坐标的框 (x0, y0, x1, y1)。
Frac = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class AISchemeIdea:
    """一张候选封面怎么排：文案、排法、配色、文字位置、取景。"""

    direction: str
    copy: AICopy
    layout: str
    preset: str
    reason: str
    # 大字、标题在上用 top/bottom，侧边用 left/right/top/bottom；上下分置不用。
    place: str = ""
    # 大于 1 时把画面放大到人物，裁掉两侧的弹幕栏和直播界面。
    zoom: float = 1.0
    # 按看图标出的脸取景：loose 带上半身、tight 特写；空表示不按脸取景。优先于 zoom。
    framing: str = ""


@dataclass(frozen=True, slots=True)
class SubmissionCopy:
    """投稿用的文字：备选标题、简介、标签。"""

    titles: tuple[str, ...]
    description: str
    tags: tuple[str, ...]


# B 站投稿标题最多 80 字；标签最多 10 个、每个最多 20 字；简介最多给这么多字。
_MAX_TITLE = 80
_MAX_TAGS = 10
_MAX_TAG = 20
_MAX_DESCRIPTION = 400


@dataclass(frozen=True, slots=True)
class AIPick:
    direction: str
    index: int
    reason: str


@dataclass(frozen=True, slots=True)
class AIChoice:
    picks: tuple[AIPick, ...]
    rejected: str


@dataclass(frozen=True, slots=True)
class AIFrameNotes:
    """看图模型看原画面（没加字）标出的框：主播的脸、人物，画面自带的字和界面各在哪。

    怎么裁、字放哪由本地按这些框算：整张脸留在画面里，字带避开界面和脸。
    busy 是界面落在画面哪几侧（上下左右），没认出脸时粗略避开用。
    """

    busy: frozenset[str]
    note: str
    face: Frac | None = None
    person: Frac | None = None
    ui: tuple[Frac, ...] = ()


@dataclass(frozen=True, slots=True)
class AIFramePick:
    """在爆点前后几帧里挑的那张（0 是原来的画面）。"""

    index: int
    expression: str
    reason: str


# AI 修改建议只能从这些一键操作里选。
FIX_ACTIONS = {
    "edit_objects": "按 ID 分别调整文字、图片、贴纸或形状，保留正文和已有风格",
    "move_bottom": "封面文字整体移到画面下方",
    "move_top": "封面文字整体移到画面上方",
    "zoom_in": "画面拉近人物，裁掉四周的界面和弹幕",
    "zoom_out": "画面拉远一点（人物太大、没地方放字时）",
    "bigger": "主文案 B 放大",
    "smaller_context": "A 缩小，让 B 更突出",
    "emphasize": "把关键词换成强调色",
    "tilt": "字整体略微倾斜，更有动感",
    "burst": "给强调词（没有就整段主文案）垫一个漫画爆炸贴",
    "outline": "字外面再加一圈描边，从背景里跳出来",
    "outline_colors": "按文字填色和画面调整各文字块的描边、外描边配色，不改填色、字号和位置",
    "restyle": "换一套配色",
    "rewrite": "换一句文案",
}


# 弹窗里显示的操作名（比给 AI 看的说明短）。
_ACTION_NAMES = {
    "edit_objects": "调整文字与构图",
    "move_bottom": "字移到下方", "move_top": "字移到上方", "zoom_in": "画面拉近", "zoom_out": "画面拉远",
    "bigger": "主文案放大", "smaller_context": "A 缩小", "tilt": "字倾斜", "outline": "加外描边",
    "burst": "垫爆炸贴",
    "outline_colors": "优化描边配色",
}
# 一条建议最多组合几个操作；互相抵消的操作只留前一个。
_MAX_FIX_ACTIONS = 3
_OPPOSITE = {"move_bottom": "move_top", "move_top": "move_bottom", "zoom_in": "zoom_out", "zoom_out": "zoom_in"}


@dataclass(frozen=True, slots=True)
class AIOutline:
    """一个已知文字块的描边配色；外描边 None 表示保留，空串表示去掉。"""

    object_id: str
    stroke: str
    outer_stroke: str | None = None


@dataclass(frozen=True, slots=True)
class AIFix:
    """一条修改建议：1~3 个一键操作组合在一起（按顺序做），附带各操作要的参数。"""

    issue: str
    actions: tuple[str, ...]
    context: str = ""
    headline: str = ""
    words: tuple[str, ...] = ()
    # restyle 时换成哪套配色（STYLE_PRESETS 的 key）。
    preset: str = ""
    outlines: tuple[AIOutline, ...] = ()
    edits: tuple[AIObjectEdit, ...] = ()

    def _name(self, action: str) -> str:
        if action == "rewrite":
            return f"换成 A「{self.context}」 B「{self.headline}」" if self.context else f"换成「{self.headline}」"
        if action == "emphasize":
            return "强调「" + "」「".join(self.words) + "」"
        if action == "restyle":
            return f"换成「{_PRESET_LABELS.get(self.preset, self.preset)}」配色"
        return _ACTION_NAMES.get(action, FIX_ACTIONS.get(action, action))

    @property
    def label(self) -> str:
        return " + ".join(self.steps)

    @property
    def steps(self) -> tuple[str, ...]:
        return tuple(step for action in self.actions for step in (
            edit_steps(self.edits) if action == "edit_objects" else (self._name(action),)
        ))


def _outline_changes(raw, allowed: set[str]) -> tuple[AIOutline, ...]:
    """只接收已知文字 ID 和六位十六进制颜色，不接收任意样式字段。"""

    if not isinstance(raw, list):
        return ()
    changes = []
    seen = set()
    for item in raw[:32]:
        if not isinstance(item, dict):
            continue
        object_id = str(item.get("id") or "").strip()
        stroke = str(item.get("stroke") or "").strip().upper()
        outer = item.get("outer_stroke")
        outer = outer.strip().upper() if isinstance(outer, str) else outer
        if object_id not in allowed or object_id in seen or not re.fullmatch(r"#[0-9A-F]{6}", stroke):
            continue
        if outer is not None and (not isinstance(outer, str) or outer and not re.fullmatch(r"#[0-9A-F]{6}", outer)):
            continue
        changes.append(AIOutline(object_id, stroke, outer))
        seen.add(object_id)
    return tuple(changes)


def from_source(text: str, source: str) -> bool:
    """文案的每个实词字都在原文里出现过：允许删减、调序和补少量虚词，不许加新内容。"""

    allowed = set(source) | _FUNCTION_WORDS
    return all(character in allowed for character in text if _WORD_RE.match(character))


def _has_evidence(quote: str, sources: Sequence[str]) -> bool:
    """证据必须是某一段真实原文，不能把散落在各处的字拼成一句话。"""

    words = "".join(_WORD_RE.findall(quote))
    return len(words) >= 2 and any(words in "".join(_WORD_RE.findall(source)) for source in sources)


def jpeg_bytes(image: str | Path | Image.Image | bytes, *, max_side: int = 1280, quality: int = 85) -> bytes:
    """缩到长边不超过 max_side 的 JPEG，发给看图模型。"""

    if isinstance(image, (bytes, bytearray)):
        image = Image.open(io.BytesIO(bytes(image)))
    elif not isinstance(image, Image.Image):
        image = Image.open(image)
    with image:
        picture = image.convert("RGB")
        picture.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        picture.save(buffer, format="JPEG", quality=quality)
        return buffer.getvalue()


def contact_sheet(thumbnails: Sequence[bytes], *, labels_outside: bool = False) -> bytes:
    """把候选缩略图拼成一张带编号（从 1 开始）的总图，按首页小图的大小给模型看。"""

    pictures = []
    for data in thumbnails:
        with Image.open(io.BytesIO(data)) as image:
            picture = image.convert("RGB")
            picture.thumbnail((_SHEET_THUMB_WIDTH, _SHEET_THUMB_WIDTH))
            pictures.append(picture)
    if not pictures:
        raise ValueError("没有候选可拼图")
    gap = 10
    label_height = 42 if labels_outside else 0
    cell_width = max(item.width for item in pictures)
    cell_height = max(item.height for item in pictures) + label_height
    rows = (len(pictures) + _SHEET_COLUMNS - 1) // _SHEET_COLUMNS
    columns = min(_SHEET_COLUMNS, len(pictures))
    sheet = Image.new("RGB", (columns * (cell_width + gap) + gap, rows * (cell_height + gap) + gap), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=30)
    for index, picture in enumerate(pictures):
        x = gap + (index % _SHEET_COLUMNS) * (cell_width + gap)
        y = gap + (index // _SHEET_COLUMNS) * (cell_height + gap)
        sheet.paste(picture, (x, y + label_height))
        label = str(index + 1)
        box = draw.textbbox((0, 0), label, font=font)
        draw.rectangle((x, y, x + box[2] + 14, y + box[3] + 10), fill=(220, 30, 30))
        draw.text((x + 7, y + 3), label, fill=(255, 255, 255), font=font)
    buffer = io.BytesIO()
    sheet.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def _box(value: object, minimum: float) -> Frac | None:
    """模型给的框：四个 0~1 的数、左上在右下之前、宽高都不小于 minimum；不合理就不用。"""

    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (min(1.0, max(0.0, float(item))) for item in value)
    except (TypeError, ValueError):
        return None
    if x1 - x0 < minimum or y1 - y0 < minimum:
        return None
    return x0, y0, x1, y1


def _sides(boxes: Sequence[Frac]) -> frozenset[str]:
    """界面框落在画面哪几侧：中心靠近哪条边就算哪侧。"""

    sides = set()
    for x0, y0, x1, y1 in boxes:
        center_x, center_y = (x0 + x1) / 2, (y0 + y1) / 2
        sides.update(name for name, hit in (
            ("top", center_y < 0.25), ("bottom", center_y > 0.75), ("left", center_x < 0.25), ("right", center_x > 0.75),
        ) if hit)
    return frozenset(sides)


def _emphasis(value: object, *parts: str) -> tuple[str, ...]:
    """强调词：必须是文案里原样出现的片段（最多 8 字、两个）；整句都标等于没标，不要。"""

    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    def core(text: str) -> str:
        return "".join(character for character in text if _WORD_RE.match(character))

    words: list[str] = []
    for item in value:
        word = _clean(item, 40)
        # 去掉标点后等于整句（「改变选曲」对「改变选曲？！」）也算整句都标了。
        if 0 < len(word) <= _MAX_EMPHASIS_LENGTH and word not in words and any(
            word in part and core(word) != core(part) for part in parts
        ):
            words.append(word)
    return tuple(words[:_MAX_EMPHASIS])


# 界面框最多几个、各种框的最小边长（画面比例）。
_MAX_UI_BOXES = 10
_MIN_FACE, _MIN_PERSON, _MIN_UI = 0.03, 0.08, 0.01


def _frame_notes(payload: dict) -> AIFrameNotes:
    ui = tuple(
        box for box in (_box(item, _MIN_UI) for item in (payload.get("ui") or ())[:_MAX_UI_BOXES]) if box is not None
    )
    return AIFrameNotes(
        busy=_sides(ui), note=_clean(payload.get("note"), 60),
        face=_box(payload.get("face"), _MIN_FACE), person=_box(payload.get("person"), _MIN_PERSON), ui=ui,
    )


def _image_size(frame: bytes) -> str:
    try:
        with Image.open(io.BytesIO(frame)) as image:
            return f"{image.size[0]}x{image.size[1]} 像素，"
    except OSError:
        return ""


def _clean(value: object, limit: int) -> str:
    text = _EMOJI_RE.sub("", str(value or "")).strip().strip("“”\"'「」")
    return text[:limit].strip()


def _seconds(value: object) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _subtitle_lines(cues: Sequence[tuple[float, float, str]]) -> str:
    lines, used = [], 0
    for start, _end, text in cues:
        line = f"[{start:.1f}] {str(text).strip()}"
        if used + len(line) > _MAX_SUBTITLE_CHARS:
            lines.append("……（后面的字幕省略）")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _work_examples(works: Sequence[CoverWork]) -> str:
    rows = [f"- A「{item.context}」 B「{item.headline}」" for item in works if item.headline]
    return "\n".join(rows[:8]) or "（还没有作品）"


class CoverAI:
    def __init__(
        self,
        storage: DesktopStorage,
        *,
        llm: Callable[..., str] = call_llm,
        settings: Callable[[], AISettings] = read_ai_settings,
    ) -> None:
        self.cache = storage.ai_cache / "cover"
        self._llm = llm
        self._settings = settings

    def _ask(self, prompt: str, *, vision: bool, images: Sequence[bytes] = (), max_tokens: int = 4000) -> dict:
        settings = self._settings()
        if not settings.configured:
            raise CoverAIError("还没配置 AI 接口：到设置页填写地址、密钥和模型")
        model = (settings.vision_model or settings.text_model) if vision else settings.text_model
        digest = hashlib.sha256(model.encode("utf-8") + prompt.encode("utf-8"))
        for image in images:
            digest.update(hashlib.sha256(image).digest())
        cache_path = self.cache / f"{digest.hexdigest()[:32]}.json"
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        try:
            text = self._llm(
                prompt, max_tokens=max_tokens, model_override=model,
                images=tuple(("image/jpeg", image) for image in images),
            )
        except Exception as exc:  # noqa: BLE001 - 模型或网络的任何失败都要变成用户看得懂的提示
            raise CoverAIError(f"AI 请求失败：{str(exc)[:200]}") from exc
        payload = extract_json_payload(text)
        if not isinstance(payload, dict):
            raise CoverAIError("AI 没有按要求返回 JSON，可以再试一次")
        DesktopStorage._write_json(cache_path, payload)
        return payload

    def analyze(
        self,
        title: str,
        cues: Sequence[tuple[float, float, str]],
        *,
        streamer: str = "",
        recent: Sequence[CoverWork] = (),
        round_index: int = 0,
        title_copy: BasicCoverCopy | None = None,
        rewrite: bool = True,
    ) -> AIAnalysis:
        """读标题和整份字幕：标出爆点句，给“标题浓缩”和“原话”两种写法的 A/B 文案，并标强调词。"""

        source = title + "\n" + "\n".join(text for _start, _end, text in cues)
        title_line = (
            f"\n当前封面字是 A「{title_copy.context}」 B「{title_copy.headline}」，这是用户的基准，也给它标强调词（title_emphasis）。\n"
            if title_copy is not None and title_copy.headline else ""
        )
        prompt = f"""你是 B 站直播切片的封面文案编辑。根据投稿标题和这条切片的校对字幕，写封面上的字。

分工：投稿标题负责交代来龙去脉，封面字负责抓眼——观众先在首页看到封面，再看标题。
封面字和标题说的必须是同一个爆点（可以换个说法、更短更冲），不能拿视频里的细节当 B：
比如标题是「打排位被队友气到摔耳机」，「第三局那个辅助」「他买了两双鞋」「然后就开大了」都是只有看完视频才懂的细节，没看过的人不知道在说什么，不能用。
封面字有两种写法：
- 标题浓缩（每次给 2~4 组）：把投稿标题的爆点浓缩成 A + B 两句（A 交代、B 是结果或反差）或只有 B 一句，几组之间换说法、换语序、加反问，比如标题「逆天音姐听到公主说要用脚惩罚她时高兴的发出了鸟叫」→ A「听到要用脚惩罚」 B「高兴的发出了鸟叫」。字幕只用来找更有力的原词，不要换成视频里别的事。
- 原话（0~2 组）：主播（或对方）的原话本身就有情绪、不用上下文也懂（骂人、惊叫、反问、离谱发言）时，删到 4~12 个字直接当 B，比如「我靠，真给我开盒了」「你有病啊！？」。这条视频没有这种话就不要给原话写法，别硬凑。

要求：
1. 只能从标题和字幕里截取、删减、压缩，可以补“的、了、被、到”这类虚词；不许加原文没有的事实和词，不写营销话术。
2. 没看过视频的人只看封面和标题，要能明白在说什么事、为什么好笑或离谱。不要空泛的话（「这就是暗号」「原来是这样」）。
3. “告诉你”“揭秘”“原因竟然是这个”这类预告语不能当 B。
4. 每组标 1~2 个强调词 emphasis：封面上单独换颜色的词，必须是 A 或 B 里原样出现的片段，2~5 个字，挑最戳人的（数字、关键名词、骂人的词、反差词），比如「开盒」「14个耳钉」「万楼」；不要把整句都标上。

这位切片员以前的标题 → 封面字（标题浓缩）：
- 「泽音一个晚上居然被冲了万楼？！原因居然是这个？！」→ A「一个晚上」 B「被冲了万楼？！」 强调「万楼」
- 「逆天音姐听到公主说要用脚惩罚她时高兴的发出了鸟叫」→ A「听到要用脚惩罚」 B「高兴的发出了鸟叫」 强调「鸟叫」
- 「刚上播不清醒的音音 嘴滑承认18岁是虚假宣传」→ A「刚上播不清醒」 B「18岁是虚假宣传？！」 强调「虚假宣传」

这位切片员最近导出的封面文字（参考长短和口吻，不要照抄）：
{_work_examples(recent)}

主播：{streamer or "未知"}
投稿标题：{title}
{title_line}
字幕（[秒数] 文本）：
{_subtitle_lines(cues)}

给 3~5 组，按好坏排序、最好的放最前面，角度尽量不同；每组自评“没看过视频的人配合标题能不能一眼看懂、抓不抓眼”1~5 分，低于 4 分的不要给。
第 {round_index + 1} 次生成{"，请给出和之前不同的句子" if round_index else ""}。
{"本次保留当前文案，只找与投稿标题同一事件的爆点原话和真实时间，copies 返回空列表。选帧附近的字幕只能帮助挑表情，不是必须用作封面字。" if not rewrite else "新文案必须比当前封面和标题更清楚、更有记忆点；没有更好的就让 copies 为空，不必凑数。不能为了引用选帧附近的字幕而丢失标题里的诱因、结果与反差。"}
只输出 JSON，不要解释：
{{"highlight": {{"quote": "爆点字幕原话", "start": 秒数, "end": 秒数, "reason": "为什么是爆点（20 字内）"}},
 "title_emphasis": ["标题那句的强调词"],
 "copies": [{{"style": "标题|原话", "context": "A，可为空", "headline": "B", "emphasis": ["强调词"], "clarity": 1到5, "reason": "一句话说明（20 字内）"}}]}}"""
        payload = self._ask(prompt, vision=False)
        copies: list[AICopy] = []
        for item in (payload.get("copies") or ()) if rewrite else ():
            if not isinstance(item, dict):
                continue
            try:
                clarity = float(item.get("clarity", _MIN_CLARITY))
            except (TypeError, ValueError):
                clarity = _MIN_CLARITY
            # 超长句直接丢弃，截断会把有意义的句子变成半句话。
            context = _clean(item.get("context"), _MAX_CONTEXT + 1)
            headline = _clean(item.get("headline"), _MAX_HEADLINE + 1)
            if len(context) > _MAX_CONTEXT or len(headline) > _MAX_HEADLINE:
                continue
            copy = AICopy(
                context=context, headline=headline,
                angle=_clean(item.get("style") or item.get("angle"), 8),
                reason=_clean(item.get("reason"), 40),
                emphasis=_emphasis(item.get("emphasis"), context, headline),
            )
            # 逐字校验：有原文里没有的实词就丢掉；模型自己都觉得看不懂的也不要。
            if not copy.headline or not _MIN_CLARITY <= clarity <= 5 or not from_source(copy.context + copy.headline, source):
                continue
            if all(existing.headline != copy.headline for existing in copies):
                copies.append(copy)
        raw = payload.get("highlight")
        highlight = None
        if isinstance(raw, dict) and str(raw.get("quote") or "").strip():
            quote = _clean(raw.get("quote"), 60)
            claimed = _seconds(raw.get("start"))
            matched = [cue for cue in cues if _has_evidence(quote, (cue[2],))]
            cue = min(matched, key=lambda cue: abs(cue[0] - claimed)) if matched else None
        else:
            cue = None
        if cue is not None:
            highlight = AIHighlight(
                quote=quote, start=cue[0], end=cue[1], reason=_clean(raw.get("reason"), 40),
            )
        if not copies and (title_copy is None or not title_copy.headline):
            raise CoverAIError("AI 给的文案都不合格（不是出自原文或看不懂），已全部丢弃；可以再试一次")
        title_emphasis = (
            _emphasis(payload.get("title_emphasis"), title_copy.context, title_copy.headline) if title_copy else ()
        )
        return AIAnalysis(highlight=highlight, copies=tuple(copies), title_emphasis=title_emphasis)

    def review_copies(
        self, title: str, cues: Sequence[tuple[float, float, str]], copies: Sequence[AICopy], *,
        current: BasicCoverCopy, fixed_texts: Sequence[str] = (),
    ) -> tuple[AICopy, ...]:
        """单独复核新文案：必须优于当前文案及标题，并能提供一段连续的原文证据。"""

        if not copies:
            return ()
        rows = [{"index": index, "context": copy.context, "headline": copy.headline}
                for index, copy in enumerate(copies, 1)]
        prompt = f"""你是独立的封面文案复核编辑，下面是另一位编辑给出的候选，不能沿用它的自评分。
投稿标题：{title}
当前封面字：A「{current.context}」 B「{current.headline}」
画布上还会保留的用户文字：{json.dumps(list(fixed_texts), ensure_ascii=False)}
候选文案：{json.dumps(rows, ensure_ascii=False)}
字幕：[秒数] 文本
{_subtitle_lines(cues)}

逐组与当前封面字、投稿标题比较，只有同时满足以下条件才通过：
- 同一个核心事件，保留标题最有价值的诱因、后果、反差，不能把附近的随口一句当爆点；
- 没看过视频的人能读懂指代、关系和笑点，口语自然，不是半句话或空泛感叹；
- 配合保留的用户文字不重复、不矛盾；在清楚程度或记忆点上明确优于当前文字与标题浓缩。
普通改写、换语序、效果相当、无法判断都淘汰。没有更好的就返回空列表。
evidence 必须逐字引用标题或某一条字幕里连续的一段，能支撑这个核心事件，不能拼字。
只输出 JSON：{{"reviews": [{{"index": 1, "same_event": true, "standalone": true,
"better_than_current": true, "better_than_title": true, "evidence": "真实原文", "reason": "具体好在哪里"}}]}}"""
        payload = self._ask(prompt, vision=False, max_tokens=2500)
        accepted = []
        seen: set[int] = set()
        sources = (title, *(text for _start, _end, text in cues))
        for row in payload.get("reviews") or ():
            if not isinstance(row, dict):
                continue
            index = row.get("index")
            if type(index) is not int or not 1 <= index <= len(copies) or index in seen:
                continue
            if not all(row.get(key) is True for key in (
                "same_event", "standalone", "better_than_current", "better_than_title",
            )) or not _has_evidence(str(row.get("evidence") or ""), sources):
                continue
            copy = copies[index - 1]
            if (
                "".join(_WORD_RE.findall(copy.context + copy.headline))
                == "".join(_WORD_RE.findall(current.context + current.headline))
            ):
                continue
            if not from_source(copy.context + copy.headline, "\n".join(sources)):
                continue
            seen.add(index)
            accepted.append(copy)
        return tuple(accepted)

    def inspect(self, frame: bytes) -> AIFrameNotes:
        """看没加字的原画面：标出主播的脸、人物，以及画面自带的字和界面各在哪（取景由本地算）。"""

        prompt = f"""这是一帧直播切片画面（{_image_size(frame)}还没加封面文字），要拿来做 B 站封面。请标出：
1. face：主播（立绘或真人）脸的框：上到眉毛、下到下巴、左右到两颊，不含头发、耳朵和脖子（动漫立绘的脸大约只占头部的一半）；画面里没有人物就给 null。
2. person：主播从头顶（含头发）到画面里能看到的身体的框；没有就给 null。
3. ui：画面自带的文字、弹幕、聊天栏、直播界面的面板和按钮、字幕条、水印、Logo、礼物栏，每一块各给一个框，最多 {_MAX_UI_BOXES} 个；没有就给空列表。
框用画面比例坐标（左上角 0,0，右下角 1,1），格式 [x0, y0, x1, y1]，贴着内容画、不要留大边。
只输出 JSON，不要解释：
{{"face": [x0, y0, x1, y1], "person": [x0, y0, x1, y1], "ui": [[x0, y0, x1, y1]], "note": "画面原有文字和界面在哪（30 字内）"}}"""
        return _frame_notes(self._ask(prompt, vision=True, images=(frame,), max_tokens=2000))

    def pick_frame(self, frames: Sequence[bytes], *, quote: str = "", title: str = "") -> AIFramePick:
        """在爆点前后几帧里挑表情最有戏的一张（第一张是原来的画面）；只挑，取景框另外单帧看。"""

        if len(frames) < 2:
            return AIFramePick(index=0, expression="", reason="")
        prompt = f"""下面 {len(frames)} 张图是同一条直播切片里的画面，按发送顺序从 1 编号：第 1 张是现在封面用的画面，其余是爆点前后按时间排的几帧。
投稿标题：{title or "未知"}{f"；爆点是「{quote}」" if quote else ""}
要做成 B 站封面，挑一张最适合的：
- 主播（立绘或真人）的表情要有戏：惊讶、大笑、崩溃、得意、嫌弃、生气这类明显的反应，比面无表情、闭眼、说到一半的嘴型好得多；
- 脸清楚、没糊、没被遮挡，人物没有被大块弹窗或字幕条挡住；
- 几张差不多时选第 1 张。
只输出 JSON，不要解释：
{{"index": 编号, "expression": "选中那张的表情（10 字内）", "reason": "为什么选它（20 字内）"}}"""
        payload = self._ask(prompt, vision=True, images=tuple(frames), max_tokens=1000)
        try:
            index = int(payload.get("index")) - 1
        except (TypeError, ValueError):
            index = 0
        return AIFramePick(
            index=index if 0 <= index < len(frames) else 0,
            expression=_clean(payload.get("expression"), 20), reason=_clean(payload.get("reason"), 40),
        )

    def choose(
        self,
        sheet: bytes,
        count: int,
        *,
        title: str = "",
        frame: bytes | None = None,
        recent_thumbnails: Sequence[bytes] = (),
        round_index: int = 0,
        current_cover: bytes | None = None,
        candidate_texts: Sequence[dict[str, str]] = (),
        fixed_texts: Sequence[str] = (),
    ) -> AIChoice:
        """看候选总图（编号从 1 开始）挑三张：稳妥、换个构图、大胆一点；不合格的一票否决。"""

        extra = []
        if frame is not None:
            extra.append("第二张是没加字的原画面，用来分辨哪些字是画面原有的、哪些是封面加的")
        if current_cover is not None:
            extra.append("原画面后是用户当前封面，用来对照已有文字、素材和有意叠放")
        if recent_thumbnails:
            extra.append("再后面几张是这位切片员最近导出的封面")
        prompt = f"""你是 B 站直播切片的资深封面编辑。第一张图里是同一条切片的 {count} 个候选封面，左上角红底白字是编号，都按首页小图的大小排着{"；" + "；".join(extra) if extra else ""}。
投稿标题：{title or "未知"}
各候选的主文案（按编号）：{json.dumps(list(candidate_texts), ensure_ascii=False)}
所有候选中保留的自建文字：{json.dumps(list(fixed_texts), ensure_ascii=False)}
主文案和自建文字要一起读，不能互相重复、打架。当前封面文案是基准，不要求换成字幕摘句。

请挑出三张：
- 稳妥：最好用、最不会出错的一张；
- 换个构图：和稳妥的构图明显不同、同样能用的一张{"，也尽量和最近的封面不一样" if recent_thumbnails else ""}；
- 大胆一点：更抓眼（字更大、对比更强或取景更紧），但仍然清楚。

一票否决，出现任何一条都不能选：
1. 封面文字压住人脸或主要人物；
2. 排版新增了文字之间的挤压，或主文案盖住固定素材、弹幕或直播界面；原封面已有的有意叠放和文字背后的爆炸贴不算错误；
3. 文案是只有看过视频才懂的细节（不知道指什么的数字、东西、半句话），或者和投稿标题说的不是同一个爆点；
4. 在小图上字太小、读不清。
都合格时，人物表情有戏、关键词换了颜色更醒目、一眼能抓住的优先。
合格的不够三张就少选，全部不合格就返回空列表。第 {round_index + 1} 次挑选。
只输出 JSON，不要解释：
{{"picks": [{{"direction": "稳妥|换个构图|大胆一点", "index": 编号, "reason": "为什么选它（25 字内）"}}], "rejected": "其余候选被淘汰的主要原因（30 字内）"}}"""
        images = (sheet, *((frame,) if frame is not None else ()),
                  *((current_cover,) if current_cover is not None else ()), *recent_thumbnails[:2])
        payload = self._ask(prompt, vision=True, images=images)
        picks: list[AIPick] = []
        for item in payload.get("picks") or ():
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index")) - 1
            except (TypeError, ValueError):
                continue
            direction = str(item.get("direction") or "").strip()
            if not 0 <= index < count or any(pick.index == index for pick in picks):
                continue
            picks.append(AIPick(
                direction=direction if direction in DIRECTIONS else DIRECTIONS[min(len(picks), 2)],
                index=index, reason=_clean(item.get("reason"), 50),
            ))
        return AIChoice(picks=tuple(picks[:3]), rejected=_clean(payload.get("rejected"), 60))

    def submission_copy(
        self,
        title: str,
        cues: Sequence[tuple[float, float, str]],
        *,
        streamer: str = "",
        past_titles: Sequence[str] = (),
        round_index: int = 0,
    ) -> SubmissionCopy:
        """按这位切片员以前的标题口吻写投稿标题（3 个备选）、简介和标签；只用字幕里有的事。"""

        examples = "\n".join(f"- {item}" for item in list(dict.fromkeys(past_titles))[:12]) or "（还没有）"
        prompt = f"""你是 B 站直播切片的投稿编辑。根据这条切片现在的标题和校对字幕，写投稿用的标题、简介和标签。

1. 标题：给 3 个备选，学这位切片员以前标题的口吻和格式（比如开头的【主播名】、标点和语气），{_MAX_TITLE} 字以内；
   要有这条视频真正的爆点或反差，只写字幕里有的事，不编造、不写“震惊”“全网”这类营销话术；三个之间换角度或说法。
2. 简介：2~3 句，交代背景和看点，可以引用一句主播原话；不要写“点赞投币关注”。
3. 标签：6~{_MAX_TAGS} 个，每个 {_MAX_TAG} 字以内：主播名和常用称呼、这件事的关键词，再加“虚拟主播”“直播切片”这类通用标签。

这位切片员以前的标题：
{examples}

主播：{streamer or "未知"}
现在的标题：{title}

字幕（[秒数] 文本）：
{_subtitle_lines(cues)}

第 {round_index + 1} 次生成{"，请给出和之前不同的标题" if round_index else ""}。
只输出 JSON，不要解释：
{{"titles": ["标题1", "标题2", "标题3"], "description": "简介", "tags": ["标签"]}}"""
        payload = self._ask(prompt, vision=False, max_tokens=3000)
        titles = tuple(dict.fromkeys(
            text for text in (_clean(item, _MAX_TITLE) for item in payload.get("titles") or () if isinstance(item, str)) if text
        ))[:3]
        tags = tuple(dict.fromkeys(
            text.lstrip("#") for text in (_clean(item, _MAX_TAG) for item in payload.get("tags") or () if isinstance(item, str)) if text
        ))[:_MAX_TAGS]
        description = str(payload.get("description") or "").strip()[:_MAX_DESCRIPTION]
        if not titles and not description:
            raise CoverAIError("AI 没给出投稿文案，可以再试一次")
        return SubmissionCopy(titles=titles, description=description, tags=tags)

    def suggest_fixes(
        self,
        cover: bytes,
        *,
        texts: tuple[str, str] = ("", ""),
        title: str = "",
        source: str = "",
        frame: bytes | None = None,
        state: str = "",
        text_layers: Sequence[dict[str, object]] = (),
        scene: Sequence[dict[str, object]] = (),
    ) -> tuple[AIFix, ...]:
        """看成品挑最影响效果的问题，每条只给工具能一键做到的修改；没问题就返回空。

        state 是封面现在的样子（已强调的词、倾斜、外描边、配色、放大倍数），让 AI 别提已经做过的。
        """

        context, headline = texts
        actions = "\n".join(f"- {key}：{text}" for key, text in FIX_ACTIONS.items())
        presets = "、".join(f"{preset.key}（{preset.label}）" for preset in STYLE_PRESETS)
        layer_data = json.dumps(list(text_layers), ensure_ascii=False)
        scene_data = json.dumps(list(scene), ensure_ascii=False)
        prompt = f"""你是 B 站直播切片的封面审稿人。第一张图是准备导出的封面（按首页小图尺寸看）{"；第二张是没加字的原画面" if frame is not None else ""}。
封面主文案是：A「{context}」（可能为空）、B「{headline}」；其它可编辑文字见后面的文字块清单。清单之外画面原有的直播界面、弹幕、视频自带文字不是封面文字，不要评价它们本身。
投稿标题：{title or "未知"}
{f"封面现在：{state}。已经做到的不要再提（比如已经强调过的词、已经倾斜过）。" if state else ""}
根据这张封面的实际内容与风格，给最多 6 个有实际改善的候选方案，按效果从大到小排，稍后会渲染成品复核。
适用于一句标题、多段文字、对话、多人、无人、游戏、物品、抠图和拼图；不要假定一定有人脸、三行字或固定主次。
保留用户写好的正文、手动换行与原有风格。不要使用 rewrite；画面已经清楚时不要机械放大、加描边、换高饱和配色或增加装饰。
有意的文字叠放、贴边、倾斜和装饰穿插可以保留；优先解决实际的难读、主次混乱或无意遮挡。
每个方案组合 1~3 个下面的操作（比如“主文案放大 + 字倾斜 + 加外描边”），改完要在首页小图上一眼看得出变化；几个方案之间要明显不同。这个工具能一键做到的只有这些操作：
{actions}
restyle 时在 preset 里给配色名，只能是：{presets}。
rewrite 时给出新的 A 和 B：只能用投稿标题和字幕里的原话截取、删减、压缩，可以补“的、了、被、到”这类虚词；配合标题要能看懂，优先主播最冲的一句原话。
emphasize 时在 words 里给 1~2 个要换色的词，必须是封面上 A 或 B 里原样出现的片段（2~5 字）。
可调整描边的全部文字块（id、正文及当前颜色）：{layer_data}。
完整对象清单（位置和 bounds 为画布比例，font_size 为导出像素，locked 的对象只能作为避让约束）：{scene_data}。
优先使用 edit_objects，在 edits 中按清单里的 id 精确调整任意文字，包括新建与复制的文字，不依赖 A/B 或创建顺序。
每个 edits 元素可以包含：id；scale（当前大小的 0.65~1.5 倍）；dx/dy（相对当前位置的画布比例，-0.35~0.35）；rotation（目标角度 -30~30）。
文字还可使用 width_scale（行宽额外乘 0.65~1.6）、line_spacing（0.8~1.8）、fill/stroke/outer_stroke/accent（#RRGGBB）、words（正文中原样出现的强调词列表，空列表取消强调）。
outer_stroke 空串可取消外描边；未给出的字段必须保持。只给真正需要改变的参数，不移动锁定对象、不显示隐藏对象、不新增或删除文字。
已有文字关联的 burst-文字ID 装饰会跟随文字几何调整，无须重复调整；若装饰锁定则保留原位。其它图片、贴纸、形状可单独移动缩放旋转。
背景取景默认保留；确有必要且背景未锁定时才使用 zoom_in/zoom_out。多块自定义文字请用 edits 分别排放，避免整体移到上下方造成拥挤。
outline_colors 时在 outlines 里按 id 指定修改。结合填色、背景明暗和整体色调挑描边色：可以用深蓝、深紫、暖棕、米白等协调颜色，避免所有文字一律套纯黑边，同时保证小图清晰、强调词也读得清。
已有描边或外描边仍可优化配色。stroke 必须是 #RRGGBB；outer_stroke 为 #RRGGBB 表示该外描边色，为空串表示去掉外描边，为 null 或省略表示保留现状。只改给定文字块的描边，不改填色、字号、位置、文案。
已经很好、改了也不会更好就返回空列表，不要为了凑数提建议；不要提这些操作做不到的事。
只输出 JSON，不要解释：
{{"fixes": [{{"issue": "这个方案具体改善什么（20 字内）", "actions": ["edit_objects"], "edits": [{{"id": "清单中的对象ID", "scale": 1.1, "dy": -0.03}}], "outlines": []}}]}}"""
        images = (cover, *((frame,) if frame is not None else ()))
        payload = self._ask(prompt, vision=True, images=images, max_tokens=3000)
        fixes: list[AIFix] = []
        signatures = set()
        allowed_layers = {item["id"] for item in text_layers if isinstance(item.get("id"), str)}
        for item in payload.get("fixes") or ():
            if not isinstance(item, dict):
                continue
            raw = item.get("actions") or item.get("action") or ()
            raw = [raw] if isinstance(raw, str) else raw if isinstance(raw, (list, tuple)) else ()
            new_context = _clean(item.get("context"), _MAX_CONTEXT)
            new_headline = _clean(item.get("headline"), _MAX_HEADLINE)
            words = _emphasis(item.get("words"), context, headline)
            preset = str(item.get("preset") or "").strip()
            outlines = _outline_changes(item.get("outlines"), allowed_layers)
            edits = object_changes(item.get("edits"), scene or text_layers)
            actions: list[str] = []
            for value in raw:
                action = str(value).strip().lower()
                if action not in FIX_ACTIONS or action in actions or _OPPOSITE.get(action) in actions:
                    continue
                # 换文案同样逐字校验，不能编；强调词必须是封面上已有的字；配色只能是预设里的。
                if action == "rewrite" and (not new_headline or not from_source(new_context + new_headline, source or title)):
                    continue
                if action == "emphasize" and not words or action == "restyle" and preset not in _PRESET_LABELS:
                    continue
                if action == "outline_colors" and not outlines:
                    continue
                if action == "edit_objects" and not edits:
                    continue
                actions.append(action)
            if not actions:
                continue
            fix = AIFix(
                issue=_clean(item.get("issue"), 40), actions=tuple(actions[:_MAX_FIX_ACTIONS]),
                context=new_context if "rewrite" in actions else "", headline=new_headline if "rewrite" in actions else "",
                words=words if "emphasize" in actions else (), preset=preset if "restyle" in actions else "",
                outlines=outlines if "outline_colors" in actions else (),
                edits=edits if "edit_objects" in actions else (),
            )
            signature = (fix.actions, fix.context, fix.headline, fix.words, fix.preset, fix.outlines, fix.edits)
            if signature not in signatures:
                signatures.add(signature)
                fixes.append(fix)
        return tuple(fixes[:6])

    def review_fixes(self, sheet: bytes, count: int, *, title: str = "", state: str = "") -> AIChoice:
        """原图和候选一起审阅；只保留确有改善且彼此不同的成品。"""

        prompt = f"""封面修改成品复核：总图中编号 1 是用户原图，编号 2~{count + 1} 是修改后的候选。
投稿标题：{title}。对象与原有状态：{state}。
只挑出比原图更好读、层级更清楚或构图更协调的候选，最多 3 张，并按值得采用的程度排序。
按当前内容判断视觉重点，兼容无人、多人、游戏、物品、拼图和任意数量的文字。保留原有风格与有意叠放。
淘汰无意截字、新增挤压遮挡、重点变小难读、颜色变脏、装饰错位的方案；很小的改色或几乎相同的方案不必选。
多个候选只有轻微配色差别时只留更好的一张；每张必须说明相对原图的具体改善。不要为了凑数挑选。
全部不如原图或无法确认改善时返回空 picks，并在 rejected 说明；不能仅因变化大就认定更好。
只输出 JSON：{{"picks": [{{"index": 2, "reason": "相对原图的具体改善（30字内）"}}], "rejected": "未选方案的原因或原图更合适的理由"}}"""
        payload = self._ask(prompt, vision=True, images=(sheet,), max_tokens=1600)
        if not isinstance(payload.get("picks"), list):
            raise CoverAIError("AI 没有完成修改效果复核，请重试")
        picks = []
        seen = set()
        for item in payload["picks"]:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            reason = _clean(item.get("reason"), 60)
            if isinstance(index, bool) or not isinstance(index, int) or not 2 <= index <= count + 1 or index in seen or not reason:
                continue
            picks.append(AIPick("", index - 2, reason))
            seen.add(index)
        return AIChoice(tuple(picks[:3]), _clean(payload.get("rejected"), 120))
