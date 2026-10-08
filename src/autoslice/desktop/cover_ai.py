"""封面 AI：读字幕找爆点、写 A/B 文案；看画面出三套方案；看图点评。

AI 只在用户点击时运行。文案必须出自标题和字幕原文（逐字校验，允许删减和
调序，不许加词）；方案只是“用哪组文案、哪种排法、哪套配色”的选择，由本地
排版引擎生成可编辑的文档，避让人物、溢出这些仍由本地规则保证。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image

from autoslice.llm.transport import call_llm, extract_json_payload

from .ai_settings import AISettings, read_ai_settings
from .cover_copy import BasicCoverCopy
from .cover_style import STYLE_PRESETS
from .cover_works import CoverWork, describe_composition
from .foundation import DesktopStorage

# 每种排法可选的位置。
PLACES = {"stack": ("top", "bottom"), "headline": ("top", "bottom"), "slot": ("left", "right", "top", "bottom")}
LAYOUTS = {
    "split": "上下分置：A 放上缘一行，B 放下缘，人物留在中间",
    "stack": "标题在上：B 大标题、A 作小字紧跟其下，整组放在画面更空的上缘或下缘",
    "slot": "侧边：A、B 放到画面较空的一侧，人物更完整",
    "headline": "大字：只留 B，放大成一条宽带，首页小图也看得清",
}
DIRECTIONS = ("稳妥", "换个构图", "大胆一点")
# 字幕太长时只取这么多字给模型；够覆盖一条切片。
_MAX_SUBTITLE_CHARS = 9000
_MAX_CONTEXT = 16
_MAX_HEADLINE = 24
_WORD_RE = re.compile(r"[一-鿿A-Za-z0-9]")
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF☀-➿️‍]")


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
    direction: str
    copy: AICopy
    layout: str
    preset: str
    reason: str
    # AI 看图选的位置：大字、标题在上用 top/bottom，侧边用 left/right；上下分置不用。
    place: str = ""


@dataclass(frozen=True, slots=True)
class AINote:
    issue: str
    suggestion: str


def from_source(text: str, source: str) -> bool:
    """文案的每个字都在原文里出现过：允许删减和调序，不许加词。"""

    allowed = set(source)
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
    rows = [
        f"- A「{item.context}」 B「{item.headline}」 构图：{describe_composition(item.composition)}"
        for item in works if item.headline
    ]
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
        """读标题和整份字幕：标出爆点句，给 4 组角度不同的 A/B 文案。"""

        source = title + "\n" + "\n".join(text for _start, _end, text in cues)
        prompt = f"""你是 B 站直播切片的封面文案编辑。根据投稿标题和这条切片的校对字幕，找出这条视频的爆点，并写封面文字。

规则：
1. 封面最多两块字：A 是让 B 成立的最小上下文，可以为空，要短（不超过 10 个字）；B 是最该被看到的原话、结果、反差或梗，优先 4~14 个字。
2. 只能从标题和字幕原文里截取、删减或调换语序，不许加原文没有的字，不写营销话术，不编造事实。
3. “告诉你”“揭秘”“原因竟然是这个”这类预告或包装语不能当 B。
4. 给 4 组不同角度的方案：原话、结果、反差、悬念，各一组；不同组的 B 不要相同。
5. 参考这位切片员最近做过的封面文字的长短和口吻，但不要照抄。

主播：{streamer or "未知"}
投稿标题：{title}

最近的封面文字：
{_work_examples(recent)}

字幕（[秒数] 文本）：
{_subtitle_lines(cues)}

第 {round_index + 1} 次生成{"，请给出和之前不同的角度" if round_index else ""}。
只输出 JSON，不要解释：
{{"highlight": {{"quote": "爆点字幕原话", "start": 秒数, "end": 秒数, "reason": "为什么是爆点（20 字内）"}},
 "copies": [{{"context": "A，可为空", "headline": "B", "angle": "原话|结果|反差|悬念", "reason": "一句话说明（20 字内）"}}]}}"""
        payload = self._ask(prompt, vision=False)
        copies: list[AICopy] = []
        for item in payload.get("copies") or ():
            if not isinstance(item, dict):
                continue
            copy = AICopy(
                context=_clean(item.get("context"), _MAX_CONTEXT),
                headline=_clean(item.get("headline"), _MAX_HEADLINE),
                angle=_clean(item.get("angle"), 8),
                reason=_clean(item.get("reason"), 40),
            )
            # 逐字校验：有原文里没有的字就丢掉这组。
            if not copy.headline or not from_source(copy.context + copy.headline, source):
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
            raise CoverAIError("AI 给的文案都不是出自原文，已全部丢弃；可以再试一次")
        return AIAnalysis(highlight=highlight, copies=tuple(copies))

    def design(
        self,
        frame: bytes,
        analysis: AIAnalysis,
        *,
        recent: Sequence[CoverWork] = (),
        recent_thumbnails: Sequence[bytes] = (),
        round_index: int = 0,
    ) -> tuple[AISchemeIdea, ...]:
        """看画面和最近的封面，给“稳妥 / 换个构图 / 大胆一点”三套方案。"""

        copies = "\n".join(
            f"{index}. A「{item.context}」 B「{item.headline}」（{item.angle}）"
            for index, item in enumerate(analysis.copies)
        )
        layouts = "\n".join(f"- {key}：{text}" for key, text in LAYOUTS.items())
        presets = "\n".join(
            f"- {item.key}：{item.label}（B {item.fill}，A {item.context_fill or item.fill}）" for item in STYLE_PRESETS
        )
        recent_text = "\n".join(
            f"- 构图：{describe_composition(item.composition)}；配色：{item.palette}" for item in recent[:6]
        ) or "（还没有作品）"
        prompt = f"""你是 B 站直播切片的封面设计师。第一张图是这条切片选好的画面{"，后面几张是这位切片员最近导出的封面" if recent_thumbnails else ""}。
请为这张画面设计三套封面方案：
- 稳妥：像这位切片员平常的封面，稳定好用；
- 换个构图：和最近的封面在构图上明显不同；
- 大胆一点：更强的对比或更大的字，但仍然要看得清、不挡住人脸和关键信息。

可选文案：
{copies}

可选排法：
{layouts}

可选配色：
{presets}

最近封面的构图和配色：
{recent_text}

看清画面里人物的脸、画面原有的大字、弹幕和杂乱区域，再选排法和位置：
- place 是文字放在哪：标题在上、大字填 top 或 bottom（上缘或下缘），侧边填 left、right、top 或 bottom；上下分置不用填。
- 位置要避开人脸和画面原有的文字；上下分置的 A 一定在上缘、B 在下缘，上缘或下缘有原有大字时不要选上下分置。
第 {round_index + 1} 次生成{"，请给出和之前不同的组合" if round_index else ""}。
只输出 JSON，不要解释：
{{"schemes": [{{"direction": "稳妥|换个构图|大胆一点", "copy": 文案序号, "layout": "排法", "place": "位置", "preset": "配色", "reason": "这套的优点和风险（30 字内）"}}]}}"""
        images = (frame, *recent_thumbnails[:3])
        payload = self._ask(prompt, vision=True, images=images)
        presets = {item.key for item in STYLE_PRESETS}
        ideas: list[AISchemeIdea] = []
        for item in payload.get("schemes") or ():
            if not isinstance(item, dict):
                continue
            try:
                copy = analysis.copies[int(item.get("copy", 0))]
            except (TypeError, ValueError, IndexError):
                copy = analysis.copies[0]
            layout = str(item.get("layout") or "").strip()
            preset = str(item.get("preset") or "").strip()
            direction = str(item.get("direction") or "").strip()
            if layout not in LAYOUTS or preset not in presets:
                continue
            place = str(item.get("place") or "").strip().lower()
            ideas.append(AISchemeIdea(
                direction=direction if direction in DIRECTIONS else DIRECTIONS[min(len(ideas), 2)],
                copy=copy, layout=layout, preset=preset, reason=_clean(item.get("reason"), 60),
                place=place if place in PLACES.get(layout, ()) else "",
            ))
        if not ideas:
            raise CoverAIError("AI 没给出可用的方案（排法或配色不在可选范围内），可以再试一次")
        return tuple(ideas[:3])

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
