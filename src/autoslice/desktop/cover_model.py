"""AutoCover 的纯数据编辑模型。

本模块不依赖 Qt、Pillow 或媒体文件。它只负责描述可持久化的封面文档，
供迁移层、服务层和未来的画布控制器共同使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from autoslice_cover.text_layout import (
    DEFAULT_TEXT_STYLE,
    TEXT_STYLE_REVISION,
    normalize_text_style,
)

DOCUMENT_VERSION = 4
# 1：文字、图片、贴纸、形状统一按 z_index 绘制；0 为旧版“文字永远在最上”。
LAYER_REVISION = 1
OBJECT_KINDS = {"background", "text", "image", "sticker", "shape"}
PROFILE_SIZES = {"4x3": (1440, 1080), "16x9": (1920, 1080)}


def _number(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result and abs(result) != float("inf") else default


def _bounded(value: Any, default: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, _number(value, default)))


def _text(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _color(value: Any) -> str:
    """可选颜色：#RGB/#RRGGBB/#RRGGBBAA，其余视为关闭。"""

    if not isinstance(value, str):
        return ""
    value = value.strip()
    if len(value) in {4, 7, 9} and value.startswith("#"):
        try:
            int(value[1:], 16)
        except ValueError:
            return ""
        return value.upper()
    return ""


@dataclass(frozen=True)
class Transform:
    """与画布无关的通用对象变换。x/y 使用归一化画布坐标。"""

    x: float = 0.5
    y: float = 0.5
    scale: float = 1.0
    rotation: float = 0.0

    def to_payload(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "scale": self.scale, "rotation": self.rotation}

    @classmethod
    def from_payload(cls, payload: Any) -> "Transform":
        payload = payload if isinstance(payload, Mapping) else {}
        return cls(
            x=_bounded(payload.get("x"), 0.5, 0.0, 1.0),
            y=_bounded(payload.get("y"), 0.5, 0.0, 1.0),
            scale=_bounded(payload.get("scale"), 1.0, 0.01, 100.0),
            rotation=_bounded(payload.get("rotation"), 0.0, -360.0, 360.0),
        )


@dataclass(frozen=True)
class Rect:
    x: float = 0.0
    y: float = 0.0
    width: float = 1.0
    height: float = 1.0

    def to_payload(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    @classmethod
    def from_payload(cls, payload: Any) -> "Rect":
        payload = payload if isinstance(payload, Mapping) else {}
        return cls(
            x=_bounded(payload.get("x"), 0.0, -10.0, 10.0),
            y=_bounded(payload.get("y"), 0.0, -10.0, 10.0),
            width=_bounded(payload.get("width"), 1.0, 0.001, 10.0),
            height=_bounded(payload.get("height"), 1.0, 0.001, 10.0),
        )


@dataclass(frozen=True)
class AssetRef:
    path: str | None = None
    asset_id: str | None = None

    def to_payload(self) -> dict[str, str | None]:
        return {"path": self.path, "asset_id": self.asset_id}

    @classmethod
    def from_payload(cls, payload: Any) -> "AssetRef | None":
        if payload is None:
            return None
        if isinstance(payload, str):
            return cls(path=payload)
        if not isinstance(payload, Mapping):
            return None
        path = payload.get("path")
        asset_id = payload.get("asset_id")
        return cls(
            path=path.strip() if isinstance(path, str) and path.strip() else None,
            asset_id=asset_id.strip() if isinstance(asset_id, str) and asset_id.strip() else None,
        )


@dataclass(frozen=True)
class SourceRef:
    video_id: str | None = None
    selected_timestamp: float = 0.0
    image_asset_id: str | None = None
    # 用户明确锁定后，后台候选、文案和 AI 都不能替换当前取景帧。
    frame_locked: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "video_id": self.video_id,
            "selected_timestamp": self.selected_timestamp,
            "image_asset_id": self.image_asset_id,
            "frame_locked": self.frame_locked,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "SourceRef":
        payload = payload if isinstance(payload, Mapping) else {}
        return cls(
            video_id=payload.get("video_id") if isinstance(payload.get("video_id"), str) else None,
            selected_timestamp=max(0.0, _number(payload.get("selected_timestamp"), 0.0)),
            image_asset_id=(
                payload.get("image_asset_id")
                if isinstance(payload.get("image_asset_id"), str)
                else None
            ),
            frame_locked=bool(payload.get("frame_locked", False)),
        )


@dataclass(frozen=True)
class TextWrap:
    max_width: float = 0.86
    mode: str = "wrap"
    max_lines: int = 8

    def to_payload(self) -> dict[str, object]:
        return {"max_width": self.max_width, "mode": self.mode, "max_lines": self.max_lines}

    @classmethod
    def from_payload(cls, payload: Any) -> "TextWrap":
        payload = payload if isinstance(payload, Mapping) else {}
        try:
            max_lines = int(payload.get("max_lines", 8))
        except (TypeError, ValueError):
            max_lines = 8
        return cls(
            max_width=_bounded(payload.get("max_width"), 0.86, 0.05, 1.0),
            mode=_text(payload.get("mode"), "wrap") if payload.get("mode") in {"wrap", "clip"} else "wrap",
            max_lines=min(32, max(1, max_lines)),
        )


@dataclass(frozen=True)
class TextStyle:
    """封面文字样式。

    ``font_family`` 保留历史字段名：默认字体保存族名，用户明确选择的
    自定义字体可以保存绝对文件路径。Qt 与 Pillow 必须通过
    ``autoslice_cover.fonts.resolve_font_selection`` 把这个兼容值解析为同一
    个实际字体文件和族名后再绘制。
    """

    font_family: str = str(DEFAULT_TEXT_STYLE["font_family"])
    font_weight: int = int(DEFAULT_TEXT_STYLE["font_weight"])
    font_size: int = 104
    fill: str = str(DEFAULT_TEXT_STYLE["fill_color"])
    stroke: str = str(DEFAULT_TEXT_STYLE["stroke_color"])
    stroke_width: int = int(DEFAULT_TEXT_STYLE["stroke_width"])
    shadow: bool = bool(DEFAULT_TEXT_STYLE["shadow"])
    line_spacing: float = float(DEFAULT_TEXT_STYLE["line_spacing"])
    align: str = "left"
    # 可选效果：外描边（双层描边）与文字底条；空字符串表示关闭。
    outer_stroke: str = ""
    outer_stroke_width: int = 0
    backdrop: str = ""

    @property
    def fill_color(self) -> str:
        """正式字段名；``fill`` 保留用于旧 v4 草稿兼容。"""

        return self.fill

    @property
    def stroke_color(self) -> str:
        """正式字段名；``stroke`` 保留用于旧 v4 草稿兼容。"""

        return self.stroke

    def to_payload(self) -> dict[str, object]:
        return {
            "font_family": self.font_family,
            "font_weight": self.font_weight,
            "font_size": self.font_size,
            "fill": self.fill,
            "stroke": self.stroke,
            "fill_color": self.fill,
            "stroke_color": self.stroke,
            "stroke_width": self.stroke_width,
            "shadow": self.shadow,
            "line_spacing": self.line_spacing,
            "align": self.align,
            "outer_stroke": self.outer_stroke,
            "outer_stroke_width": self.outer_stroke_width,
            "backdrop": self.backdrop,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "TextStyle":
        payload = normalize_text_style(payload) | (payload if isinstance(payload, Mapping) else {})
        try:
            font_size = int(payload.get("font_size", 104))
            stroke_width = int(payload.get("stroke_width", 6))
        except (TypeError, ValueError):
            font_size, stroke_width = 104, 6
        return cls(
            font_family=_text(payload.get("font_family")),
            font_weight=min(1000, max(100, _integer(payload.get("font_weight"), int(DEFAULT_TEXT_STYLE["font_weight"]))),),
            font_size=min(320, max(24, font_size)),
            fill=_text(payload.get("fill_color", payload.get("fill")), str(DEFAULT_TEXT_STYLE["fill_color"])),
            stroke=_text(payload.get("stroke_color", payload.get("stroke")), str(DEFAULT_TEXT_STYLE["stroke_color"])),
            stroke_width=min(64, max(0, stroke_width)),
            shadow=bool(payload.get("shadow", DEFAULT_TEXT_STYLE["shadow"])),
            line_spacing=_bounded(payload.get("line_spacing"), float(DEFAULT_TEXT_STYLE["line_spacing"]), 0.5, 3.0),
            align=payload.get("align") if payload.get("align") in {"left", "center", "right"} else "left",
            outer_stroke=_color(payload.get("outer_stroke")),
            outer_stroke_width=min(48, max(0, _integer(payload.get("outer_stroke_width"), 0))),
            backdrop=_color(payload.get("backdrop")),
        )


@dataclass(frozen=True)
class RenderObject:
    id: str
    kind: str
    transform: Transform = field(default_factory=Transform)
    z_index: int = 0
    visible: bool = True
    locked: bool = False

    def _base_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "transform": self.transform.to_payload(),
            "z_index": self.z_index,
            "visible": self.visible,
            "locked": self.locked,
        }


@dataclass(frozen=True)
class BackgroundObject(RenderObject):
    kind: str = field(default="background", init=False)
    asset: AssetRef | None = None
    fit_mode: str = "cover"
    scale: float = 1.0
    pan_x: float = 0.5
    pan_y: float = 0.5
    crop: Rect | None = None

    def to_payload(self) -> dict[str, object]:
        value = self._base_payload()
        value.update({
            "asset": self.asset.to_payload() if self.asset else None,
            "fit_mode": self.fit_mode,
            "scale": self.scale,
            "pan_x": self.pan_x,
            "pan_y": self.pan_y,
            "crop": self.crop.to_payload() if self.crop else None,
        })
        return value

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "BackgroundObject":
        return cls(
            id=_text(payload.get("id"), "background-main"),
            transform=Transform.from_payload(payload.get("transform")),
            z_index=_integer(payload.get("z_index"), 0),
            visible=bool(payload.get("visible", True)),
            locked=bool(payload.get("locked", False)),
            asset=AssetRef.from_payload(payload.get("asset")),
            fit_mode=payload.get("fit_mode") if payload.get("fit_mode") in {"cover", "contain"} else "cover",
            scale=_bounded(payload.get("scale"), 1.0, 0.01, 100.0),
            pan_x=_bounded(payload.get("pan_x"), 0.5, 0.0, 1.0),
            pan_y=_bounded(payload.get("pan_y"), 0.5, 0.0, 1.0),
            crop=Rect.from_payload(payload["crop"]) if isinstance(payload.get("crop"), Mapping) else None,
        )


@dataclass(frozen=True)
class TextObject(RenderObject):
    kind: str = field(default="text", init=False)
    text: str = ""
    # 基础封面文案的语义角色；旧草稿缺失时按 B（主视觉）兼容。
    copy_role: str = "B"
    rect: Rect = field(default_factory=lambda: Rect(width=0.58, height=0.30))
    wrap: TextWrap = field(default_factory=TextWrap)
    align: str = "left"
    style: TextStyle = field(default_factory=TextStyle)

    def to_payload(self) -> dict[str, object]:
        value = self._base_payload()
        value.update({
            "text": self.text,
            "copy_role": self.copy_role if self.copy_role in {"A", "B"} else "B",
            "rect": self.rect.to_payload(),
            "wrap": self.wrap.to_payload(),
            "align": self.align,
            "style": self.style.to_payload(),
        })
        return value

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "TextObject":
        align = payload.get("align") if payload.get("align") in {"left", "center", "right"} else "left"
        return cls(
            id=_text(payload.get("id"), "title-main"),
            transform=Transform.from_payload(payload.get("transform")),
            z_index=int(payload.get("z_index", 10)),
            visible=bool(payload.get("visible", True)),
            locked=bool(payload.get("locked", False)),
            text=_text(payload.get("text")),
            copy_role=payload.get("copy_role") if payload.get("copy_role") in {"A", "B"} else "B",
            rect=(
                Rect.from_payload(payload["rect"])
                if isinstance(payload.get("rect"), Mapping)
                else Rect(width=0.58, height=0.30)
            ),
            wrap=TextWrap.from_payload(payload.get("wrap")),
            align=align,
            style=TextStyle.from_payload(payload.get("style")),
        )


@dataclass(frozen=True)
class ImageObject(RenderObject):
    kind: str = field(default="image", init=False)
    asset: AssetRef | None = None
    opacity: float = 1.0
    crop: Rect | None = None

    def to_payload(self) -> dict[str, object]:
        value = self._base_payload()
        value.update({
            "asset": self.asset.to_payload() if self.asset else None,
            "opacity": self.opacity,
            "crop": self.crop.to_payload() if self.crop else None,
        })
        return value

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ImageObject":
        return cls(
            id=_text(payload.get("id"), "image-main"),
            transform=Transform.from_payload(payload.get("transform")),
            z_index=_integer(payload.get("z_index"), 20),
            visible=bool(payload.get("visible", True)),
            locked=bool(payload.get("locked", False)),
            asset=AssetRef.from_payload(payload.get("asset")),
            opacity=_bounded(payload.get("opacity"), 1.0, 0.0, 1.0),
            crop=Rect.from_payload(payload["crop"]) if isinstance(payload.get("crop"), Mapping) else None,
        )


@dataclass(frozen=True)
class StickerObject(RenderObject):
    kind: str = field(default="sticker", init=False)
    asset: AssetRef | None = None
    category: str = "effect"
    opacity: float = 1.0

    def to_payload(self) -> dict[str, object]:
        value = self._base_payload()
        value.update({
            "asset": self.asset.to_payload() if self.asset else None,
            "category": self.category,
            "opacity": self.opacity,
        })
        return value

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "StickerObject":
        return cls(
            id=_text(payload.get("id"), "sticker-main"),
            transform=Transform.from_payload(payload.get("transform")),
            z_index=_integer(payload.get("z_index"), 30),
            visible=bool(payload.get("visible", True)),
            locked=bool(payload.get("locked", False)),
            asset=AssetRef.from_payload(payload.get("asset")),
            category=_text(payload.get("category"), "effect"),
            opacity=_bounded(payload.get("opacity"), 1.0, 0.0, 1.0),
        )


@dataclass(frozen=True)
class ShapeObject(RenderObject):
    kind: str = field(default="shape", init=False)
    shape_type: str = "rect"
    fill: str | None = None
    stroke: str = "#FFDB4D"
    stroke_width: int = 8
    width: float = 0.22
    height: float = 0.16

    def to_payload(self) -> dict[str, object]:
        value = self._base_payload()
        value.update({
            "shape_type": self.shape_type,
            "fill": self.fill,
            "stroke": self.stroke,
            "stroke_width": self.stroke_width,
            "width": self.width,
            "height": self.height,
        })
        return value

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ShapeObject":
        shape_type = payload.get("shape_type") if payload.get("shape_type") in {"circle", "arrow", "rect"} else "rect"
        try:
            stroke_width = int(payload.get("stroke_width", 8))
        except (TypeError, ValueError):
            stroke_width = 8
        return cls(
            id=_text(payload.get("id"), "shape-main"),
            transform=Transform.from_payload(payload.get("transform")),
            z_index=_integer(payload.get("z_index"), 25),
            visible=bool(payload.get("visible", True)),
            locked=bool(payload.get("locked", False)),
            shape_type=shape_type,
            fill=payload.get("fill") if isinstance(payload.get("fill"), str) else None,
            stroke=_text(payload.get("stroke"), "#FFDB4D"),
            stroke_width=min(64, max(0, stroke_width)),
            width=_bounded(payload.get("width"), 0.22, 0.02, 0.95),
            height=_bounded(payload.get("height"), 0.16, 0.02, 0.95),
        )


RenderableObject = BackgroundObject | TextObject | ImageObject | StickerObject | ShapeObject


def _object_from_payload(payload: Any) -> RenderableObject | None:
    if not isinstance(payload, Mapping):
        return None
    kind = payload.get("kind")
    if kind == "background":
        return BackgroundObject.from_payload(payload)
    if kind == "text":
        return TextObject.from_payload(payload)
    if kind == "image":
        return ImageObject.from_payload(payload)
    if kind == "sticker":
        return StickerObject.from_payload(payload)
    if kind == "shape":
        return ShapeObject.from_payload(payload)
    return None


@dataclass(frozen=True)
class LayoutProfile:
    key: str
    width: int
    height: int
    safe_area: Rect = field(default_factory=lambda: Rect(0.04, 0.04, 0.92, 0.92))
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    export_suffix: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "key": self.key,
            "width": self.width,
            "height": self.height,
            "safe_area": self.safe_area.to_payload(),
            "overrides": self.overrides,
            "export_suffix": self.export_suffix,
        }

    @classmethod
    def from_payload(cls, key: str, payload: Any) -> "LayoutProfile":
        payload = payload if isinstance(payload, Mapping) else {}
        default_width, default_height = PROFILE_SIZES.get(key, (1440, 1080))
        try:
            width = max(1, int(payload.get("width", default_width)))
            height = max(1, int(payload.get("height", default_height)))
        except (TypeError, ValueError):
            width, height = default_width, default_height
        raw_overrides = payload.get("overrides")
        overrides = {
            str(object_id): dict(value)
            for object_id, value in raw_overrides.items()
            if isinstance(object_id, str) and isinstance(value, Mapping)
        } if isinstance(raw_overrides, Mapping) else {}
        return cls(
            key=key,
            width=width,
            height=height,
            safe_area=Rect.from_payload(payload.get("safe_area")),
            overrides=overrides,
            export_suffix=_text(payload.get("export_suffix"), ""),
        )


@dataclass(frozen=True)
class CoverDocument:
    """可持久化的封面文档；选择态只存在根级元数据，不写入对象字段。"""

    version: int = DOCUMENT_VERSION
    style_revision: int = TEXT_STYLE_REVISION
    source: SourceRef = field(default_factory=SourceRef)
    profiles: dict[str, LayoutProfile] = field(default_factory=dict)
    objects: tuple[RenderableObject, ...] = ()
    active_profile: str = "4x3"
    selected_object_id: str | None = None
    layer_revision: int = LAYER_REVISION

    def __post_init__(self) -> None:
        if self.version != DOCUMENT_VERSION:
            raise ValueError(f"不支持的 CoverDocument 版本：{self.version}")
        if not self.profiles:
            object.__setattr__(self, "profiles", default_profiles())
        if self.active_profile not in self.profiles:
            object.__setattr__(self, "active_profile", next(iter(self.profiles), "4x3"))
        try:
            revision = int(self.style_revision)
        except (TypeError, ValueError):
            revision = 0
        if revision < 0:
            object.__setattr__(self, "style_revision", 0)
        elif revision != self.style_revision:
            object.__setattr__(self, "style_revision", revision)

    def to_payload(self) -> dict[str, object]:
        return {
            "version": self.version,
            "style_revision": self.style_revision,
            "source": self.source.to_payload(),
            "profiles": {key: value.to_payload() for key, value in self.profiles.items()},
            "objects": [item.to_payload() for item in self.objects],
            "active_profile": self.active_profile,
            "selected_object_id": self.selected_object_id,
            "layer_revision": self.layer_revision,
        }

    def object(self, object_id: str, profile_key: str | None = None) -> RenderableObject | None:
        """按 id 读取对象；提供 profile key 时返回该比例的有效覆盖。"""

        return object_for_profile(self, object_id, profile_key) if profile_key else next(
            (item for item in self.objects if item.id == object_id), None
        )

    def replace_object(
        self,
        updated: RenderableObject,
        *,
        profile_key: str | None = None,
        persist_profile_override: bool = True,
    ) -> "CoverDocument":
        """返回替换对象后的新文档，避免 UI 层散落手写 profile 更新。"""

        from dataclasses import replace

        objects = tuple(updated if item.id == updated.id else item for item in self.objects)
        result = replace(
            self,
            objects=objects,
            active_profile=profile_key or self.active_profile,
            selected_object_id=updated.id,
        )
        if not persist_profile_override:
            return result
        key = profile_key or self.active_profile
        profile = result.profiles.get(key)
        if profile is None:
            return result
        override: dict[str, Any] = {
            "transform": updated.transform.to_payload(),
            "visible": bool(updated.visible),
        }
        if isinstance(updated, BackgroundObject):
            override.update({
                "scale": updated.scale,
                "pan_x": updated.pan_x,
                "pan_y": updated.pan_y,
                "fit_mode": updated.fit_mode,
            })
        elif isinstance(updated, TextObject):
            override.update({
                "rect": updated.rect.to_payload(),
                "wrap": updated.wrap.to_payload(),
                "align": updated.align,
                "style": updated.style.to_payload(),
            })
        elif isinstance(updated, (ImageObject, StickerObject)):
            override["opacity"] = updated.opacity
        elif isinstance(updated, ShapeObject):
            override.update({
                "shape_type": updated.shape_type,
                "fill": updated.fill,
                "stroke": updated.stroke,
                "stroke_width": updated.stroke_width,
                "width": updated.width,
                "height": updated.height,
            })
        return replace(
            result,
            profiles={
                **result.profiles,
                key: replace(profile, overrides={**profile.overrides, updated.id: override}),
            },
        )

    @classmethod
    def from_payload(cls, payload: Any) -> "CoverDocument":
        if not isinstance(payload, Mapping) or payload.get("version") != DOCUMENT_VERSION:
            raise ValueError("不是版本 4 的 CoverDocument")
        raw_profiles = payload.get("profiles")
        profiles = {
            str(key): LayoutProfile.from_payload(str(key), value)
            for key, value in raw_profiles.items()
            if isinstance(key, str)
        } if isinstance(raw_profiles, Mapping) else {}
        objects = tuple(
            item for raw in payload.get("objects", ())
            if (item := _object_from_payload(raw)) is not None
        ) if isinstance(payload.get("objects", ()), (list, tuple)) else ()
        selected = payload.get("selected_object_id")
        return cls(
            style_revision=_integer(payload.get("style_revision"), 0),
            source=SourceRef.from_payload(payload.get("source")),
            profiles=profiles,
            objects=objects,
            active_profile=_text(payload.get("active_profile"), "4x3"),
            selected_object_id=selected if isinstance(selected, str) else None,
            # 缺失即旧草稿，由迁移层把文字提到素材之上后再升级。
            layer_revision=max(0, _integer(payload.get("layer_revision"), 0)),
        )


def default_profiles() -> dict[str, LayoutProfile]:
    return {
        key: LayoutProfile(key, width, height, export_suffix="" if key == "4x3" else "-16x9")
        for key, (width, height) in PROFILE_SIZES.items()
    }


def object_for_profile(document: CoverDocument, object_id: str, profile_key: str | None = None) -> RenderableObject | None:
    """返回应用 profile 覆盖后的对象，供未来渲染适配器使用。"""

    profile = document.profiles.get(profile_key or document.active_profile)
    item = next((candidate for candidate in document.objects if candidate.id == object_id), None)
    if item is None or profile is None or object_id not in profile.overrides:
        return item
    override = profile.overrides[object_id]
    if not isinstance(override, Mapping):
        return item
    # profile 覆盖只保存本比例发生变化的字段，避免切换比例时互相覆盖。
    changes: dict[str, Any] = {}
    if isinstance(override.get("transform"), Mapping):
        changes["transform"] = Transform.from_payload({**item.transform.to_payload(), **override["transform"]})
    if "visible" in override:
        changes["visible"] = bool(override["visible"])
    if isinstance(item, BackgroundObject):
        for field_name in ("scale", "pan_x", "pan_y", "fit_mode"):
            if field_name in override:
                changes[field_name] = override[field_name]
    if isinstance(item, TextObject):
        if isinstance(override.get("rect"), Mapping):
            changes["rect"] = Rect.from_payload(override["rect"])
        if isinstance(override.get("wrap"), Mapping):
            changes["wrap"] = TextWrap.from_payload(override["wrap"])
        if override.get("align") in {"left", "center", "right"}:
            changes["align"] = override["align"]
        if isinstance(override.get("style"), Mapping):
            changes["style"] = TextStyle.from_payload(override["style"])
    if isinstance(item, (ImageObject, StickerObject)):
        if "opacity" in override:
            changes["opacity"] = _bounded(override.get("opacity"), item.opacity, 0.0, 1.0)
    if isinstance(item, ShapeObject):
        if override.get("shape_type") in {"circle", "arrow", "rect"}:
            changes["shape_type"] = override["shape_type"]
        if isinstance(override.get("fill"), str) or override.get("fill") is None:
            changes["fill"] = override.get("fill")
        if isinstance(override.get("stroke"), str):
            changes["stroke"] = override["stroke"]
        if "stroke_width" in override:
            changes["stroke_width"] = min(64, max(0, _integer(override.get("stroke_width"), item.stroke_width)))
        if "width" in override:
            changes["width"] = _bounded(override.get("width"), item.width, 0.02, 0.95)
        if "height" in override:
            changes["height"] = _bounded(override.get("height"), item.height, 0.02, 0.95)
    if not changes:
        return item
    from dataclasses import replace
    return replace(item, **changes)


# 两比例共享的文字样式字段；字号、位置、尺寸和对齐按比例分别保存。
SHARED_TEXT_STYLE_FIELDS = (
    "font_family",
    "font_weight",
    "fill",
    "stroke",
    "stroke_width",
    "shadow",
    "line_spacing",
    "outer_stroke",
    "outer_stroke_width",
    "backdrop",
)


def text_override_payload(item: TextObject) -> dict[str, Any]:
    return {
        "transform": item.transform.to_payload(),
        "visible": bool(item.visible),
        "rect": item.rect.to_payload(),
        "wrap": item.wrap.to_payload(),
        "align": item.align,
        "style": item.style.to_payload(),
    }


def update_text_object(document: CoverDocument, updated: TextObject, *, profile_key: str) -> CoverDocument:
    """写回一次文字编辑：文案和样式同步到两个比例，其余只写当前比例。"""

    from dataclasses import replace

    base = next((item for item in document.objects if item.id == updated.id), None)
    if not isinstance(base, TextObject):
        return document
    shared = {name: getattr(updated.style, name) for name in SHARED_TEXT_STYLE_FIELDS}
    new_base = replace(base, text=updated.text, style=replace(base.style, **shared))
    profiles: dict[str, LayoutProfile] = {}
    for key, profile in document.profiles.items():
        override = profile.overrides.get(updated.id)
        if key == profile_key:
            payload: dict[str, Any] = text_override_payload(updated)
        elif isinstance(override, Mapping) and isinstance(override.get("style"), Mapping):
            style = replace(TextStyle.from_payload(override["style"]), **shared)
            payload = {**override, "style": style.to_payload()}
        else:
            profiles[key] = profile
            continue
        profiles[key] = replace(profile, overrides={**profile.overrides, updated.id: payload})
    return replace(
        document,
        objects=tuple(new_base if item.id == updated.id else item for item in document.objects),
        profiles=profiles,
        active_profile=profile_key,
    )
