"""封面 AI：读标题和字幕写 A/B 文案；看候选成品挑三张；看图点评。

AI 只在用户点击时运行。文案必须出自标题和字幕原文（逐字校验，允许删减、
调序和补少量虚词，不许加实词）。方案不让 AI 给坐标：本地排版引擎先排出十几
张候选（不同文案、排法位置、取景、配色）并渲染成缩略图总图，看图模型像审
稿一样从成品里挑——压脸、压住画面原有的字、看不懂的都淘汰。
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
# 只看封面能不能看懂（模型自评 1~5），低于这个分的文案不要。
_MIN_CLARITY = 4
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
    angle: str
    reason: str

    def as_basic(self) -> BasicCoverCopy:
        return BasicCoverCopy(context=self.context, headline=self.headline)


@dataclass(frozen=True, slots=True)
class AIAnalysis:
    highlight: AIHighlight | None
    copies: tuple[AICopy, ...]


@dataclass(frozen=True, slots=True)
class AISchemeIdea:
    """一张候选封面怎么排：文案、排法、配色、文字位置、取景放大倍数。"""

    direction: str
    copy: AICopy
    layout: str
    preset: str
    reason: str
    # 大字、标题在上用 top/bottom，侧边用 left/right/top/bottom；上下分置不用。
    place: str = ""
    # 大于 1 时把画面放大到人物，裁掉两侧的弹幕栏和直播界面。
    zoom: float = 1.0


@dataclass(frozen=True, slots=True)
class AIPick:
    direction: str
    index: int
    reason: str


@dataclass(frozen=True, slots=True)
class AIChoice:
    picks: tuple[AIPick, ...]
    rejected: str


# 画面四周的区域：上缘、下缘、左侧、右侧。
REGIONS = ("top", "bottom", "left", "right")


@dataclass(frozen=True, slots=True)
class AIFrameNotes:
    """看图模型看原画面（没加字）的结论：哪些区域有画面自带的文字、弹幕或界面。"""

    busy: frozenset[str]
    note: str


@dataclass(frozen=True, slots=True)
class AINote:
    issue: str
    suggestion: str


def from_source(text: str, source: str) -> bool:
    """文案的每个实词字都在原文里出现过：允许删减、调序和补少量虚词，不许加新内容。"""

    allowed = set(source) | _FUNCTION_WORDS
    return all(character in allowed for character in text if _WORD_RE.match(character))


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


def contact_sheet(thumbnails: Sequence[bytes]) -> bytes:
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
    cell_width = max(item.width for item in pictures)
    cell_height = max(item.height for item in pictures)
    rows = (len(pictures) + _SHEET_COLUMNS - 1) // _SHEET_COLUMNS
    columns = min(_SHEET_COLUMNS, len(pictures))
    sheet = Image.new("RGB", (columns * (cell_width + gap) + gap, rows * (cell_height + gap) + gap), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=30)
    for index, picture in enumerate(pictures):
        x = gap + (index % _SHEET_COLUMNS) * (cell_width + gap)
        y = gap + (index // _SHEET_COLUMNS) * (cell_height + gap)
        sheet.paste(picture, (x, y))
        label = str(index + 1)
        box = draw.textbbox((0, 0), label, font=font)
        draw.rectangle((x, y, x + box[2] + 14, y + box[3] + 10), fill=(220, 30, 30))
        draw.text((x + 7, y + 3), label, fill=(255, 255, 255), font=font)
    buffer = io.BytesIO()
    sheet.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


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
    ) -> AIAnalysis:
        """读标题和整份字幕：标出爆点句，给几组只看封面就能懂的 A/B 文案。"""

        source = title + "\n" + "\n".join(text for _start, _end, text in cues)
        prompt = f"""你是 B 站直播切片的封面文案编辑。根据投稿标题和这条切片的校对字幕，写封面上的字。

最重要的标准：观众只看封面（再加上投稿标题）就能明白发生了什么、为什么有意思。
1. 封面最多两块字。B 是爆点：一句完整、能独立看懂的话，有主语或明确的事件、结果、反差，6~16 个字。A 交代背景或主语，可以为空，不超过 10 个字，分量要比 B 轻。
2. 优先用投稿标题里的爆点——标题是切片员自己写的总结；字幕用来确认爆点，或找到更有力的原话。
3. 只能从标题和字幕里截取、删减、压缩，可以补“的、了、被、到”这类虚词让句子通顺；不许加原文没有的事实和词，不写营销话术。
4. 不要只有看过视频才懂的碎片，比如「交个备用机」「4个emoji」「ins简介改了」：没头没尾，观众不知道在说谁、发生了什么。
5. “告诉你”“揭秘”“原因竟然是这个”这类预告语不能当 B。

好的例子（这位切片员以前的标题 → 封面字）：
- 「泽音一个晚上居然被冲了万楼？！原因居然是这个？！」→ A「一个晚上」 B「被冲了万楼？！」
- 「逆天音姐听到公主说要用脚惩罚她时高兴的发出了鸟叫」→ A「听到要用脚惩罚」 B「高兴的发出了鸟叫」
- 「刚上播不清醒的音音 嘴滑承认18岁是虚假宣传」→ A「刚上播不清醒」 B「18岁是虚假宣传？！」

这位切片员最近导出的封面文字（参考长短和口吻，不要照抄）：
{_work_examples(recent)}

主播：{streamer or "未知"}
投稿标题：{title}

字幕（[秒数] 文本）：
{_subtitle_lines(cues)}

给 4 组，角度尽量不同（原话、结果、反差、悬念）；每组自评“只看封面能不能看懂”1~5 分，低于 4 分的不要给。
第 {round_index + 1} 次生成{"，请给出和之前不同的角度" if round_index else ""}。
只输出 JSON，不要解释：
{{"highlight": {{"quote": "爆点字幕原话", "start": 秒数, "end": 秒数, "reason": "为什么是爆点（20 字内）"}},
 "copies": [{{"context": "A，可为空", "headline": "B", "angle": "原话|结果|反差|悬念", "clarity": 1到5, "reason": "一句话说明（20 字内）"}}]}}"""
        payload = self._ask(prompt, vision=False)
        copies: list[AICopy] = []
        for item in payload.get("copies") or ():
            if not isinstance(item, dict):
                continue
            try:
                clarity = float(item.get("clarity", _MIN_CLARITY))
            except (TypeError, ValueError):
                clarity = _MIN_CLARITY
            copy = AICopy(
                context=_clean(item.get("context"), _MAX_CONTEXT),
                headline=_clean(item.get("headline"), _MAX_HEADLINE),
                angle=_clean(item.get("angle"), 8),
                reason=_clean(item.get("reason"), 40),
            )
            # 逐字校验：有原文里没有的实词就丢掉；模型自己都觉得看不懂的也不要。
            if not copy.headline or clarity < _MIN_CLARITY or not from_source(copy.context + copy.headline, source):
                continue
            if all(existing.headline != copy.headline for existing in copies):
                copies.append(copy)
        raw = payload.get("highlight")
        highlight = None
        if isinstance(raw, dict) and str(raw.get("quote") or "").strip():
            start = _seconds(raw.get("start"))
            highlight = AIHighlight(
                quote=_clean(raw.get("quote"), 60), start=start,
                end=max(start, _seconds(raw.get("end"))), reason=_clean(raw.get("reason"), 40),
            )
        if not copies:
            raise CoverAIError("AI 给的文案都不合格（不是出自原文或看不懂），已全部丢弃；可以再试一次")
        return AIAnalysis(highlight=highlight, copies=tuple(copies))

    def inspect(self, frame: bytes) -> AIFrameNotes:
        """看没加字的原画面：上缘、下缘、左侧、右侧哪里有画面自带的文字、弹幕或直播界面。"""

        prompt = """这是一帧直播切片画面，还没有加封面文字。
判断画面的上缘（顶部约四分之一）、下缘（底部约四分之一）、左侧、右侧，哪些区域有画面自带的文字、弹幕、直播界面、字幕条或水印——封面大字放在那里会和它们叠在一起、显得乱。
只输出 JSON，不要解释：
{"busy": ["top|bottom|left|right 中有原有文字或界面的区域"], "note": "画面原有文字和界面在哪（30 字内）"}"""
        payload = self._ask(prompt, vision=True, images=(frame,), max_tokens=2000)
        busy = frozenset(str(item).strip().lower() for item in payload.get("busy") or () if str(item).strip().lower() in REGIONS)
        return AIFrameNotes(busy=busy, note=_clean(payload.get("note"), 60))

    def choose(
        self,
        sheet: bytes,
        count: int,
        *,
        title: str = "",
        frame: bytes | None = None,
        recent_thumbnails: Sequence[bytes] = (),
        round_index: int = 0,
    ) -> AIChoice:
        """看候选总图（编号从 1 开始）挑三张：稳妥、换个构图、大胆一点；不合格的一票否决。"""

        extra = []
        if frame is not None:
            extra.append("第二张是没加字的原画面，用来分辨哪些字是画面原有的、哪些是封面加的")
        if recent_thumbnails:
            extra.append("再后面几张是这位切片员最近导出的封面")
        prompt = f"""你是 B 站直播切片的资深封面编辑。第一张图里是同一条切片的 {count} 个候选封面，左上角红底白字是编号，都按首页小图的大小排着{"；" + "；".join(extra) if extra else ""}。
投稿标题：{title or "未知"}

请挑出三张：
- 稳妥：最好用、最不会出错的一张；
- 换个构图：和稳妥的构图明显不同、同样能用的一张{"，也尽量和最近的封面不一样" if recent_thumbnails else ""}；
- 大胆一点：更抓眼（字更大、对比更强或取景更紧），但仍然清楚。

一票否决，出现任何一条都不能选：
1. 封面文字压住人脸或主要人物；
2. 封面文字（彩色描边的大字）和画面原有的文字、弹幕或直播界面叠在一起，哪怕只叠一部分；
3. 文案只看封面看不懂，不知道在说谁、发生了什么；
4. 在小图上字太小、读不清。
合格的不够三张就少选，全部不合格就返回空列表。第 {round_index + 1} 次挑选。
只输出 JSON，不要解释：
{{"picks": [{{"direction": "稳妥|换个构图|大胆一点", "index": 编号, "reason": "为什么选它（25 字内）"}}], "rejected": "其余候选被淘汰的主要原因（30 字内）"}}"""
        images = (sheet, *((frame,) if frame is not None else ()), *recent_thumbnails[:2])
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

    def critique(
        self, cover: bytes, *, texts: Sequence[str] = (), recent_thumbnails: Sequence[bytes] = (),
    ) -> tuple[AINote, ...]:
        """按首页小图的尺寸看一眼成品：读不读得清、挡没挡脸、主次、和最近的像不像。"""

        added = "、".join(f"「{text}」" for text in texts if text.strip()) or "（看不出）"
        prompt = f"""你是 B 站直播切片的封面审稿人。第一张图是准备导出的封面（按首页小图尺寸看）{"，后面几张是这位切片员最近导出的封面" if recent_thumbnails else ""}。
封面上加的字只有：{added}。画面里原本就有的直播界面、弹幕、视频自带文字不是封面文字，不要评价它们本身；封面文字压住它们或被它们干扰才指出。
从这几点挑最重要的问题，最多 4 条，没有问题就返回空列表：
1. 首页小图上文字读不读得清；
2. 有没有挡住人脸或画面关键信息；
3. 上下两块字主次清不清楚；
4. 和最近的封面是不是太像。
每条给一个具体可做的修改建议。只输出 JSON：
{{"notes": [{{"issue": "问题（20 字内）", "suggestion": "怎么改（30 字内）"}}]}}"""
        payload = self._ask(prompt, vision=True, images=(cover, *recent_thumbnails[:3]), max_tokens=3000)
        notes = []
        for item in payload.get("notes") or ():
            if isinstance(item, dict) and str(item.get("issue") or "").strip():
                notes.append(AINote(_clean(item.get("issue"), 40), _clean(item.get("suggestion"), 60)))
        return tuple(notes[:4])
