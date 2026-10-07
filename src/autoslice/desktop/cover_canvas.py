"""AutoCover 的对象化编辑画布。

画布只处理显示和鼠标手势，持久化由 ``CoverEditorWidget`` 通过
``CoverDocument`` 完成。为了兼容 Phase 1 的调用方，本模块仍保留标题/底图
位置变化信号和 ``set_title_rect`` 等旧入口；新代码应使用 ``set_document``。
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import QLabel

from autoslice_cover.document_layout import (
    BACKGROUND_FILL,
    BACKGROUND_SCALE_MAX,
    BACKGROUND_SCALE_MIN,
    SHADOW_ALPHA,
    Box,
    TextLayout,
    arrow_head_size,
    backdrop_box,
    backdrop_radius,
    background_box,
    focus_after_drag,
    load_font,
    shadow_offset,
)
from autoslice_cover.document_render import rgba
from autoslice_cover.fonts import FontResolution, resolve_font_selection

from .cover_layout import (
    asset_path,
    background_geometry,
    overlay_geometry,
    shape_geometry,
    text_layout,
    text_paint,
)
from .cover_model import (
    PROFILE_SIZES,
    BackgroundObject,
    CoverDocument,
    ImageObject,
    Rect,
    RenderObject,
    ShapeObject,
    StickerObject,
    TextObject,
    Transform,
    object_for_profile,
)
from .qt_preview.theme import COLORS


def _qcolor(value: str | None, default: str = "#000000") -> QColor:
    # 模型颜色为 #RRGGBBAA，Qt 的同长度写法是 #AARRGGBB，统一走同一解析。
    return QColor(*rgba(value, default))


class CoverCanvas(QLabel):
    """以 ``CoverDocument`` 为来源的轻量设计工具式画布。"""

    title_position_changed = Signal(float, float)
    title_position_finished = Signal()
    background_position_changed = Signal(float, float)
    background_position_finished = Signal()
    zoom_changed = Signal(float)
    selected_changed = Signal(bool)
    selected_object_changed = Signal(str)
    safe_area_warning_changed = Signal(bool)
    object_changed = Signal(object, str)
    # 手势进行中只同步轻量的控件显示，不触碰 CoverDocument 或预览任务。
    object_preview_changed = Signal(object, str)
    # 选中框四角的删除/复制按钮：由编辑器执行，画布只发出请求。
    delete_requested = Signal(str)
    duplicate_requested = Signal(str)

    _SAFE_MARGIN = 0.06
    _HANDLE = 9.0
    # 旧版 Web Canvas 的行为常量：移动到画布中心附近 8px 即吸附，
    # 缩放按右下角位移换算为字号比例。
    _CENTER_SNAP_THRESHOLD_PX = 8.0
    _MIN_FONT_SIZE = 12
    _MAX_FONT_SIZE = 320
    # 选中框：贴合对象，四角为操作按钮，文字左右为改宽手柄。
    _CHROME_COLOR = QColor(59, 160, 255)
    _CHROME_PAD = 6.0
    _HANDLE_RADIUS = 11.0
    _EDGE_HANDLE = 5.0
    _ROTATE_SNAP_DEGREES = 4.0
    _HANDLE_CURSORS = {
        "delete": Qt.CursorShape.PointingHandCursor,
        "duplicate": Qt.CursorShape.PointingHandCursor,
        "rotate": Qt.CursorShape.CrossCursor,
        "scale": Qt.CursorShape.SizeFDiagCursor,
        "width-left": Qt.CursorShape.SizeHorCursor,
        "width-right": Qt.CursorShape.SizeHorCursor,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(420, 260)
        self._pixmap = QPixmap()
        self._background_pixmap = QPixmap()
        self._document: CoverDocument | None = None
        self._profile_key = "4x3"
        self._canvas_ratio = PROFILE_SIZES["4x3"]
        self._legacy_title_rect = QRectF()
        self._selected_object = "title"
        self._mode: str | None = None
        self._press = QPointF()
        self._drag_offset = QPointF()
        self._start_transform = Transform()
        self._start_rect = Rect(width=0.58, height=0.30)
        self._start_background = (0.5, 0.5, 1.0)
        self._start_font_size = 104
        self._start_object = None
        self._start_frame = None
        self._safe_area_warning = False
        self._background_x = 0.5
        self._background_y = 0.5
        self._zoom = 1.0
        self._guide_vertical = False
        self._guide_horizontal = False
        self._hover_handle: str | None = None
        self._font_family_cache: dict[str, str] = {}
        self._font_id_cache: dict[str, int] = {}
        self._variable_font_cache: dict[str, bool] = {}
        self._glyph_path_cache: dict[tuple, tuple[QPainterPath, tuple]] = {}
        self._outline_cache: dict[tuple, QPainterPath] = {}
        self._overlay_pixmap_cache: dict[str, QPixmap] = {}
        self._last_resolved_font: FontResolution | None = None
        self._last_qt_font_family = ""
        self._last_qt_font_id = -1
        # 拖动期间的本地暂态对象，释放鼠标时才提交。
        self._gesture_objects: dict[str, object] = {}
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(
            f"background: {COLORS.nav}; border: 1px solid {COLORS.divider}; border-radius: 6px;"
        )

    def set_document(self, document: CoverDocument | None, profile_key: str | None = None):
        self._document = document
        self._gesture_objects.clear()
        self._profile_key = profile_key or (document.active_profile if document else "4x3")
        self.set_canvas_ratio(self._profile_key)
        selected = document.selected_object_id if document else None
        text = self._find_text()
        self._selected_object = selected or (text.id if text else "background")
        self.update()

    def document(self) -> CoverDocument | None:
        return self._document

    @property
    def resolved_font(self) -> FontResolution | None:
        """最近一次实际绘制文字所使用的字体来源，供诊断和 QTest 使用。"""

        return self._last_resolved_font

    @property
    def resolved_qt_font(self) -> tuple[str, int]:
        """最近一次绘制使用的 Qt family 与 application font id。"""

        return self._last_qt_font_family, self._last_qt_font_id

    def set_canvas_ratio(self, profile_key: str | int, height: int | None = None):
        if isinstance(profile_key, str):
            self._profile_key = profile_key if profile_key in PROFILE_SIZES else "4x3"
            self._canvas_ratio = PROFILE_SIZES[self._profile_key]
        else:
            self._canvas_ratio = (max(1, int(profile_key)), max(1, int(height or 1)))
        self.update()

    def set_background_pixmap(self, pixmap: QPixmap):
        self._background_pixmap = pixmap
        self.update()

    def set_preview(self, pixmap: QPixmap):
        self._pixmap = pixmap
        if self._background_pixmap.isNull():
            self._background_pixmap = pixmap
        self.update()

    def pixmap(self):
        return self._pixmap

    def set_selected_object(self, object_name: str):
        if object_name == "title" and self._find_text() is not None:
            object_name = self._find_text().id
        if object_name == "background" and self._find_background() is not None:
            object_name = self._find_background().id
        self._selected_object = object_name
        self.update()

    def set_zoom(self, zoom: float):
        self._zoom = max(BACKGROUND_SCALE_MIN, min(BACKGROUND_SCALE_MAX, float(zoom)))
        self.update()

    def set_background_focus(self, x: float, y: float):
        self._background_x = max(0.0, min(1.0, float(x)))
        self._background_y = max(0.0, min(1.0, float(y)))
        self.update()

    def set_title_rect(self, rect: QRectF):
        self._legacy_title_rect = rect
        self._set_safe_area_warning(self._title_outside_safe_area())
        self.update()

    def _canvas_rect(self) -> QRectF:
        if self._document is None and not self._pixmap.isNull():
            target = QRectF(self.rect())
            ratio = self._pixmap.width() / max(1, self._pixmap.height())
            if target.width() / max(1.0, target.height()) > ratio:
                height = target.height()
                width = height * ratio
            else:
                width = target.width()
                height = width / ratio
            return QRectF(target.center().x() - width / 2, target.center().y() - height / 2, width, height)
        width, height = self._canvas_ratio
        target = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        ratio = width / max(1.0, height)
        if target.width() / max(1.0, target.height()) > ratio:
            h = target.height()
            w = h * ratio
        else:
            w = target.width()
            h = w / ratio
        return QRectF(target.center().x() - w / 2, target.center().y() - h / 2, w, h)

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

    def _norm_point(self, point: QPointF, *, clamp: bool = False) -> tuple[float, float] | None:
        image = self._canvas_rect()
        if image.isNull() or (not clamp and not image.contains(point)):
            return None
        return (
            max(0.0, min(1.0, (point.x() - image.left()) / max(1.0, image.width()))),
            max(0.0, min(1.0, (point.y() - image.top()) / max(1.0, image.height()))),
        )

    def _export_size(self) -> tuple[int, int]:
        return PROFILE_SIZES.get(self._profile_key, self._canvas_ratio) if self._document is not None else self._canvas_ratio

    def _to_screen(self, box: Box) -> QRectF:
        """导出像素矩形映射到屏幕；与导出共用同一几何。"""

        image = self._canvas_rect()
        factor = image.width() / max(1, self._export_size()[0])
        return QRectF(
            image.left() + box.left * factor,
            image.top() + box.top * factor,
            box.width * factor,
            box.height * factor,
        )

    def _text_layout_for(self, obj: TextObject) -> TextLayout:
        return text_layout(obj, self._export_size())

    def _text_box(self, obj: TextObject) -> tuple[Box, Box]:
        """（贴合文字的框, 行宽区域）；空文字时退回行宽区域。"""

        layout = self._text_layout_for(obj)
        area = layout.area
        if layout.lines:
            return layout.ink, area
        return Box(area.left, area.top, area.width, max(24.0, area.height)), area

    def _display_rect(self, obj: TextObject) -> QRectF:
        """贴合文字的选中框（未旋转，屏幕坐标），用于命中、吸附与选中态。"""

        return self._to_screen(self._text_box(obj)[0])

    def _frame(self, obj: RenderObject) -> tuple[QRectF, QPointF, float] | None:
        """对象选中框：未旋转矩形、旋转中心与角度（屏幕坐标），与导出旋转一致。"""

        if isinstance(obj, TextObject):
            box, area = self._text_box(obj)
            center = self._to_screen(Box(area.center_x, area.center_y, 0, 0)).topLeft()
            return self._to_screen(box), center, float(obj.transform.rotation)
        if isinstance(obj, (ImageObject, StickerObject, ShapeObject)):
            rect = self._object_display_rect(obj)
            return rect, rect.center(), float(obj.transform.rotation)
        return None

    @staticmethod
    def _rotate(point: QPointF, center: QPointF, degrees: float) -> QPointF:
        if abs(degrees) < 0.01:
            return QPointF(point)
        radians = math.radians(degrees)
        dx, dy = point.x() - center.x(), point.y() - center.y()
        return QPointF(
            center.x() + dx * math.cos(radians) - dy * math.sin(radians),
            center.y() + dx * math.sin(radians) + dy * math.cos(radians),
        )

    def _frame_contains(self, obj: RenderObject, point: QPointF) -> bool:
        frame = self._frame(obj)
        if frame is None:
            return False
        rect, center, angle = frame
        return rect.adjusted(-4, -4, 4, 4).contains(self._rotate(point, center, -angle))

    def _selected_editable(self) -> RenderObject | None:
        if self._document is None:
            return None
        return next(
            (item for item in self._layered_objects() if item.id == self._selected_object and item.visible),
            None,
        )

    def _handle_points(self, obj: RenderObject) -> dict[str, QPointF]:
        """四角操作按钮与左右改宽手柄的屏幕位置（随对象旋转）。"""

        frame = self._frame(obj)
        if frame is None:
            return {}
        rect, center, angle = frame
        rect = rect.adjusted(-self._CHROME_PAD, -self._CHROME_PAD, self._CHROME_PAD, self._CHROME_PAD)
        local = {
            "delete": rect.topLeft(),
            "rotate": rect.topRight(),
            "duplicate": rect.bottomLeft(),
            "scale": rect.bottomRight(),
        }
        if isinstance(obj, TextObject):
            local["width-left"] = QPointF(rect.left(), rect.center().y())
            local["width-right"] = QPointF(rect.right(), rect.center().y())
        return {name: self._rotate(position, center, angle) for name, position in local.items()}

    def _handle_at(self, point: QPointF) -> tuple[str, RenderObject] | None:
        obj = self._selected_editable()
        if obj is None:
            return None
        for name, position in self._handle_points(obj).items():
            radius = self._EDGE_HANDLE + 4 if name.startswith("width") else self._HANDLE_RADIUS + 2
            if math.hypot(point.x() - position.x(), point.y() - position.y()) <= radius:
                return name, obj
        return None


    def _overlay_box(self, obj: RenderObject) -> Box:
        size = self._export_size()
        if isinstance(obj, ShapeObject):
            return shape_geometry(obj, size)
        box = overlay_geometry(obj, size) if isinstance(obj, (ImageObject, StickerObject)) else None
        if box is None:
            # 资源缺失时保留可点选的占位框，便于删除或替换。
            width, height = size
            scale = max(0.05, float(obj.transform.scale or 1.0))
            return Box(obj.transform.x * width, obj.transform.y * height, width * 0.24 * scale, height * 0.18 * scale)
        return box

    def _object_display_rect(self, obj: RenderObject) -> QRectF:
        return self._to_screen(self._overlay_box(obj))

    def _legacy_display_rect(self) -> QRectF:
        image = self._canvas_rect()
        rect = self._legacy_title_rect
        if rect.isNull():
            return QRectF()
        return QRectF(image.left() + rect.left() * image.width(), image.top() + rect.top() * image.height(), rect.width() * image.width(), rect.height() * image.height())

    def _find_text(self) -> TextObject | None:
        if self._document is None:
            return None
        candidates = [item for item in self._document.objects if isinstance(item, TextObject)]
        for item in candidates:
            live = self._gesture_objects.get(item.id)
            effective = live if isinstance(live, TextObject) else object_for_profile(self._document, item.id, self._profile_key)
            if item.id == self._selected_object and isinstance(effective, TextObject):
                return effective
        for item in candidates:
            live = self._gesture_objects.get(item.id)
            effective = live if isinstance(live, TextObject) else object_for_profile(self._document, item.id, self._profile_key)
            if isinstance(effective, TextObject):
                return effective
        return None

    def _find_texts(self) -> tuple[TextObject, ...]:
        """返回当前比例下的文字对象，支持最小 A/B 双块显示。"""

        if self._document is None:
            return ()
        result: list[TextObject] = []
        for item in self._document.objects:
            if not isinstance(item, TextObject):
                continue
            live = self._gesture_objects.get(item.id)
            effective = live if isinstance(live, TextObject) else object_for_profile(self._document, item.id, self._profile_key)
            if isinstance(effective, TextObject):
                result.append(effective)
        return tuple(result)

    def _find_background(self) -> BackgroundObject | None:
        if self._document is None:
            return None
        for item in self._document.objects:
            if isinstance(item, BackgroundObject):
                live = self._gesture_objects.get(item.id)
                if isinstance(live, BackgroundObject):
                    return live
                effective = object_for_profile(self._document, item.id, self._profile_key)
                return effective if isinstance(effective, BackgroundObject) else item
        return None

    def _find_overlay_objects(self) -> tuple[RenderObject, ...]:
        if self._document is None:
            return ()
        result: list[RenderObject] = []
        for item in self._document.objects:
            if isinstance(item, (ImageObject, StickerObject, ShapeObject)):
                live = self._gesture_objects.get(item.id)
                effective = live if isinstance(live, type(item)) else object_for_profile(self._document, item.id, self._profile_key)
                if isinstance(effective, (ImageObject, StickerObject, ShapeObject)):
                    result.append(effective)
        return tuple(result)

    def _layered_objects(self) -> tuple[RenderObject, ...]:
        """除背景外的对象（含手势暂态），按 z 排序；与导出绘制顺序一致。"""

        if self._document is None:
            return ()
        ordered = []
        for index, item in enumerate(self._document.objects):
            if isinstance(item, BackgroundObject):
                continue
            live = self._gesture_objects.get(item.id)
            effective = live if isinstance(live, type(item)) else object_for_profile(self._document, item.id, self._profile_key)
            if effective is not None:
                ordered.append((effective.z_index, index, effective))
        return tuple(item for _z, _index, item in sorted(ordered, key=lambda value: (value[0], value[1])))

    def _hit_object(self, point: QPointF) -> RenderObject | None:
        """按 z 从上到下命中第一个可见对象；旋转对象按旋转后的框命中。"""

        for item in reversed(self._layered_objects()):
            if item.visible and self._frame_contains(item, point):
                return item
        return None

    def _text_at(self, point: QPointF) -> TextObject | None:
        hit = self._hit_object(point)
        return hit if isinstance(hit, TextObject) else None

    def _overlay_at(self, point: QPointF) -> RenderObject | None:
        hit = self._hit_object(point)
        return hit if isinstance(hit, (ImageObject, StickerObject, ShapeObject)) else None

    def _title_display_rect(self) -> QRectF:
        text = self._find_text()
        return self._display_rect(text) if text else self._legacy_display_rect()

    def _safe_area_warning_for(self, obj: TextObject | None) -> bool:
        if obj is None:
            return False
        return obj.transform.x < self._SAFE_MARGIN or obj.transform.y < self._SAFE_MARGIN or obj.transform.x + obj.rect.width > 1.0 - self._SAFE_MARGIN or obj.transform.y + obj.rect.height > 1.0 - self._SAFE_MARGIN

    def _title_outside_safe_area(self) -> bool:
        text = self._find_text()
        if text:
            return self._safe_area_warning_for(text)
        rect = self._legacy_title_rect
        return bool(rect) and (rect.left() < self._SAFE_MARGIN or rect.top() < self._SAFE_MARGIN or rect.right() > 1.0 - self._SAFE_MARGIN or rect.bottom() > 1.0 - self._SAFE_MARGIN)

    def _set_safe_area_warning(self, value: bool):
        value = bool(value)
        if value != self._safe_area_warning:
            self._safe_area_warning = value
            self.safe_area_warning_changed.emit(value)

    def _set_local_effective(self, obj: object):
        if self._document is None:
            return
        profile = self._document.profiles.get(self._profile_key)
        if profile is None:
            return
        overrides = dict(profile.overrides)
        payload: dict[str, Any] = {"transform": obj.transform.to_payload()}  # type: ignore[attr-defined]
        payload["visible"] = bool(getattr(obj, "visible", True))
        if isinstance(obj, BackgroundObject):
            payload.update({"scale": obj.scale, "pan_x": obj.pan_x, "pan_y": obj.pan_y})
        elif isinstance(obj, TextObject):
            payload.update({"rect": obj.rect.to_payload(), "align": obj.align, "wrap": obj.wrap.to_payload(), "style": obj.style.to_payload()})
        elif isinstance(obj, (ImageObject, StickerObject)):
            payload["opacity"] = obj.opacity
        elif isinstance(obj, ShapeObject):
            payload.update({"shape_type": obj.shape_type, "fill": obj.fill, "stroke": obj.stroke, "stroke_width": obj.stroke_width, "width": obj.width, "height": obj.height})
        self._document = replace(self._document, profiles={**self._document.profiles, self._profile_key: replace(profile, overrides={**overrides, obj.id: payload})})  # type: ignore[attr-defined]

    def _emit_text(self, obj: TextObject, *, commit: bool = True):
        if commit:
            self._set_local_effective(obj)
            self._gesture_objects.pop(obj.id, None)
            self.object_changed.emit(obj, self._profile_key)
        else:
            self._gesture_objects[obj.id] = obj
            self.object_preview_changed.emit(obj, self._profile_key)
        self.title_position_changed.emit(obj.transform.x, obj.transform.y)
        self._set_safe_area_warning(self._safe_area_warning_for(obj))

    def _emit_background(self, obj: BackgroundObject, *, commit: bool = True):
        if commit:
            self._set_local_effective(obj)
            self._gesture_objects.pop(obj.id, None)
            self.object_changed.emit(obj, self._profile_key)
        else:
            self._gesture_objects[obj.id] = obj
        self.background_position_changed.emit(obj.pan_x, obj.pan_y)
        if commit:
            self.zoom_changed.emit(obj.scale)

    def _emit_overlay(self, obj: RenderObject, *, commit: bool = True):
        if commit:
            self._set_local_effective(obj)
            self._gesture_objects.pop(obj.id, None)
            self.object_changed.emit(obj, self._profile_key)
        else:
            self._gesture_objects[obj.id] = obj
            self.object_preview_changed.emit(obj, self._profile_key)
        self.update()

    def _commit_gesture(self):
        """一次性提交本次鼠标手势中的最后对象状态。"""

        pending = tuple(self._gesture_objects.values())
        self._gesture_objects.clear()
        for item in pending:
            if isinstance(item, (TextObject, BackgroundObject, ImageObject, StickerObject, ShapeObject)):
                self._set_local_effective(item)
                self.object_changed.emit(item, self._profile_key)

    @staticmethod
    def _snap_to_center(
        candidate_x: float,
        candidate_y: float,
        element_width: float,
        element_height: float,
        frame_width: float,
        frame_height: float,
    ) -> tuple[float, float, bool, bool]:
        """复刻旧版 ``snapElementToCanvasCenter`` 的 8px 阈值。"""

        frame_width = max(1.0, float(frame_width))
        frame_height = max(1.0, float(frame_height))
        centered_x = (frame_width - element_width) / (2.0 * frame_width)
        centered_y = (frame_height - element_height) / (2.0 * frame_height)
        snap_x = abs((candidate_x - centered_x) * frame_width) <= CoverCanvas._CENTER_SNAP_THRESHOLD_PX
        snap_y = abs((candidate_y - centered_y) * frame_height) <= CoverCanvas._CENTER_SNAP_THRESHOLD_PX
        return (
            centered_x if snap_x else candidate_x,
            centered_y if snap_y else candidate_y,
            snap_x,
            snap_y,
        )

    def _set_alignment_guides(self, vertical: bool, horizontal: bool):
        self._guide_vertical = bool(vertical)
        self._guide_horizontal = bool(horizontal)
        self.update()

    def _screen_to_export(self) -> float:
        return self._export_size()[0] / max(1.0, self._canvas_rect().width())

    def _scale_gesture(self, point: QPointF):
        """右下角：以左上角为锚点等比缩放；文字改字号和行宽，素材改缩放。"""

        obj, (rect, center, angle) = self._start_object, self._start_frame
        anchor = self._rotate(rect.topLeft(), center, angle)
        start = math.hypot(self._press.x() - anchor.x(), self._press.y() - anchor.y())
        now = math.hypot(point.x() - anchor.x(), point.y() - anchor.y())
        ratio = self._clamp(now / max(8.0, start), 0.15, 6.0)
        if isinstance(obj, TextObject):
            size = int(self._clamp(round(obj.style.font_size * ratio), self._MIN_FONT_SIZE, self._MAX_FONT_SIZE))
            actual = size / max(1, obj.style.font_size)
            width = self._clamp(obj.rect.width * actual, 0.02, 3.0)
            self._emit_text(replace(
                obj,
                style=replace(obj.style, font_size=size),
                rect=replace(obj.rect, width=width, height=self._clamp(obj.rect.height * actual, 0.02, 3.0)),
                wrap=replace(obj.wrap, max_width=min(1.0, width)),
            ), commit=False)
        else:
            scale = self._clamp(obj.transform.scale * ratio, 0.05, 4.0)
            self._emit_overlay(replace(obj, transform=replace(obj.transform, scale=scale)), commit=False)
        self.update()

    def _rotate_gesture(self, point: QPointF):
        """右上角：绕对象中心旋转，接近 0°/±90°/180° 时吸附。"""

        obj, (_rect, center, _angle) = self._start_object, self._start_frame
        before = math.atan2(self._press.y() - center.y(), self._press.x() - center.x())
        after = math.atan2(point.y() - center.y(), point.x() - center.x())
        value = obj.transform.rotation + math.degrees(after - before)
        value = (value + 180.0) % 360.0 - 180.0
        for target in (-180.0, -90.0, 0.0, 90.0, 180.0):
            if abs(value - target) <= self._ROTATE_SNAP_DEGREES:
                value = target
                break
        updated = replace(obj, transform=replace(obj.transform, rotation=round(value, 1)))
        if isinstance(obj, TextObject):
            self._emit_text(updated, commit=False)
        else:
            self._emit_overlay(updated, commit=False)
        self.update()

    def _width_gesture(self, point: QPointF, *, left: bool):
        """左右手柄：只改文字行宽，字号不变，文字随宽度重新换行。"""

        obj, (_rect, _center, angle) = self._start_object, self._start_frame
        radians = math.radians(angle)
        axis = (math.cos(radians), math.sin(radians))
        moved = (point.x() - self._press.x()) * axis[0] + (point.y() - self._press.y()) * axis[1]
        moved *= self._screen_to_export()
        width_px, height_px = self._export_size()
        scale = max(0.01, float(obj.transform.scale or 1.0))
        start = obj.rect.width * scale * width_px
        minimum = max(24.0, obj.style.font_size * scale * 1.1)
        new_width = max(minimum, start + (-moved if left else moved))
        transform = obj.transform
        if left:
            # 左手柄移动的是左边界：位置沿旋转后的横轴同步平移。
            shift = start - new_width
            transform = replace(
                transform,
                x=transform.x + shift * axis[0] / width_px,
                y=transform.y + shift * axis[1] / height_px,
            )
        normalized = new_width / scale / width_px
        self._emit_text(replace(
            obj,
            transform=transform,
            rect=replace(obj.rect, width=normalized),
            wrap=replace(obj.wrap, max_width=min(1.0, normalized)),
        ), commit=False)
        self.update()

    def _begin_handle(self, name: str, obj: RenderObject, point: QPointF):
        self._mode = name
        self._start_object = obj
        self._start_frame = self._frame(obj)
        self._press = point
        self.grabMouse()
        self.setCursor(self._HANDLE_CURSORS.get(name, Qt.CursorShape.ArrowCursor))
        self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        point = event.position()
        handle = self._handle_at(point)
        if handle is not None:
            # 操作按钮可能在画面外缘，先于画面范围判断。
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._begin_handle(handle[0], handle[1], point)
            event.accept()
            return
        norm = self._norm_point(point)
        if norm is None:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        hit = self._hit_object(point)
        text = hit if isinstance(hit, TextObject) else None
        overlay = hit if isinstance(hit, (ImageObject, StickerObject, ShapeObject)) else None
        legacy = self._document is None and self._legacy_display_rect().contains(point)
        if text is not None or legacy:
            self._selected_object = text.id if text is not None else "title"
            self._mode = "text"
            if text is not None:
                self._start_transform, self._start_rect = text.transform, text.rect
                self._start_font_size = int(text.style.font_size)
            else:
                self._start_transform = Transform(x=self._legacy_title_rect.left(), y=self._legacy_title_rect.top())
                self._start_rect = Rect(width=self._legacy_title_rect.width(), height=self._legacy_title_rect.height())
            self._drag_offset = QPointF(norm[0] - self._start_transform.x, norm[1] - self._start_transform.y)
            self.selected_changed.emit(True)
            self.selected_object_changed.emit(text.id if text is not None else "title")
        elif overlay is not None:
            self._selected_object = overlay.id
            self._mode = "object"
            self._start_transform = overlay.transform
            self._drag_offset = QPointF(norm[0] - overlay.transform.x, norm[1] - overlay.transform.y)
            self.selected_changed.emit(False)
            self.selected_object_changed.emit(overlay.id)
        else:
            background = self._find_background()
            self._selected_object = background.id if background else "background"
            self._mode = "background"
            self._start_background = (background.pan_x, background.pan_y, background.scale) if background else (self._background_x, self._background_y, self._zoom)
            self.selected_changed.emit(False)
            self.selected_object_changed.emit(self._selected_object)
            self._set_alignment_guides(False, False)
        self._press = point
        # 旧版把 move/up 监听挂在 window 上，指针离开文字节点后仍继续拖动；
        # QWidget 对应行为用 grabMouse 保持同样的连续抓取感。
        self.grabMouse()
        self.setCursor(Qt.CursorShape.SizeAllCursor if self._mode == "text" else Qt.CursorShape.ClosedHandCursor)
        self.update()
        event.accept()

    def mouseMoveEvent(self, event):
        if self._mode is None:
            self._update_hover_state(event.position())
            self.update()
            return
        image = self._canvas_rect()
        point = event.position()
        if self._mode == "scale":
            self._scale_gesture(point)
        elif self._mode == "rotate":
            self._rotate_gesture(point)
        elif self._mode in ("width-left", "width-right"):
            self._width_gesture(point, left=self._mode == "width-left")
        elif self._mode in ("delete", "duplicate"):
            pass
        elif self._mode == "text":
            obj = self._find_text()
            norm = self._norm_point(point, clamp=True)
            if obj is not None and norm is not None:
                candidate_x = self._clamp(norm[0] - self._drag_offset.x(), -0.5, 1.0)
                candidate_y = self._clamp(norm[1] - self._drag_offset.y(), -0.5, 1.0)
                # 吸附以贴合文字的框为准：框中心对齐画面中心。
                box, area = self._text_box(obj)
                width_px, height_px = self._export_size()
                offset_x = (box.left - area.left) / width_px
                offset_y = (box.top - area.top) / height_px
                display = self._display_rect(obj)
                box_x, box_y, snap_x, snap_y = self._snap_to_center(
                    candidate_x + offset_x,
                    candidate_y + offset_y,
                    display.width(),
                    display.height(),
                    image.width(),
                    image.height(),
                )
                self._set_alignment_guides(snap_x, snap_y)
                updated = replace(obj, transform=replace(obj.transform, x=box_x - offset_x, y=box_y - offset_y))
                self._emit_text(updated, commit=False)
                self.update()
            elif obj is None and norm is not None:
                x = self._clamp(norm[0] - self._drag_offset.x(), 0.0, max(0.0, 1.0 - self._legacy_title_rect.width()))
                y = self._clamp(norm[1] - self._drag_offset.y(), 0.0, max(0.0, 1.0 - self._legacy_title_rect.height()))
                self._legacy_title_rect.moveTo(x, y)
                self.title_position_changed.emit(x, y)
                self._set_safe_area_warning(self._title_outside_safe_area())
                self.update()
        elif self._mode == "object":
            norm = self._norm_point(point, clamp=True)
            obj = next((item for item in self._find_overlay_objects() if item.id == self._selected_object), None)
            if obj is not None and norm is not None:
                candidate_x = self._clamp(norm[0] - self._drag_offset.x(), -0.5, 1.0)
                candidate_y = self._clamp(norm[1] - self._drag_offset.y(), -0.5, 1.0)
                self._emit_overlay(replace(obj, transform=replace(obj.transform, x=candidate_x, y=candidate_y)), commit=False)
        else:
            dx = (point.x() - self._press.x()) / max(1.0, image.width())
            dy = (point.y() - self._press.y()) / max(1.0, image.height())
            obj = self._find_background()
            if obj:
                px, py, scale = self._start_background
                width, height = self._export_size()
                drawn = self._background_box(replace(obj, scale=scale))
                # focus 语义与导出一致，换算后画面跟手移动。
                pan_x = focus_after_drag(px, dx * width, width, drawn.width) if drawn else px
                pan_y = focus_after_drag(py, dy * height, height, drawn.height) if drawn else py
                self._emit_background(replace(obj, pan_x=pan_x, pan_y=pan_y, scale=scale), commit=False)
            else:
                self._background_x = self._clamp(self._start_background[0] + dx, 0.0, 1.0)
                self._background_y = self._clamp(self._start_background[1] + dy, 0.0, 1.0)
                self.background_position_changed.emit(self._background_x, self._background_y)
                self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._mode is not None:
            mode = self._mode
            target = self._start_object
            clicked = (
                mode in ("delete", "duplicate")
                and math.hypot(event.position().x() - self._press.x(), event.position().y() - self._press.y()) <= self._HANDLE_RADIUS
            )
            self._commit_gesture()
            self._mode = None
            self._start_object = None
            self._set_alignment_guides(False, False)
            if self.mouseGrabber() is self:
                self.releaseMouse()
            self.unsetCursor()
            self.update()
            if clicked and target is not None:
                (self.delete_requested if mode == "delete" else self.duplicate_requested).emit(target.id)
            elif mode not in ("delete", "duplicate"):
                (self.background_position_finished if mode == "background" else self.title_position_finished).emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self._document is not None and self._selected_object not in {"background", (self._find_background().id if self._find_background() else "background")}:
            super().wheelEvent(event)
            return
        steps = event.angleDelta().y() / 120.0
        if not steps:
            return
        obj = self._find_background()
        value = max(BACKGROUND_SCALE_MIN, min(BACKGROUND_SCALE_MAX, (obj.scale if obj else self._zoom) + steps * 0.1))
        if obj:
            self._emit_background(replace(obj, scale=value))
        else:
            self._zoom = value
            self.zoom_changed.emit(value)
        self.update()
        event.accept()

    def keyPressEvent(self, event):
        """迁移旧版文字键盘微调，不把编辑状态写进文档。"""

        text = self._find_text()
        overlay = next((item for item in self._find_overlay_objects() if item.id == self._selected_object), None)
        if overlay is not None and self._mode is None:
            key = event.key()
            if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                event.accept()
                self._emit_overlay(replace(overlay, visible=False))
                self._selected_object = self._find_background().id if self._find_background() else "background"
                self.selected_changed.emit(False)
                self.selected_object_changed.emit(self._selected_object)
                return
            large_step = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            step = 0.02 if large_step else 0.005
            dx = -step if key == Qt.Key.Key_Left else step if key == Qt.Key.Key_Right else 0.0
            dy = -step if key == Qt.Key.Key_Up else step if key == Qt.Key.Key_Down else 0.0
            if dx or dy:
                event.accept()
                self._emit_overlay(replace(overlay, transform=replace(overlay.transform, x=self._clamp(overlay.transform.x + dx, 0.0, 1.0), y=self._clamp(overlay.transform.y + dy, 0.0, 1.0))))
                return
        if (
            text is None
            or not text.visible
            or self._selected_object != text.id
            or self._mode is not None
        ):
            super().keyPressEvent(event)
            return
        key = event.key()
        modifiers = event.modifiers()
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            event.accept()
            updated = replace(text, visible=False)
            self._emit_text(updated)
            background = self._find_background()
            self._selected_object = background.id if background else "background"
            self.selected_changed.emit(False)
            self.selected_object_changed.emit(self._selected_object)
            self.update()
            return
        large_step = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        move_step = 0.02 if large_step else 0.005
        resize_step = 12 if large_step else 4
        x, y = text.transform.x, text.transform.y
        changed = False
        if key == Qt.Key.Key_Left:
            x = self._clamp(x - move_step, 0.0, 1.0)
            changed = True
        elif key == Qt.Key.Key_Right:
            x = self._clamp(x + move_step, 0.0, 1.0)
            changed = True
        elif key == Qt.Key.Key_Up:
            y = self._clamp(y - move_step, 0.0, 1.0)
            changed = True
        elif key == Qt.Key.Key_Down:
            y = self._clamp(y + move_step, 0.0, 1.0)
            changed = True
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            updated_style = replace(text.style, font_size=min(self._MAX_FONT_SIZE, text.style.font_size + resize_step))
            ratio = updated_style.font_size / max(1, text.style.font_size)
            updated = replace(text, style=updated_style, rect=replace(text.rect, width=min(1.0, text.rect.width * ratio), height=min(1.0, text.rect.height * ratio)))
            self._emit_text(updated)
            event.accept()
            return
        elif key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            updated_style = replace(text.style, font_size=max(self._MIN_FONT_SIZE, text.style.font_size - resize_step))
            ratio = updated_style.font_size / max(1, text.style.font_size)
            updated = replace(text, style=updated_style, rect=replace(text.rect, width=max(0.02, text.rect.width * ratio), height=max(0.02, text.rect.height * ratio)))
            self._emit_text(updated)
            event.accept()
            return
        if changed:
            self._set_alignment_guides(False, False)
            self._emit_text(replace(text, transform=replace(text.transform, x=x, y=y)))
            event.accept()
            return
        super().keyPressEvent(event)

    def _update_hover_state(self, point: QPointF):
        handle = self._handle_at(point)
        self._hover_handle = handle[0] if handle else None
        if handle is not None:
            self.setCursor(self._HANDLE_CURSORS.get(handle[0], Qt.CursorShape.ArrowCursor))
        elif self._hit_object(point) is not None or (
            self._document is None and self._legacy_display_rect().contains(point)
        ):
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.unsetCursor()

    def _draw_handle_icon(self, painter: QPainter, name: str, center: QPointF):
        """白底圆形按钮 + 线性图标，参考剪映/Canva 的选中框。"""

        radius = self._HANDLE_RADIUS
        painter.save()
        painter.setPen(QPen(QColor(0, 0, 0, 60), 1))
        painter.setBrush(QColor(255, 255, 255, 245))
        painter.drawEllipse(center, radius, radius)
        pen = QPen(QColor(40, 48, 58), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        x, y, r = center.x(), center.y(), radius * 0.42
        if name == "delete":
            painter.drawLine(QPointF(x - r, y - r), QPointF(x + r, y + r))
            painter.drawLine(QPointF(x - r, y + r), QPointF(x + r, y - r))
        elif name == "rotate":
            painter.drawArc(QRectF(x - r, y - r, r * 2, r * 2), 40 * 16, 280 * 16)
            tip = QPointF(x + r * math.cos(math.radians(40)), y - r * math.sin(math.radians(40)))
            painter.drawLine(tip, QPointF(tip.x() + r * 0.55, tip.y() + r * 0.1))
            painter.drawLine(tip, QPointF(tip.x() - r * 0.05, tip.y() - r * 0.6))
        elif name == "duplicate":
            painter.drawRoundedRect(QRectF(x - r, y - r * 0.4, r * 1.3, r * 1.4), 1.5, 1.5)
            painter.drawRoundedRect(QRectF(x - r * 0.3, y - r, r * 1.3, r * 1.4), 1.5, 1.5)
        else:
            painter.drawLine(QPointF(x - r, y - r), QPointF(x + r, y + r))
            for tip, sign in ((QPointF(x + r, y + r), -1), (QPointF(x - r, y - r), 1)):
                painter.drawLine(tip, QPointF(tip.x() + sign * r * 0.75, tip.y()))
                painter.drawLine(tip, QPointF(tip.x(), tip.y() + sign * r * 0.75))
        painter.restore()

    def _draw_selection(self, painter: QPainter, obj: RenderObject):
        frame = self._frame(obj)
        if frame is None:
            return
        rect, center, angle = frame
        rect = rect.adjusted(-self._CHROME_PAD, -self._CHROME_PAD, self._CHROME_PAD, self._CHROME_PAD)
        painter.save()
        painter.translate(center)
        painter.rotate(angle)
        painter.translate(-center)
        painter.setPen(QPen(self._CHROME_COLOR, 1.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)
        painter.restore()
        for name, position in self._handle_points(obj).items():
            if name.startswith("width"):
                painter.setPen(QPen(self._CHROME_COLOR, 1.5))
                painter.setBrush(QColor(255, 255, 255))
                side = self._EDGE_HANDLE
                painter.drawRect(QRectF(position.x() - side, position.y() - side, side * 2, side * 2))
            else:
                self._draw_handle_icon(painter, name, position)

    def _source_pixmap(self) -> QPixmap:
        return self._background_pixmap if not self._background_pixmap.isNull() else self._pixmap

    def _background_box(self, obj: BackgroundObject | None) -> Box | None:
        pixmap = self._source_pixmap()
        if pixmap.isNull():
            return None
        size = self._export_size()
        source = (pixmap.width(), pixmap.height())
        if obj is not None:
            return background_geometry(obj, size, source)
        return background_box(source, size, scale=self._zoom, focus_x=self._background_x, focus_y=self._background_y)

    def _draw_background(self, painter: QPainter):
        """导出像素坐标系内绘制；放置矩形与导出共用。"""

        width, height = self._export_size()
        pixmap = self._source_pixmap()
        if pixmap.isNull():
            painter.fillRect(QRectF(0, 0, width, height), QColor(COLORS.raised))
            return
        painter.fillRect(QRectF(0, 0, width, height), _qcolor(BACKGROUND_FILL))
        box = self._background_box(self._find_background())
        if box is not None:
            painter.drawPixmap(
                QRectF(box.left, box.top, box.width, box.height),
                pixmap,
                QRectF(0, 0, pixmap.width(), pixmap.height()),
            )

    @staticmethod
    def _rotate_about(painter: QPainter, center_x: float, center_y: float, rotation: float):
        if abs(rotation) >= 0.05:
            painter.translate(center_x, center_y)
            painter.rotate(rotation)
            painter.translate(-center_x, -center_y)

    def _overlay_pixmap(self, path: str | None) -> QPixmap:
        if not path:
            return QPixmap()
        pixmap = self._overlay_pixmap_cache.get(path)
        if pixmap is None:
            if len(self._overlay_pixmap_cache) > 48:
                self._overlay_pixmap_cache.clear()
            pixmap = QPixmap(path)
            self._overlay_pixmap_cache[path] = pixmap
        return pixmap

    def _draw_overlay(self, painter: QPainter, item: RenderObject):
        box = self._overlay_box(item)
        target = QRectF(box.left, box.top, box.width, box.height)
        if isinstance(item, (ImageObject, StickerObject)):
            pixmap = self._overlay_pixmap(asset_path(item))
            if pixmap.isNull():
                return
            painter.save()
            painter.setOpacity(max(0.0, min(1.0, item.opacity)))
            self._rotate_about(painter, box.center_x, box.center_y, item.transform.rotation)
            painter.drawPixmap(target, pixmap, QRectF(0, 0, pixmap.width(), pixmap.height()))
            painter.restore()
            return
        if isinstance(item, ShapeObject):
            painter.save()
            self._rotate_about(painter, box.center_x, box.center_y, item.transform.rotation)
            stroke = _qcolor(item.stroke, "#FFDB4D")
            pen = QPen(stroke, max(1.0, float(item.stroke_width)))
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
            painter.setBrush(_qcolor(item.fill) if item.fill else Qt.BrushStyle.NoBrush)
            if item.shape_type == "circle":
                painter.drawEllipse(target)
            elif item.shape_type == "arrow":
                painter.drawLine(target.bottomLeft(), target.topRight())
                head = arrow_head_size(box)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(stroke)
                painter.drawPolygon(QPolygonF((
                    target.topRight(),
                    QPointF(target.right() - head, target.top()),
                    QPointF(target.right(), target.top() + head),
                )))
            else:
                painter.drawRect(target)
            painter.restore()

    def _qt_family(self, font_path: str | None) -> str:
        key = font_path or ""
        family = self._font_family_cache.get(key)
        if family is None:
            font_id = QFontDatabase.addApplicationFont(font_path) if font_path else -1
            loaded = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
            family = loaded[0] if loaded else self.font().family()
            self._font_family_cache[key] = family
            self._font_id_cache[key] = font_id
        return family

    def _is_variable_font(self, font_path: str | None) -> bool:
        if not font_path:
            return False
        cached = self._variable_font_cache.get(font_path)
        if cached is None:
            try:
                axes = load_font(font_path, 32).get_variation_axes()
                cached = any(axis.get("name") in (b"Weight", "Weight") for axis in axes)
            except (AttributeError, OSError):
                cached = False
            self._variable_font_cache[font_path] = cached
        return cached

    def _qt_font(self, font_path: str | None, size: int, weight: int) -> QFont:
        font = QFont(self._qt_family(font_path))
        font.setPixelSize(max(1, int(size)))
        # 只给可变字体设字重，静态字体用文件本身字重，避免 Qt 合成粗体。
        if self._is_variable_font(font_path):
            font.setVariableAxis(QFont.Tag("wght"), float(weight))
            font.setWeight(
                QFont.Weight.Black if weight >= 850 else QFont.Weight.Bold if weight >= 650 else QFont.Weight.Normal
            )
        return font

    def _glyph_path(self, layout: TextLayout) -> tuple[QPainterPath, tuple, tuple]:
        """相对文字区域左上角构建字形路径；拖动只平移，路径可复用。"""

        key = (
            layout.font_size,
            layout.font_weight,
            tuple(
                (line.text, line.runs, round(line.origin_x - layout.area.left, 2), round(line.baseline - layout.area.top, 2))
                for line in layout.lines
            ),
        )
        cached = self._glyph_path_cache.get(key)
        if cached is not None:
            return cached[0], cached[1], key
        path = QPainterPath()
        emoji = []
        for line in layout.lines:
            for run in line.runs:
                x = line.origin_x - layout.area.left + run.offset
                y = line.baseline - layout.area.top
                if run.emoji:
                    emoji.append((x, y, run.text, run.font_path))
                    continue
                path.addText(QPointF(x, y), self._qt_font(run.font_path, layout.font_size, layout.font_weight), run.text)
        if len(self._glyph_path_cache) > 64:
            self._glyph_path_cache.clear()
            self._outline_cache.clear()
        self._glyph_path_cache[key] = (path, tuple(emoji))
        return path, tuple(emoji), key

    def _outline(self, path: QPainterPath, key: tuple, radius: float) -> QPainterPath:
        """描边轮廓与字形合并为一块，半透明填充时不会在重叠处加深。"""

        cache_key = (key, round(radius, 2))
        cached = self._outline_cache.get(cache_key)
        if cached is None:
            if radius > 0:
                stroker = QPainterPathStroker()
                stroker.setWidth(radius * 2)
                stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
                cached = stroker.createStroke(path).united(path)
            else:
                cached = QPainterPath(path)
            self._outline_cache[cache_key] = cached
        return cached

    @staticmethod
    def _round_pen(color: QColor, radius: float) -> QPen:
        # Pillow 描边按半径向外扩，Qt 画笔以路径为中心，宽度取两倍。
        pen = QPen(color, max(0.0, radius) * 2)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        return pen

    def _draw_text(self, painter: QPainter, text: TextObject):
        self._last_resolved_font = resolve_font_selection(text.style.font_family)
        layout = text_layout(text, self._export_size())
        if not layout.lines:
            return
        paint = text_paint(text)
        primary = next((run.font_path for line in layout.lines for run in line.runs if not run.emoji), None)
        self._last_qt_font_family = self._qt_family(primary)
        self._last_qt_font_id = self._font_id_cache.get(primary or "", -1)
        path, emoji, key = self._glyph_path(layout)
        area = layout.area
        stroke = max(0, int(paint.stroke_width))
        outer = max(0, int(paint.outer_stroke_width)) if paint.outer_stroke else 0
        painter.save()
        self._rotate_about(painter, area.center_x, area.center_y, text.transform.rotation)
        if paint.backdrop:
            backdrop = backdrop_box(layout.ink, layout.font_size)
            radius = backdrop_radius(layout.font_size)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_qcolor(paint.backdrop))
            painter.drawRoundedRect(QRectF(backdrop.left, backdrop.top, backdrop.width, backdrop.height), radius, radius)
        painter.translate(area.left, area.top)
        if paint.shadow:
            offset = shadow_offset(layout.font_size)
            shadow = self._outline(path, key, stroke + outer + 1)
            painter.fillPath(shadow.translated(offset, offset), QColor(0, 0, 0, SHADOW_ALPHA))
        if outer:
            painter.strokePath(path, self._round_pen(_qcolor(paint.outer_stroke), stroke + outer))
        if stroke:
            painter.strokePath(path, self._round_pen(_qcolor(paint.stroke, "#111111"), stroke))
        painter.fillPath(path, _qcolor(paint.fill, "#FFE438"))
        for x, y, value, font_path in emoji:
            painter.setFont(self._qt_font(font_path, layout.font_size, layout.font_weight))
            painter.setPen(QColor(0, 0, 0))
            painter.drawText(QPointF(x, y), value)
        painter.restore()

    def paintEvent(self, event):
        if self._background_pixmap.isNull() and self._pixmap.isNull():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.palette().window())
        canvas = self._canvas_rect()
        # 内容在导出像素坐标系中绘制后整体缩放，与 Pillow 导出逐像素对应。
        export_width, export_height = self._export_size()
        painter.save()
        painter.translate(canvas.left(), canvas.top())
        painter.scale(canvas.width() / max(1, export_width), canvas.height() / max(1, export_height))
        painter.setClipRect(QRectF(0, 0, export_width, export_height))
        self._draw_background(painter)
        for item in self._layered_objects():
            if not item.visible:
                continue
            if isinstance(item, TextObject):
                if item.text.strip():
                    self._draw_text(painter, item)
            else:
                self._draw_overlay(painter, item)
        painter.restore()
        selected = self._selected_editable()
        if self._guide_vertical:
            painter.setPen(QPen(QColor(66, 215, 255, 220), 1))
            painter.drawLine(QPointF(canvas.center().x(), canvas.top()), QPointF(canvas.center().x(), canvas.bottom()))
        if self._guide_horizontal:
            painter.setPen(QPen(QColor(66, 215, 255, 220), 1))
            painter.drawLine(QPointF(canvas.left(), canvas.center().y()), QPointF(canvas.right(), canvas.center().y()))
        if selected is not None:
            self._draw_selection(painter, selected)
        elif self._document is None and not self._legacy_display_rect().isNull() and self._selected_object == "title":
            painter.setPen(QPen(QColor(255, 255, 255, 210), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self._legacy_display_rect().adjusted(-4, -4, 4, 4))
        elif self._selected_object == "background" or (self._find_background() and self._selected_object == self._find_background().id):
            painter.setPen(QPen(Qt.GlobalColor.cyan, 2, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(canvas.adjusted(1, 1, -1, -1))
        painter.end()
