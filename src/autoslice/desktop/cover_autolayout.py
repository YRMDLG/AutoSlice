"""自动排版与快速方案：文字槽位代价、上下分置/标题在上/侧边、方案生成与套用、比例同步。"""

from __future__ import annotations

import io
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image

from autoslice_cover.composition import best_crop_focus, region_cost, saliency_map
from autoslice_cover.document_layout import Box, background_box, clamp_background_scale
from autoslice_cover.document_render import compose_document

from .cover_ai import AICopy, AIFrameNotes, AISchemeIdea, Frac
from .cover_copy import BasicCoverCopy
from .cover_draft import CoverDraft
from .cover_framing import SIDE_ZONES, subject_crop, text_hit
from .cover_layout import (
    canvas_size,
    document_layers,
    fitted_font_size,
    image_size,
    overlay_geometry,
    text_layout,
)
from .cover_model import (
    AssetRef,
    BackgroundObject,
    CoverDocument,
    ImageObject,
    Rect,
    ShapeObject,
    StickerObject,
    TextObject,
    object_for_profile,
    object_override_payload,
    resize_text_style,
    set_object_visible,
    set_profile_override,
    text_override_payload,
    update_text_object,
)
from .cover_style import (
    STYLE_PRESETS,
    StylePreset,
)
from .cover_works import CoverWork, composition_signature, palette_signature

# 默认文字槽位：(x, y, 宽度)；高度统一 0.32，按比例分别给出。
_TEXT_SLOTS = {
    "4x3": ((0.06, 0.14, 0.48, "left"), (0.46, 0.14, 0.48, "right"), (0.08, 0.05, 0.72, "top"), (0.08, 0.60, 0.72, "bottom")),
    "16x9": ((0.06, 0.14, 0.44, "left"), (0.50, 0.14, 0.44, "right"), (0.08, 0.05, 0.66, "top"), (0.08, 0.60, 0.66, "bottom")),
}

_TEXT_SLOT_HEIGHT = 0.32

# 上下分置布局：参考真实投稿封面，A/B 各占上下缘一条宽带。
_SPLIT_X = 0.06

_SPLIT_WIDTH = 0.88

_SPLIT_HEIGHT = 0.22

_SPLIT_TOP = 0.05

_SPLIT_BOTTOM = 0.70

_SPLIT_FONT_A = 136

_SPLIT_FONT_B = 150

# 主次：自动排版时 A 的字号不超过 B 的这个比例，再小也不低于下限。
_CONTEXT_RATIO = 0.65

_CONTEXT_MIN_FONT = 40


def _context_cap(headline_size: int) -> int:
    """A 只补背景：字号压在 B 的 65% 以内，主次一眼可辨。"""

    return max(_CONTEXT_MIN_FONT, round(headline_size * _CONTEXT_RATIO))


# 槽位代价表里上下两条宽带的键；不参与单槽位选择。
_SPLIT_TOP_KEY = "split-top"

_SPLIT_BOTTOM_KEY = "split-bottom"

# “标题在上”构图：大标题两行高、小字一行高。
_STACK_HEAD_HEIGHT = 0.24

_STACK_SUB_HEIGHT = 0.11

# 只有 B 时的宽带高度：两行大字。
_BAND_HEIGHT = 0.30

# 方案的初始请求字号：自动排版在槽位里只缩不放，所以先给足。
_SCHEME_FONT_A = 73

_SCHEME_FONT_B = 104

_SCHEME_FONT_BIG = 168

# “换一批”轮换的配色；第一个方案保留当前（记忆）样式。
_SCHEME_PRESETS = ("duo", "yellow-red", "yellow-purple", "white", "classic")

# AI 候选的排法和位置；前一轮用原画面，后一轮放大到人物、裁掉两侧杂物。
_CANDIDATE_ARRANGEMENTS = (
    ("split", ""), ("lead", "bottom"), ("headline", "bottom"), ("slot", "left"),
    ("lead", "top"), ("slot", "right"), ("headline", "top"),
)
# 每种排法会用到画面的哪些区域（上下分置的 A 在上缘、B 在下缘；侧边槽位在画面中上部，也算上缘）。
_ARRANGEMENT_REGIONS = {
    ("split", ""): frozenset({"top", "bottom"}),
    ("lead", "bottom"): frozenset({"bottom"}),
    ("headline", "bottom"): frozenset({"bottom"}),
    ("slot", "left"): frozenset({"left", "top"}),
    ("lead", "top"): frozenset({"top"}),
    ("slot", "right"): frozenset({"right", "top"}),
    ("headline", "top"): frozenset({"top"}),
}
_CANDIDATE_ZOOM = 1.35
# 候选成品的字被界面或脸盖住的比例不超过这个算合格；合格的不够这么多张时放宽，按压得少的补齐。
_TEXT_HIT_OK = 0.06
_MIN_CANDIDATES = 4
# 第一轮候选只用最好的几句文案（标题那句 + AI 排在前面的），换着配到不同构图上。
_TOP_COPIES = 3
# 字放上方比放下方多算的代价：差不多干净时优先放下方（切片封面最常见）。
_TOP_ZONE_BIAS = 0.05
# 拉近时画面上下方向的锚点：原取景高度的这个位置留在画面中线。
_ZOOM_ANCHOR_Y = 0.42

@dataclass(frozen=True)
class CoverScheme:
    """一套可直接套用的本地方案：文案、排版、配色一起换；参考旧网页端“推荐排版”。"""

    key: str
    label: str
    reason: str
    document: CoverDocument

def primary_copy_ids(document: CoverDocument) -> dict[str, str]:
    """每个角色的第一个文本框是 A/B 主文案；复制或新建的文本框不参与换文案与方案。"""

    ids: dict[str, str] = {}
    for item in document.objects:
        if isinstance(item, TextObject):
            ids.setdefault(item.copy_role, item.id)
    return ids


def ensure_context_text(document: CoverDocument) -> CoverDocument:
    """文档里没有 A 块时，照着 B 的样式补一个空的隐藏 A 块：每个比例都放在 B 的上方，字号约为 B 的七成。

    默认文案只有 B 时文档里没有 A 块；之后换上带 A 的文案（方案、换一版）需要有地方放。
    """

    ids = primary_copy_ids(document)
    if "A" in ids or "B" not in ids:
        return document
    headline = next(item for item in document.objects if item.id == ids["B"])
    used = {item.id for item in document.objects}
    object_id = "copy-a" if "copy-a" not in used else next(
        f"copy-a-{index}" for index in range(2, 1000) if f"copy-a-{index}" not in used
    )

    def above(item: TextObject) -> TextObject:
        size = max(_CONTEXT_MIN_FONT, round(item.style.font_size * 0.7))
        return replace(
            item, id=object_id, copy_role="A", text="", visible=False,
            style=resize_text_style(item.style, size),
            transform=replace(item.transform, y=max(0.03, item.transform.y - 0.13)),
        )

    z_index = max((item.z_index for item in document.objects), default=0) + 1
    document = replace(document, objects=(*document.objects, replace(above(headline), z_index=z_index)))
    for key in document.profiles:
        current = object_for_profile(document, headline.id, key)
        if isinstance(current, TextObject):
            document = set_profile_override(document, key, replace(above(current), z_index=z_index))
    return document


def replace_copy(document: CoverDocument, copy: BasicCoverCopy, profile_key: str) -> CoverDocument:
    """只换 A/B 文字（连同强调词）：位置、字号和行宽沿用当前排版；原封面只有 B 时先在 B 上方补一个 A 块。"""

    if copy.context.strip():
        document = ensure_context_text(document)
    primary = set(primary_copy_ids(document).values())
    for item in document.objects:
        if not isinstance(item, TextObject) or item.id not in primary:
            continue
        value = copy.context if item.copy_role == "A" else copy.headline
        current = object_for_profile(document, item.id, profile_key)
        current = current if isinstance(current, TextObject) else item
        document = update_text_object(
            document, replace(current, text=value, emphasis=copy.emphasis_in(value)), profile_key=profile_key,
        )
        document = set_object_visible(document, item.id, bool(value.strip()))
    return document


class CoverLayoutService:
    """混入 CoverService；共享 storage、缓存与锁等状态。"""

    @staticmethod
    def _text_slot_costs(
        image_path: str | Path,
        canvas_key: str,
        *,
        focus_x: float,
        focus_y: float,
        scale: float,
    ) -> dict[str, tuple[float, float, float]] | None:
        """各文字槽位的代价：盖住人脸 > 杂乱细节 > 字幕带；返回 {槽位: (代价, x, y)}。"""

        saliency = saliency_map(image_path)
        if saliency is None:
            return None
        try:
            with Image.open(image_path) as source:
                source_size = source.size
        except OSError:
            return None
        canvas = canvas_size(canvas_key)
        frame = background_box(source_size, canvas, scale=scale, focus_x=focus_x, focus_y=focus_y)
        scored: dict[str, tuple[float, float, float]] = {}
        for order, (x, y, width, name) in enumerate(_TEXT_SLOTS.get(canvas_key, _TEXT_SLOTS["4x3"])):
            # 画布槽位换算到源图归一化坐标，与当前取景一致。
            box = (
                (x * canvas[0] - frame.left) / frame.width,
                (y * canvas[1] - frame.top) / frame.height,
                ((x + width) * canvas[0] - frame.left) / frame.width,
                ((y + _TEXT_SLOT_HEIGHT) * canvas[1] - frame.top) / frame.height,
            )
            scored[name] = (region_cost(saliency, box) + order * 0.002, x, y)
        # 上下分置的两条宽带，与单槽位比较；不参与单槽位选择。
        for name, top in ((_SPLIT_TOP_KEY, _SPLIT_TOP), (_SPLIT_BOTTOM_KEY, _SPLIT_BOTTOM)):
            scored[name] = (region_cost(saliency, (
                (_SPLIT_X * canvas[0] - frame.left) / frame.width,
                (top * canvas[1] - frame.top) / frame.height,
                ((_SPLIT_X + _SPLIT_WIDTH) * canvas[0] - frame.left) / frame.width,
                ((top + _SPLIT_HEIGHT) * canvas[1] - frame.top) / frame.height,
            )), _SPLIT_X, top)
        return scored

    @classmethod
    def _best_text_slot(
        cls,
        image_path: str | Path,
        canvas_key: str,
        *,
        focus_x: float,
        focus_y: float,
        scale: float,
    ) -> tuple[float, float] | None:
        costs = cls._text_slot_costs(image_path, canvas_key, focus_x=focus_x, focus_y=focus_y, scale=scale)
        if not costs:
            return None
        _cost, x, y = min(value for name, value in costs.items() if name not in {_SPLIT_TOP_KEY, _SPLIT_BOTTOM_KEY})
        return x, y

    def suggest_text_position(
        self,
        image_path: str | Path,
        draft: CoverDraft,
        *,
        canvas_key: str = "4x3",
    ) -> tuple[float, float]:
        """在少量构图槽位中选择较清爽的文字区域。"""

        position = self._best_text_slot(
            image_path, canvas_key,
            focus_x=draft.background_x, focus_y=draft.background_y, scale=draft.background_scale,
        )
        return position if position is not None else (draft.text_x, draft.text_y)

    @staticmethod
    def warm_composition(image_path: str | Path) -> None:
        saliency_map(image_path)

    @staticmethod
    def _centered_focus(
        source_size: tuple[int, int], canvas: tuple[int, int], zoom: float, center: tuple[float, float],
    ) -> tuple[float, float]:
        """放大后让主体落在画面中间的 focus（与 background_box 的 focus 同义）。"""

        drawn = background_box(source_size, canvas, scale=zoom)

        def axis(value: float, extent: float, size: float) -> float:
            span = extent - size
            if abs(span) < 0.5:
                return 0.5
            return max(0.0, min(1.0, (extent / 2 - value * size) / span))

        return axis(center[0], canvas[0], drawn.width), axis(center[1], canvas[1], drawn.height)

    @classmethod
    def _crop_framing(cls, source_size: tuple[int, int], canvas: tuple[int, int], crop: Frac) -> tuple[float, float, float]:
        """画布比例的裁切框（源图比例坐标）换算成底图放大倍数和 focus。"""

        x0, y0, x1, y1 = crop
        base = max(canvas[0] / max(1, source_size[0]), canvas[1] / max(1, source_size[1]))
        scale = clamp_background_scale(canvas[0] / (base * max(1.0, (x1 - x0) * source_size[0])))
        focus_x, focus_y = cls._centered_focus(source_size, canvas, scale, ((x0 + x1) / 2, (y0 + y1) / 2))
        return scale, focus_x, focus_y

    def apply_auto_layout(
        self, document: CoverDocument, image_path: str | Path, *, mode: str = "auto", position: str = "",
        zoom: float = 1.0, notes: AIFrameNotes | None = None, framing: str = "", reframe: bool = True,
    ) -> CoverDocument:
        """新底图的默认构图：两个比例分别保住主体、给 A/B 找空区。

        mode="split" 强制上下分置（A 上缘、B 下缘；只有 B 时占代价更低的一条宽带），
        mode="stack" 大标题在上、A 作小字紧跟其下，整组放在更空的上缘或下缘；
        mode="lead" 同样叠在一起，但 A 作小字引子在上、大标题在下（AI 文案 A 是交代、B 是结果，按读的顺序排），
        mode="slot" 强制放进最空的单侧槽位。
        只排 A/B 主文案；用户新建或复制的文本框不动。
        position 指定文字放在哪（top/bottom/left/right），给了就按它放。
        zoom > 1 时把画面放大到主体，裁掉两侧的弹幕栏和直播界面。
        notes 是看图标出的脸和界面框，framing（loose 带上半身 / tight 特写）给了就按脸裁切，字带避开界面，
        优先于 zoom。
        reframe=False 时只重排 A/B，底图取景保持现状（用户或 AI 调好的放大、平移不动）。
        """

        saliency = saliency_map(image_path)
        background = next((item for item in document.objects if isinstance(item, BackgroundObject)), None)
        primary = set(primary_copy_ids(document).values())
        texts = [item for item in document.objects if isinstance(item, TextObject) and item.id in primary]
        has_context = any(item.copy_role == "A" and item.visible and item.text.strip() for item in texts)
        profiles = dict(document.profiles)
        source_size = None
        if notes is not None and framing or zoom > 1.0 and saliency is not None:
            try:
                with Image.open(image_path) as source:
                    source_size = source.size
            except OSError:
                source_size = None
        # 4:3 是主画布：先定 4:3 的布局方式，16:9 跟随上下分置，避免另一比例压脸。
        main_split = False
        ordered = sorted(document.profiles.items(), key=lambda item: item[0] != "4x3")
        for key, profile in ordered:
            overrides = dict(profile.overrides)
            focus_x, focus_y, scale = 0.5, 0.5, 1.0
            if background is not None and not reframe:
                current = object_for_profile(document, background.id, key)
                current = current if isinstance(current, BackgroundObject) else background
                focus_x, focus_y, scale = current.pan_x, current.pan_y, current.scale
            elif background is not None:
                current = object_for_profile(document, background.id, key)
                current = current if isinstance(current, BackgroundObject) else background
                if saliency is not None and current.fit_mode == "cover":
                    focus_x, focus_y = best_crop_focus(saliency, profile.width / max(1, profile.height))
                scale = current.scale if saliency is None else 1.0
                found = None
                if framing and notes is not None and source_size is not None and current.fit_mode == "cover":
                    # 字放哪侧：上下分置（有 A）两侧都要；只有一块字时按指定位置。
                    zone = "" if mode == "split" and has_context else position
                    found = subject_crop(source_size, (profile.width, profile.height), notes, zone, framing)
                if found is not None:
                    scale, focus_x, focus_y = self._crop_framing(source_size, (profile.width, profile.height), found[0])
                elif source_size is not None and zoom > 1.0 and current.fit_mode == "cover":
                    # 在原取景上拉近：以原画面中心为准、略偏上（脸通常在上半部），不去猜人物在哪——
                    # 弹幕栏里的头像会把肤色重心拉偏。
                    canvas = (profile.width, profile.height)
                    shown = background_box(source_size, canvas, focus_x=focus_x, focus_y=focus_y)
                    center = (
                        (canvas[0] / 2 - shown.left) / shown.width,
                        (canvas[1] * _ZOOM_ANCHOR_Y - shown.top) / shown.height,
                    )
                    scale = zoom
                    focus_x, focus_y = self._centered_focus(source_size, canvas, zoom, center)
                updated = replace(current, pan_x=focus_x, pan_y=focus_y, scale=scale)
                overrides[background.id] = object_override_payload(updated)
            costs = self._text_slot_costs(image_path, key, focus_x=focus_x, focus_y=focus_y, scale=scale)
            bands = (costs.pop(_SPLIT_TOP_KEY)[0], costs.pop(_SPLIT_BOTTOM_KEY)[0]) if costs else None
            best = min(costs.items(), key=lambda item: item[1][0]) if costs else None
            if mode == "slot" and costs and position in costs:
                best = (position, costs[position])
            # 人物居中（左右代价接近）时上下宽带通常比单侧槽位更空：代价不更高就用宽带；
            # 人物偏一侧（左右代价悬殊）时文字去另一侧。有 A 比两条宽带的平均，只有 B 比更空的一条。
            band_cost = (sum(bands) / 2 if has_context else min(bands)) if bands else None
            sides = sorted(costs[name][0] for name in ("left", "right")) if costs else None
            one_sided = sides is not None and sides[0] < sides[1] * 0.6
            split = mode == "split" or mode == "auto" and main_split or mode == "auto" and (
                best is not None
                and (
                    band_cost <= best[1][0] + 0.02 and not one_sided
                    or has_context
                    and best[0] in {"top", "bottom"}
                    and costs["top" if best[0] == "bottom" else "bottom"][0] <= best[1][0] * 1.5 + 0.05
                )
            )
            if key == "4x3":
                main_split = split
            canvas_width, canvas_height = canvas_size(key)

            def place(item: TextObject, x: float, y: float, width: float, height: float, *, requested: int, max_lines: int, align: str | None = None) -> TextObject:
                # 字号在槽位内拟合一次后固定下来；之后编辑器按固定字号换行。
                shaped = replace(item, align=align or item.align, transform=replace(item.transform, x=x, y=y, scale=1.0))
                area = Box(x * canvas_width, y * canvas_height, width * canvas_width, height * canvas_height)
                size = fitted_font_size(shaped, area, requested=requested, max_lines=max_lines) if item.text.strip() else requested
                return replace(
                    shaped,
                    rect=Rect(width=width, height=height),
                    wrap=replace(item.wrap, max_width=width, max_lines=8),
                    style=resize_text_style(item.style, size),
                )

            def text_height(item: TextObject) -> float:
                if not item.text.strip():
                    return 0.0
                return text_layout(item, (canvas_width, canvas_height)).area.height / canvas_height

            current_texts = [
                (text, current if isinstance(current := object_for_profile(document, text.id, key), TextObject) else text)
                for text in texts
            ]
            if mode in ("stack", "lead"):
                # 大标题 + 小字（参考账号“晚安小音音 + 一行小字”式封面），整组放在更空的一缘。
                at_top = position == "top" if position in ("top", "bottom") else bands is None or bands[0] <= bands[1]
                cursor = _SPLIT_TOP
                placed: list[tuple[TextObject, TextObject]] = []
                head_size = _SPLIT_FONT_B
                for text, current in sorted(current_texts, key=lambda pair: pair[0].copy_role != "B"):
                    head = text.copy_role == "B"
                    if not head and not (has_context and current.visible):
                        continue
                    updated = place(
                        current, _SPLIT_X, cursor, _SPLIT_WIDTH, _STACK_HEAD_HEIGHT if head else _STACK_SUB_HEIGHT,
                        requested=max(current.style.font_size, _SPLIT_FONT_B) if head else max(40, round(head_size * 0.55)),
                        max_lines=2 if head else 1, align="center",
                    )
                    if head:
                        head_size = updated.style.font_size
                    placed.append((text, updated))
                    cursor += text_height(updated) + 0.015
                if mode == "lead" and len(placed) > 1:
                    # 字号先按“B 定、A 跟”算好，再按 A、B 的顺序从上往下重新叠。
                    placed.reverse()
                    cursor = _SPLIT_TOP
                    for position_index, (text, updated) in enumerate(placed):
                        placed[position_index] = (text, replace(updated, transform=replace(updated.transform, y=cursor)))
                        cursor += text_height(updated) + 0.015
                shift = 0.0 if at_top else (1.0 - _SPLIT_TOP) - (cursor - 0.015)
                for text, updated in placed:
                    moved = replace(updated, transform=replace(updated.transform, y=updated.transform.y + shift))
                    overrides[text.id] = text_override_payload(moved)
            elif split:
                # 上下分置：A 放上缘单行、B 放下缘，居中大字；只有 B 时占代价更低的一条宽带。
                lone = not has_context
                band_top = lone and (
                    position == "top" if position in ("top", "bottom") else bands is not None and bands[0] < bands[1]
                )
                height = _BAND_HEIGHT if lone else _SPLIT_HEIGHT
                bottom_edge = 1.0 - _SPLIT_TOP if lone else _SPLIT_BOTTOM + _SPLIT_HEIGHT
                headline_size = None
                for text, current in sorted(current_texts, key=lambda pair: pair[0].copy_role != "B"):
                    top = text.copy_role == "A"
                    if top and lone:
                        continue
                    y = _SPLIT_TOP if top or band_top else bottom_edge - height
                    requested = max(current.style.font_size, _SPLIT_FONT_A if top else _SPLIT_FONT_B)
                    if top and headline_size is not None:
                        requested = min(requested, _context_cap(headline_size))
                    updated = place(
                        current, _SPLIT_X, y, _SPLIT_WIDTH, height,
                        requested=requested, max_lines=1 if top else 2, align="center",
                    )
                    if not top:
                        headline_size = updated.style.font_size
                    if not (top or band_top):
                        # 下缘贴底：行数少时整体下移，不悬在画面中部。
                        settled = max(y, bottom_edge - text_height(updated))
                        updated = replace(updated, transform=replace(updated.transform, y=settled))
                    overrides[text.id] = text_override_payload(updated)
            elif best is not None:
                _cost, text_x, text_y = best[1]
                slot_width = next(width for _x, _y, width, name in _TEXT_SLOTS.get(key, _TEXT_SLOTS["4x3"]) if name == best[0])
                # A/B 作为同一槽位的上下两块：A 在上，B 紧跟 A 的实际高度往下排。
                cursor = max(0.04, text_y)
                # A 排在 B 上面要先放；字号只取决于槽位宽高，先试排一次 B 拿到它的字号。
                headline = next((current for text, current in current_texts if text.copy_role == "B"), None)
                headline_size = (
                    place(headline, text_x, cursor, slot_width, 0.30, requested=headline.style.font_size, max_lines=2).style.font_size
                    if headline is not None and headline.text.strip() else None
                )
                for text, current in sorted(current_texts, key=lambda pair: pair[0].copy_role != "A"):
                    context = text.copy_role == "A"
                    if context and not (has_context and current.visible):
                        continue
                    requested = current.style.font_size
                    if context and headline_size is not None:
                        requested = min(requested, _context_cap(headline_size))
                    updated = place(
                        current, text_x, cursor, slot_width, 0.14 if context else 0.30,
                        requested=requested, max_lines=1 if context else 2,
                    )
                    overrides[text.id] = text_override_payload(updated)
                    cursor += text_height(updated) + (0.025 if context else 0.0)
            profiles[key] = replace(profile, overrides=overrides)
        return replace(document, profiles=profiles)

    @staticmethod
    def sync_profile(document: CoverDocument, source_key: str, target_key: str) -> CoverDocument:
        """把一个比例里调好的文字和素材套到另一个比例：字号与像素尺寸不变，中心横向位置不变。

        底图取景不同步，两个比例各自保住主体。
        """

        if source_key == target_key or target_key not in document.profiles:
            return document
        source_width, _ = canvas_size(source_key)
        target_width, _ = canvas_size(target_key)
        ratio = source_width / target_width
        profile = document.profiles[target_key]
        overrides = dict(profile.overrides)
        for base in document.objects:
            if base.locked:
                continue
            item = object_for_profile(document, base.id, source_key)
            if isinstance(item, TextObject):
                width = min(0.92, item.rect.width * ratio)
                center = item.transform.x + item.rect.width / 2
                moved = replace(
                    item,
                    transform=replace(item.transform, x=max(-0.5, min(1.0, center - width / 2))),
                    rect=replace(item.rect, width=width),
                    wrap=replace(item.wrap, max_width=width),
                )
                overrides[base.id] = text_override_payload(moved)
            elif isinstance(item, (ImageObject, StickerObject)):
                box = overlay_geometry(item, canvas_size(source_key))
                if box is None:
                    continue
                center = (box.left + box.width / 2) / source_width
                scale = item.transform.scale * ratio
                x = max(-0.5, min(1.0, center - box.width / target_width / 2))
                overrides[base.id] = object_override_payload(replace(item, transform=replace(item.transform, x=x, scale=scale)))
            elif isinstance(item, ShapeObject):
                width = item.width * ratio
                center = item.transform.x + item.width * item.transform.scale / 2
                x = max(-0.5, min(1.0, center - width * item.transform.scale / 2))
                overrides[base.id] = object_override_payload(replace(item, width=width, transform=replace(item.transform, x=x)))
        return replace(document, profiles={**document.profiles, target_key: replace(profile, overrides=overrides)})

    @staticmethod
    def _seed_copy(
        document: CoverDocument, copy: BasicCoverCopy, preset: StylePreset | None, *, big: bool,
    ) -> CoverDocument:
        """把一套文案和样式写回 A/B 主文案，清掉它们的比例覆盖，交给自动排版重新放置。"""

        if copy.context.strip():
            document = ensure_context_text(document)
        ids = set(primary_copy_ids(document).values())
        objects = []
        for item in document.objects:
            if isinstance(item, TextObject) and item.id in ids:
                value = (copy.context if item.copy_role == "A" else copy.headline).strip()
                current = object_for_profile(document, item.id, "4x3")
                style = current.style if isinstance(current, TextObject) else item.style
                if preset is not None:
                    style = preset.apply(style, item.copy_role)
                size = (_SCHEME_FONT_BIG if big else _SCHEME_FONT_B) if item.copy_role == "B" else _SCHEME_FONT_A
                item = replace(
                    item, text=value, visible=bool(value), style=resize_text_style(style, size),
                    transform=replace(item.transform, rotation=0.0, scale=1.0), emphasis=copy.emphasis_in(value),
                )
            objects.append(item)
        profiles = {
            key: replace(profile, overrides={k: v for k, v in profile.overrides.items() if k not in ids})
            for key, profile in document.profiles.items()
        }
        return replace(document, objects=tuple(objects), profiles=profiles)

    def layout_schemes(
        self,
        document: CoverDocument,
        image_path: str | Path,
        variants: tuple[BasicCoverCopy, ...],
        *,
        batch: int = 0,
        recent: tuple[CoverWork, ...] = (),
    ) -> tuple[CoverScheme, ...]:
        """三套方案：推荐 / 换一种构图 / 换文案换配色；“换一批”轮换文案和配色。

        recent 是最近导出的作品：第二套优先最近没用过的构图，第三套优先没用过的配色。
        """

        if not variants:
            ids = primary_copy_ids(document)
            texts = {
                role: item.text if isinstance(item := object_for_profile(document, object_id, "4x3"), TextObject) else ""
                for role, object_id in ids.items()
            }
            variants = (BasicCoverCopy(context=texts.get("A", ""), headline=texts.get("B", "")),)
        first = variants[batch % len(variants)]
        second = variants[(batch + 1) % len(variants)]
        presets = {preset.key: preset for preset in STYLE_PRESETS}
        # 只比 B 的颜色（主色调）；稳定排序：最近用得少的配色在前，同样少时保持原有轮换顺序。
        used_palettes = Counter(item.palette.split("|")[0] for item in recent)
        ranked_presets = sorted(
            _SCHEME_PRESETS,
            key=lambda key: used_palettes[
                palette_signature(self._seed_copy(document, second, presets[key], big=False)).split("|")[0]
            ],
        )
        preset = presets[ranked_presets[batch % len(ranked_presets)]]

        def build(copy: BasicCoverCopy, style: StylePreset | None, *, big: bool = False, mode: str = "auto") -> CoverDocument:
            return self.apply_auto_layout(self._seed_copy(document, copy, style, big=big), image_path, mode=mode)

        recommended = build(first, None)
        # 第二套换一种构图；“换一批”在与推荐不同的构图之间轮换。
        alternatives: list[CoverScheme] = []
        if first.context.strip():
            alternatives += [
                CoverScheme("split", "上下分置", "A 放上缘、B 放下缘，人物留在中间", build(first, None, mode="split")),
                CoverScheme("stack", "标题在上", "大标题在上、A 作小字紧跟其下", build(first, None, mode="stack")),
            ]
        alternatives += [
            CoverScheme(
                "headline", "大字", "只留主文案放大成一条宽带，首页小图也看得清",
                build(replace(first, context=""), None, big=True, mode="split"),
            ),
            CoverScheme("side", "侧边", "文字放到画面较空的一侧，人物更完整", build(first, None, mode="slot")),
        ]
        distinct = [item for item in alternatives if item.document != recommended] or alternatives
        used_compositions = Counter(item.composition for item in recent)
        distinct.sort(key=lambda item: used_compositions[composition_signature(item.document)])
        middle = distinct[batch % len(distinct)]
        return (
            CoverScheme("recommended", "推荐", "避开人物主体自动排版：A 交代背景，B 放大爆点", recommended),
            middle,
            CoverScheme("alternate", preset.label, "换一版文案并换配色", build(second, preset)),
        )

    def schemes_from_ideas(
        self, document: CoverDocument, image_path: str | Path, ideas: tuple[AISchemeIdea, ...], *,
        notes: AIFrameNotes | None = None,
    ) -> tuple[CoverScheme, ...]:
        """AI 选的“文案 + 排法 + 配色”交给本地排版引擎，生成可编辑的方案。"""

        presets = {preset.key: preset for preset in STYLE_PRESETS}
        schemes = []
        for idea in ideas:
            copy = idea.copy.as_basic()
            big = idea.layout == "headline"
            if big:
                copy = replace(copy, context="")
            mode = "split" if big else idea.layout
            built = self.apply_auto_layout(
                self._seed_copy(document, copy, presets[idea.preset], big=big), image_path, mode=mode, position=idea.place,
                zoom=idea.zoom, notes=notes, framing=idea.framing,
            )
            schemes.append(CoverScheme(f"ai:{idea.direction}", f"AI·{idea.direction}", idea.reason, built))
        return tuple(schemes)

    def ai_candidates(
        self, document: CoverDocument, image_path: str | Path, copies: tuple[AICopy, ...], *, limit: int = 12,
        notes: AIFrameNotes | None = None,
    ) -> tuple[CoverScheme, ...]:
        """给看图模型挑的候选：文案 × 排法和位置 × 取景 × 配色，尽量各不相同。

        看图标出了脸：以按脸取景为主（带上半身 / 特写），字放在字带更干净的上方或下方，另有一种人在一侧、
        字在另一侧的构图，原画面只留一张兜底；排好后量一量字压没压到界面或脸，压到的不给模型挑（合格的
        不够四张时按压得少的补齐）。copies 要按好坏排好：前几句先配满各种构图，其余的再补。
        没标出脸时退回“原画面 / 拉近”两轮，跳过界面所在的那几侧（剩不到两种就不过滤）。
        """

        if not copies:
            return ()
        busy = notes.busy if notes is not None else frozenset()
        arrangements = tuple(
            item for item in _CANDIDATE_ARRANGEMENTS if not (_ARRANGEMENT_REGIONS[item] & busy)
        )
        if len(arrangements) < 2:
            arrangements = _CANDIDATE_ARRANGEMENTS
        source_size = image_size(str(image_path)) if notes is not None and notes.face is not None else None
        if source_size is not None:
            zone = min(("bottom", "top"), key=lambda value: self._zone_cost(source_size, notes, value))
            side = min(SIDE_ZONES, key=lambda value: self._zone_cost(source_size, notes, value))
            plans = [
                (("lead", zone), 1.0, "loose"), (("slot", side), 1.0, "loose"), (("headline", zone), 1.0, "tight"),
                (("split", zone), 1.0, "loose"), (("lead", zone), 1.0, "tight"), (("headline", zone), 1.0, "loose"),
                (("split", zone), 1.0, "tight"),
            ] + [(arrangement, 1.0, "") for arrangement in arrangements[:1]]
        else:
            plans = [(arrangement, 1.0, "") for arrangement in arrangements] + [
                (arrangement, _CANDIDATE_ZOOM, "") for arrangement in arrangements
            ]
        best = min(len(copies), _TOP_COPIES)

        def copy_for(plan: int, round_index: int) -> AICopy:
            if round_index == 0 or len(copies) <= best:
                return copies[(plan + round_index) % best]
            rest = copies[best:]
            return rest[(plan + (round_index - 1) * len(plans)) % len(rest)]

        ideas = []
        for index in range(limit):
            # 同一种排法每轮出现时换文案、换配色，避免和上一轮重复。
            plan, round_index = index % len(plans), index // len(plans)
            (layout, position), zoom, framing = plans[plan]
            ideas.append(AISchemeIdea(
                direction="", copy=copy_for(plan, round_index), layout=layout,
                preset=_SCHEME_PRESETS[(plan + 2 * round_index) % len(_SCHEME_PRESETS)], reason="", place=position,
                zoom=zoom, framing=framing,
            ))
        candidates: list[CoverScheme] = []
        for scheme in self.schemes_from_ideas(document, image_path, tuple(ideas), notes=notes):
            if all(item.document != scheme.document for item in candidates):
                candidates.append(scheme)
        if source_size is not None and candidates:
            hits = [text_hit(item.document, source_size, notes) for item in candidates]
            allowed = max(_TEXT_HIT_OK, sorted(hits)[min(len(hits), _MIN_CANDIDATES) - 1])
            candidates = [item for item, hit in zip(candidates, hits) if hit <= allowed]
        return tuple(
            replace(item, key=f"candidate-{number}", label=str(number)) for number, item in enumerate(candidates, 1)
        )

    @staticmethod
    def _zone_cost(source_size: tuple[int, int], notes: AIFrameNotes, zone: str) -> float:
        """字放这一侧时，按脸宽松取景的代价（字带压界面、压脸越多越大）；放不下脸就是无穷大。"""

        found = subject_crop(source_size, canvas_size("4x3"), notes, zone, "loose")
        return (found[1] if found is not None else float("inf")) + (_TOP_ZONE_BIAS if zone == "top" else 0.0)

    @staticmethod
    def with_frame(document: CoverDocument, path: str | Path, timestamp: float) -> CoverDocument:
        """把底图换成视频里另一帧（AI 选的表情帧）；排版不动，交给后续自动排版。"""

        objects = tuple(
            replace(item, asset=replace(item.asset, path=str(path)) if item.asset else AssetRef(path=str(path)))
            if isinstance(item, BackgroundObject) else item
            for item in document.objects
        )
        source = replace(document.source, selected_timestamp=float(timestamp), image_asset_id=str(path))
        return replace(document, objects=objects, source=source)

    @staticmethod
    def apply_scheme(document: CoverDocument, scheme: CoverScheme) -> CoverDocument:
        """套用方案：只替换 A/B 主文案和底图取景；用户加的素材和文本框保持不动。

        方案换了底图帧（AI 选的表情帧）时一起换上；用户锁了帧就只用取景、不换帧。
        """

        source = scheme.document
        locked = {item.id for item in document.objects if item.locked}
        ids = set(primary_copy_ids(source).values()) - locked
        background = next((item.id for item in source.objects if isinstance(item, BackgroundObject)), None)
        keys = ids | ({background} if background else set())
        replaced = {item.id: item for item in source.objects if item.id in ids}
        existing = {item.id for item in document.objects}
        # 方案里新补的 A 块（原封面只有 B 时）也一并带进来。
        objects = tuple(replaced.get(item.id, item) for item in document.objects) + tuple(
            item for item in source.objects if item.id in ids and item.id not in existing
        )
        profiles = {}
        for key, profile in document.profiles.items():
            overrides = {k: v for k, v in profile.overrides.items() if k not in keys}
            theirs = source.profiles.get(key)
            if theirs is not None:
                overrides.update({k: v for k, v in theirs.overrides.items() if k in keys})
            profiles[key] = replace(profile, overrides=overrides)
        result = replace(document, objects=objects, profiles=profiles)
        theirs = next((item for item in source.objects if isinstance(item, BackgroundObject)), None)
        mine = next((item for item in document.objects if isinstance(item, BackgroundObject)), None)
        if (
            theirs is not None and mine is not None and theirs.asset is not None and theirs.asset != mine.asset
            and not document.source.frame_locked
        ):
            result = CoverLayoutService.with_frame(result, theirs.asset.path, source.source.selected_timestamp)
        return result

    @staticmethod
    def scheme_thumbnail(document: CoverDocument, *, canvas_key: str = "4x3", width: int = 240) -> bytes:
        """方案缩略图（JPEG 字节）；与导出同一套图层，看到的就是套用后的样子。"""

        background, layers = document_layers(document, canvas_key)
        if background is None:
            raise ValueError("方案缺少底图")
        image = compose_document(canvas_size(canvas_key), background, layers)
        try:
            image.thumbnail((width, width), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, "JPEG", quality=88)
            return buffer.getvalue()
        finally:
            image.close()
