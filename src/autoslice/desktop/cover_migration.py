"""CoverDraft v1~v3 与 CoverDocument v4 的数据迁移和样式升级。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from autoslice_cover.text_layout import (
    DEFAULT_TEXT_STYLE,
    TEXT_STYLE_REVISION,
    wrap_text_lines,
)

from .cover_copy import BasicCoverCopy, generate_basic_copy_variants
from .cover_model import (
    DOCUMENT_VERSION,
    LAYER_REVISION,
    AssetRef,
    BackgroundObject,
    CoverDocument,
    Rect,
    SourceRef,
    TextObject,
    TextStyle,
    TextWrap,
    Transform,
    default_profiles,
)

LEGACY_DRAFT_VERSIONS = {1, 2, 3}


def _number(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result and abs(result) != float("inf") else default


def _bounded(value: Any, default: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, _number(value, default)))


def _legacy_values(payload: Mapping[str, Any], fallback_title: str) -> dict[str, Any]:
    title = str(payload.get("title") or fallback_title).strip() or fallback_title
    try:
        font_size = int(payload.get("font_size", 104))
    except (TypeError, ValueError):
        font_size = 104
    image_path = payload.get("image_path")
    if not isinstance(image_path, str) or not image_path.strip():
        image_path = None
    return {
        "title": title,
        "image_path": image_path,
        "selected_timestamp": max(0.0, _number(payload.get("selected_timestamp"), 0.0)),
        "text_x": _bounded(payload.get("text_x"), 0.06, 0.0, 1.0),
        "text_y": _bounded(payload.get("text_y"), 0.18, 0.0, 1.0),
        "font_size": min(320, max(24, font_size)),
        "background_x": _bounded(payload.get("background_x"), 0.5, 0.0, 1.0),
        "background_y": _bounded(payload.get("background_y"), 0.5, 0.0, 1.0),
        "background_scale": _bounded(payload.get("background_scale"), 1.0, 1.0, 2.5),
    }


def _text_rect(title: str, font_size: int, canvas_width: int = 1440, canvas_height: int = 1080) -> Rect:
    """使用旧模型相同的近似宽度规则生成一次初始文字框。"""

    lines = wrap_text_lines(title, font_size, max_width=0.86, max_lines=8, canvas_width=canvas_width)
    max_units = max(
        (sum(1.18 if ord(char) > 0x2E80 or ord(char) > 0x1F000 else (0.65 if char.isascii() else 1.0) for char in line) for line in lines),
        default=1.0,
    )
    width = min(0.84, max(0.22, max_units * font_size / canvas_width * 1.18))
    height = min(0.55, max(0.08, len(lines) * font_size * 1.18 / canvas_height))
    return Rect(width=width, height=height)


def _copy_text_object(
    *, object_id: str, copy_role: str, text: str, font_size: int, x: float, y: float,
) -> TextObject:
    """为 A/B 生成独立文字对象，位置保守，避免初始文字压到画面中心。"""

    size = max(24, int(font_size))
    style = TextStyle(
        font_size=size,
        font_weight=900 if copy_role == "B" else 700,
        fill=str(DEFAULT_TEXT_STYLE["fill_color"]),
        stroke=str(DEFAULT_TEXT_STYLE["stroke_color"]),
        # 旧版 context 与 emphasis 共用色板，但 context 的描边更轻；
        # 这样 A 不会和 B 叠成一块同权重的大字。
        stroke_width=4 if copy_role == "A" else int(DEFAULT_TEXT_STYLE["stroke_width"]),
        shadow=bool(DEFAULT_TEXT_STYLE["shadow"]),
        line_spacing=float(DEFAULT_TEXT_STYLE["line_spacing"]),
    )
    # rect 是 fit-to-region 的可用区域，不再是按近似字符数压出的固定框。
    # A 给足一行空间并允许必要时缩成两行；B 保留旧版主视觉的两行高度上限。
    rect = Rect(
        width=0.76 if copy_role == "A" else 0.72,
        height=0.15 if copy_role == "A" else 0.25,
    )
    return TextObject(
        id=object_id,
        copy_role=copy_role,
        transform=Transform(x=max(0.04, min(0.84, x)), y=max(0.06, min(0.86, y))),
        z_index=11 if copy_role == "B" else 10,
        text=text,
        rect=rect,
        wrap=TextWrap(max_width=rect.width, max_lines=2),
        style=style,
    )


def document_from_copy_values(
    *,
    copy: BasicCoverCopy,
    image_path: str | None,
    selected_timestamp: float,
    background_x: float,
    background_y: float,
    background_scale: float,
    font_size: int = 104,
) -> CoverDocument:
    """创建基础模式的结构化 A/B 文档；A/B 永远不会先拼回一个字符串。"""

    background = BackgroundObject(
        id="background-main",
        transform=Transform(),
        asset=AssetRef(path=image_path) if image_path else None,
        fit_mode="cover",
        scale=background_scale,
        pan_x=background_x,
        pan_y=background_y,
    )
    objects: list[TextObject] = []
    if copy.context.strip():
        objects.append(_copy_text_object(
            object_id="copy-a", copy_role="A", text=copy.context.strip(),
            font_size=max(48, round(font_size * 0.70)), x=0.06, y=0.08,
        ))
    if copy.headline.strip():
        # A/B 块之间保留一段明确的历史 gap；实际字号不足时 renderer
        # 会继续缩小，而不是把两块压成一个连续标题。
        b_y = 0.27 if objects else 0.16
        objects.append(_copy_text_object(
            object_id="copy-b", copy_role="B", text=copy.headline.strip(),
            font_size=font_size, x=0.06, y=b_y,
        ))
    if not objects:
        objects.append(_copy_text_object(
            object_id="copy-b", copy_role="B", text="未命名封面",
            font_size=font_size, x=0.06, y=0.16,
        ))
    return CoverDocument(
        source=SourceRef(selected_timestamp=selected_timestamp, image_asset_id=image_path),
        profiles=default_profiles(),
        # B 放在序列首位兼容旧 CoverDraft 读取方；z_index 仍保证画布先绘制 A、
        # 再绘制 B，两个对象的独立编辑语义不受影响。
        objects=(background, *sorted(objects, key=lambda item: item.copy_role != "B")),
        active_profile="4x3",
        selected_object_id=objects[-1].id,
    )


def document_from_legacy_payload(payload: Mapping[str, Any], fallback_title: str) -> CoverDocument:
    """把 CoverDraft v1~v3 转换为包含背景和文字对象的 v4 文档。"""

    values = _legacy_values(payload, fallback_title)
    background = BackgroundObject(
        id="background-main",
        transform=Transform(),
        asset=AssetRef(path=values["image_path"]) if values["image_path"] else None,
        fit_mode="cover",
        scale=values["background_scale"],
        pan_x=values["background_x"],
        pan_y=values["background_y"],
    )
    text = TextObject(
        id="title-main",
        transform=Transform(x=values["text_x"], y=values["text_y"]),
        z_index=10,
        text=values["title"],
        copy_role="B",
        rect=_text_rect(values["title"], values["font_size"]),
        wrap=TextWrap(max_width=0.86, max_lines=8),
        style=TextStyle(font_size=values["font_size"]),
    )
    profiles = default_profiles()
    return CoverDocument(
        source=SourceRef(
            selected_timestamp=values["selected_timestamp"],
            image_asset_id=values["image_path"],
        ),
        profiles=profiles,
        objects=(background, text),
        active_profile="4x3",
        selected_object_id=None,
    )


def document_from_payload(payload: Any, fallback_title: str) -> tuple[CoverDocument, bool]:
    """读取 v4 或迁移旧版本，返回文档和是否发生迁移。"""

    if isinstance(payload, Mapping) and payload.get("version") == DOCUMENT_VERSION:
        document, restyled = migrate_v4_style_payload(payload)
        document, relayered = migrate_layer_order(document)
        return document, restyled or relayered
    if isinstance(payload, Mapping) and payload.get("version") in LEGACY_DRAFT_VERSIONS:
        return document_from_legacy_payload(payload, fallback_title), True
    raise ValueError("封面草稿版本不受支持")


def _canonical_style(style: TextStyle) -> TextStyle:
    """只替换旧自动默认的样式字段，保留用户已经选择的字号。"""

    return replace(
        style,
        font_family=str(DEFAULT_TEXT_STYLE["font_family"]),
        font_weight=int(DEFAULT_TEXT_STYLE["font_weight"]),
        fill=str(DEFAULT_TEXT_STYLE["fill_color"]),
        stroke=str(DEFAULT_TEXT_STYLE["stroke_color"]),
        stroke_width=int(DEFAULT_TEXT_STYLE["stroke_width"]),
        shadow=bool(DEFAULT_TEXT_STYLE["shadow"]),
        line_spacing=float(DEFAULT_TEXT_STYLE["line_spacing"]),
    )


def _rect_is_obviously_invalid(raw: Any) -> bool:
    """判断旧草稿是否写入了不可能是正常归一化文字框的尺寸。"""

    if not isinstance(raw, Mapping):
        return False
    try:
        width = float(raw.get("width"))
        height = float(raw.get("height"))
    except (TypeError, ValueError):
        return True
    return width <= 0.0 or height <= 0.0 or width > 1.5 or height > 1.0


def _safe_rect(rect: Rect) -> Rect:
    """修复明显越界的尺寸，保留 x/y 位置以及正常尺寸。"""

    return replace(
        rect,
        width=min(1.0, max(0.05, rect.width)),
        height=min(0.85, max(0.04, rect.height)),
    )


def _canonicalize_text_object(text: TextObject, raw: Mapping[str, Any] | None = None) -> TextObject:
    raw = raw if isinstance(raw, Mapping) else {}
    rect = _safe_rect(text.rect) if _rect_is_obviously_invalid(raw.get("rect")) else text.rect
    raw_wrap = raw.get("wrap")
    wrap = text.wrap
    if isinstance(raw_wrap, Mapping):
        normalized_wrap = TextWrap.from_payload(raw_wrap)
        if dict(normalized_wrap.to_payload()) != dict(raw_wrap):
            wrap = normalized_wrap
    return replace(text, style=_canonical_style(text.style), rect=rect, wrap=wrap)


def migrate_v4_style_payload(payload: Mapping[str, Any]) -> tuple[CoverDocument, bool]:
    """为早期 v4 草稿执行一次显式文字样式升级。

    Phase 2.2A 之前没有文字样式编辑能力，因此 ``style_revision`` 缺失或
    小于当前 revision 的样式都属于自动默认值。字号、位置和正常文字框尺寸
    保留；只重置正式样式字段，并修复明确越界的 rect/wrap。
    """

    document = CoverDocument.from_payload(payload)
    try:
        revision = int(payload.get("style_revision", 0))
    except (TypeError, ValueError):
        revision = 0
    if revision >= TEXT_STYLE_REVISION:
        return document, False

    raw_objects = {
        raw.get("id"): raw
        for raw in payload.get("objects", ())
        if isinstance(raw, Mapping) and isinstance(raw.get("id"), str)
    }
    objects = tuple(
        _canonicalize_text_object(item, raw_objects.get(item.id))
        if isinstance(item, TextObject)
        else item
        for item in document.objects
    )
    text_ids = {item.id for item in objects if isinstance(item, TextObject)}
    profiles = {}
    for key, profile in document.profiles.items():
        overrides = dict(profile.overrides)
        for object_id in text_ids:
            override = overrides.get(object_id)
            if not isinstance(override, Mapping):
                continue
            updated = dict(override)
            if isinstance(override.get("style"), Mapping):
                override_style = TextStyle.from_payload(override["style"])
                updated["style"] = _canonical_style(override_style).to_payload()
            if _rect_is_obviously_invalid(override.get("rect")):
                updated["rect"] = _safe_rect(Rect.from_payload(override["rect"])).to_payload()
            if isinstance(override.get("wrap"), Mapping):
                normalized_wrap = TextWrap.from_payload(override["wrap"])
                if dict(normalized_wrap.to_payload()) != dict(override["wrap"]):
                    updated["wrap"] = normalized_wrap.to_payload()
            overrides[object_id] = updated
        profiles[key] = replace(profile, overrides=overrides)
    return replace(
        document,
        style_revision=TEXT_STYLE_REVISION,
        objects=objects,
        profiles=profiles,
    ), True


def migrate_layer_order(document: CoverDocument) -> tuple[CoverDocument, bool]:
    """升级到统一 z 排序：旧版总把文字画在素材之上，先保持原有观感。"""

    if document.layer_revision >= LAYER_REVISION:
        return document, False
    texts = [item for item in document.objects if isinstance(item, TextObject)]
    overlays = [
        item for item in document.objects
        if not isinstance(item, (TextObject, BackgroundObject))
    ]
    objects = document.objects
    if texts and overlays:
        shift = max(item.z_index for item in overlays) + 1 - min(item.z_index for item in texts)
        if shift > 0:
            objects = tuple(
                replace(item, z_index=item.z_index + shift) if isinstance(item, TextObject) else item
                for item in objects
            )
    return replace(document, objects=objects, layer_revision=LAYER_REVISION), True


def migrate_cover_draft(payload: Any, fallback_title: str) -> CoverDocument:
    """迁移旧 CoverDraft 载荷的明确入口，供服务和纯数据调用方使用。"""

    document, _migrated = document_from_payload(payload, fallback_title)
    return document


def legacy_payload_from_document(document: CoverDocument) -> dict[str, object]:
    """为仍使用 CoverDraft 的旧服务/界面生成兼容 v3 载荷。"""

    background = next((item for item in document.objects if isinstance(item, BackgroundObject)), None)
    text = next((item for item in document.objects if isinstance(item, TextObject) and item.copy_role == "B"), None)
    if text is None:
        text = next((item for item in document.objects if isinstance(item, TextObject)), None)
    profile = document.profiles.get(document.active_profile)
    if background is not None and profile is not None:
        override = profile.overrides.get(background.id, {})
        if isinstance(override, Mapping):
            background = replace(
                background,
                scale=_bounded(override.get("scale"), background.scale, 1.0, 100.0),
                pan_x=_bounded(override.get("pan_x"), background.pan_x, 0.0, 1.0),
                pan_y=_bounded(override.get("pan_y"), background.pan_y, 0.0, 1.0),
            )
    if text is not None and profile is not None:
        override = profile.overrides.get(text.id, {})
        if isinstance(override, Mapping) and isinstance(override.get("transform"), Mapping):
            transform = Transform.from_payload({**text.transform.to_payload(), **override["transform"]})
        else:
            transform = text.transform
        if isinstance(override, Mapping):
            if isinstance(override.get("style"), Mapping):
                text = replace(text, style=TextStyle.from_payload(override["style"]))
            if isinstance(override.get("rect"), Mapping):
                text = replace(text, rect=Rect.from_payload(override["rect"]))
    else:
        transform = text.transform if text else Transform(x=0.06, y=0.18)
    return {
        "version": 3,
        "title": text.text if text else "",
        "image_path": background.asset.path if background and background.asset else None,
        "selected_timestamp": document.source.selected_timestamp,
        "text_x": transform.x,
        "text_y": transform.y,
        "font_size": text.style.font_size if text else 104,
        "background_x": background.pan_x if background else 0.5,
        "background_y": background.pan_y if background else 0.5,
        "background_scale": background.scale if background else 1.0,
    }


def document_from_draft_values(
    *,
    title: str,
    image_path: str | None,
    selected_timestamp: float,
    text_x: float,
    text_y: float,
    font_size: int,
    background_x: float,
    background_y: float,
    background_scale: float,
) -> CoverDocument:
    return document_from_legacy_payload(
        {
            "version": 3,
            "title": title,
            "image_path": image_path,
            "selected_timestamp": selected_timestamp,
            "text_x": text_x,
            "text_y": text_y,
            "font_size": font_size,
            "background_x": background_x,
            "background_y": background_y,
            "background_scale": background_scale,
        },
        title,
    )


def document_from_basic_title_values(
    *,
    title: str,
    image_path: str | None,
    selected_timestamp: float,
    background_x: float,
    background_y: float,
    background_scale: float,
    font_size: int = 104,
) -> CoverDocument:
    """按本地确定性提取器创建新草稿，保留 A/B 独立对象。"""

    variants = generate_basic_copy_variants(title, limit=1)
    copy = variants[0] if variants else BasicCoverCopy(headline=title.strip() or "未命名封面")
    return document_from_copy_values(
        copy=copy,
        image_path=image_path,
        selected_timestamp=selected_timestamp,
        background_x=background_x,
        background_y=background_y,
        background_scale=background_scale,
        font_size=font_size,
    )


def draft_values_from_document(document: CoverDocument) -> dict[str, object]:
    """返回旧 CoverDraft 构造器所需字段，避免迁移层依赖服务模块。"""

    payload = legacy_payload_from_document(document)
    return {
        "title": payload["title"],
        "image_path": payload["image_path"],
        "selected_timestamp": payload["selected_timestamp"],
        "text_x": payload["text_x"],
        "text_y": payload["text_y"],
        "font_size": payload["font_size"],
        "background_x": payload["background_x"],
        "background_y": payload["background_y"],
        "background_scale": payload["background_scale"],
    }
