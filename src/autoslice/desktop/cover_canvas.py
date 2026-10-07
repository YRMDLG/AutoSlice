"""AutoCover 的对象化编辑画布。

画布只处理显示和鼠标手势，持久化由 ``CoverEditorWidget`` 通过
``CoverDocument`` 完成。为了兼容 Phase 1 的调用方，本模块仍保留标题/底图
位置变化信号和 ``set_title_rect`` 等旧入口；新代码应使用 ``set_document``。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import QLabel

from autoslice_cover.fonts import FontResolution, resolve_font_selection

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

    _SAFE_MARGIN = 0.06
    _HANDLE = 9.0
    # 旧版 Web Canvas 的行为常量：移动到画布中心附近 8px 即吸附，
    # 缩放按右下角位移换算为字号比例。
    _CENTER_SNAP_THRESHOLD_PX = 8.0
    _MIN_FONT_SIZE = 24
    _MAX_FONT_SIZE = 320

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
        self._resize_corner: str | None = None
        self._start_font_size = 104
        self._safe_area_warning = False
        self._background_x = 0.5
        self._background_y = 0.5
        self._zoom = 1.0
        self._guide_vertical = False
        self._guide_horizontal = False
        self._hover_handle = False
        self._font_family_cache: dict[str, str] = {}
        self._font_id_cache: dict[str, int] = {}
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
        self._zoom = max(1.0, min(4.0, float(zoom)))
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

    def _display_rect(self, obj: TextObject) -> QRectF:
        image = self._canvas_rect()
        scale = max(0.01, float(obj.transform.scale or 1.0))
        width = min(obj.rect.width * scale, obj.wrap.max_width)
        height = obj.rect.height * scale
        return QRectF(
            image.left() + obj.transform.x * image.width(),
            image.top() + obj.transform.y * image.height(),
            width * image.width(),
            height * image.height(),
        )

    def _object_display_rect(self, obj: RenderObject) -> QRectF:
        """为图片/贴纸/形状提供轻量命中区域。"""

        image = self._canvas_rect()
        scale = max(0.05, float(obj.transform.scale or 1.0))
        if isinstance(obj, ShapeObject):
            width, height = obj.width * scale, obj.height * scale
        else:
            width, height = 0.24 * scale, 0.18 * scale
        return QRectF(
            image.left() + obj.transform.x * image.width(),
            image.top() + obj.transform.y * image.height(),
            min(0.9, width) * image.width(),
            min(0.9, height) * image.height(),
        )

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

    def _text_at(self, point: QPointF) -> TextObject | None:
        """按 z 顺序命中最上层可见文字。"""

        for item in sorted(self._find_texts(), key=lambda value: value.z_index, reverse=True):
            if item.visible and self._display_rect(item).contains(point):
                return item
        return None

    def _overlay_at(self, point: QPointF) -> RenderObject | None:
        for item in sorted(self._find_overlay_objects(), key=lambda value: value.z_index, reverse=True):
            if item.visible and self._object_display_rect(item).contains(point):
                return item
        return None

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

    def _corner_at(self, point: QPointF, rect: QRectF) -> str | None:
        if rect.isNull():
            return None
        # 旧版只有一个右下角 resize-handle；保留单一抓取点可避免把
        # 文字编辑误判成自由变形器，也让字号缩放手感与旧版一致。
        corner = rect.bottomRight()
        if QRectF(
            corner.x() - self._HANDLE,
            corner.y() - self._HANDLE,
            self._HANDLE * 2,
            self._HANDLE * 2,
        ).contains(point):
            return "br"
        return None

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

    def _resize_text(self, point: QPointF):
        obj = self._find_text()
        if obj is None or self._resize_corner is None:
            return
        canvas = self._canvas_rect()
        delta_x = point.x() - self._press.x()
        delta_y = point.y() - self._press.y()
        start_size = max(36.0, canvas.width() * self._start_rect.width, canvas.height() * self._start_rect.height)
        ratio = self._clamp(1.0 + (delta_x + delta_y) / (2.0 * start_size), 0.35, 2.5)
        font_size = int(round(self._start_font_size * ratio))
        font_size = int(self._clamp(font_size, self._MIN_FONT_SIZE, self._MAX_FONT_SIZE))
        updated = replace(
            obj,
            # 旧版字号变化后预览框会随文字自然尺寸更新；这里按同一比例
            # 更新逻辑框宽高，保持自动换行比例稳定，不使用鼠标直接改四边。
            rect=replace(
                obj.rect,
                width=self._clamp(self._start_rect.width * ratio, 0.02, 1.0),
                height=self._clamp(self._start_rect.height * ratio, 0.02, 1.0),
            ),
            style=replace(obj.style, font_size=font_size),
        )
        self._emit_text(updated, commit=False)
        self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        point = event.position()
        norm = self._norm_point(point)
        if norm is None:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        text = self._text_at(point) or self._find_text()
        overlay = self._overlay_at(point) if self._text_at(point) is None else None
        # 命中 A/B 中任意一块后，后续拖动和缩放必须使用被命中的那一块，
        # 不能继续拿上一次选中的 B 的矩形。
        rect = self._display_rect(text) if text is not None else self._object_display_rect(overlay) if overlay is not None else self._title_display_rect()
        corner = self._corner_at(point, rect) if text and text.visible else None
        if (text is not None and text.visible and (corner or rect.contains(point))) or (
            text is None and overlay is None and self._document is None and rect.contains(point)
        ):
            self._selected_object = text.id if text is not None else "title"
            self._mode = "text-resize" if corner else "text"
            self._resize_corner = corner
            if text is not None:
                self._start_transform, self._start_rect = text.transform, text.rect
                self._start_font_size = int(text.style.font_size)
            else:
                self._start_transform = Transform(x=self._legacy_title_rect.left(), y=self._legacy_title_rect.top())
                self._start_rect = Rect(width=self._legacy_title_rect.width(), height=self._legacy_title_rect.height())
            if not corner:
                self._drag_offset = QPointF(norm[0] - self._start_transform.x, norm[1] - self._start_transform.y)
            self.selected_changed.emit(True)
            self.selected_object_changed.emit(text.id if text is not None else "title")
        elif overlay is not None and overlay.visible and rect.contains(point):
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
        if self._mode == "text-resize":
            self._resize_text(event.position())
        elif self._mode == "text":
            obj = self._find_text()
            norm = self._norm_point(event.position(), clamp=True)
            if obj is not None and norm is not None:
                candidate_x = self._clamp(norm[0] - self._drag_offset.x(), 0.0, 1.0)
                candidate_y = self._clamp(norm[1] - self._drag_offset.y(), 0.0, 1.0)
                display = self._display_rect(obj)
                x, y, snap_x, snap_y = self._snap_to_center(
                    candidate_x,
                    candidate_y,
                    display.width(),
                    display.height(),
                    image.width(),
                    image.height(),
                )
                self._set_alignment_guides(snap_x, snap_y)
                updated = replace(obj, transform=replace(obj.transform, x=x, y=y))
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
            norm = self._norm_point(event.position(), clamp=True)
            obj = next((item for item in self._find_overlay_objects() if item.id == self._selected_object), None)
            if obj is not None and norm is not None:
                candidate_x = self._clamp(norm[0] - self._drag_offset.x(), 0.0, 1.0)
                candidate_y = self._clamp(norm[1] - self._drag_offset.y(), 0.0, 1.0)
                self._emit_overlay(replace(obj, transform=replace(obj.transform, x=candidate_x, y=candidate_y)), commit=False)
        else:
            dx = (event.position().x() - self._press.x()) / max(1.0, image.width())
            dy = (event.position().y() - self._press.y()) / max(1.0, image.height())
            obj = self._find_background()
            if obj:
                px, py, scale = self._start_background
                self._emit_background(
                    replace(
                        obj,
                        pan_x=self._clamp(px + dx, 0.0, 1.0),
                        pan_y=self._clamp(py + dy, 0.0, 1.0),
                        scale=scale,
                    ),
                    commit=False,
                )
            else:
                self._background_x = self._clamp(self._start_background[0] + dx, 0.0, 1.0)
                self._background_y = self._clamp(self._start_background[1] + dy, 0.0, 1.0)
                self.background_position_changed.emit(self._background_x, self._background_y)
                self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._mode is not None:
            mode = self._mode
            self._commit_gesture()
            self._mode = None
            self._resize_corner = None
            self._set_alignment_guides(False, False)
            if self.mouseGrabber() is self:
                self.releaseMouse()
            self.unsetCursor()
            self.update()
            (self.title_position_finished if mode.startswith("text") else self.background_position_finished).emit()
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
        value = max(1.0, min(4.0, (obj.scale if obj else self._zoom) + steps * 0.1))
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
        text = self._find_text()
        rect = self._title_display_rect()
        self._hover_handle = bool(text and text.visible and self._corner_at(point, rect))
        if self._hover_handle:
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif text and text.visible and rect.contains(point):
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.unsetCursor()

    def _draw_background(self, painter: QPainter, canvas: QRectF):
        pixmap = self._background_pixmap if not self._background_pixmap.isNull() else self._pixmap
        if pixmap.isNull():
            painter.fillRect(canvas, QColor(COLORS.raised))
            return
        obj = self._find_background()
        scale = obj.scale if obj else self._zoom
        pan_x = obj.pan_x if obj else self._background_x
        pan_y = obj.pan_y if obj else self._background_y
        fit_mode = obj.fit_mode if obj else "cover"
        factor = (min if fit_mode == "contain" else max)(canvas.width() / pixmap.width(), canvas.height() / pixmap.height()) * scale
        draw_w, draw_h = pixmap.width() * factor, pixmap.height() * factor
        if fit_mode == "contain":
            painter.fillRect(canvas, QColor(COLORS.player))
        center_x = canvas.center().x() + (pan_x - 0.5) * (draw_w - canvas.width())
        center_y = canvas.center().y() + (pan_y - 0.5) * (draw_h - canvas.height())
        target = QRectF(center_x - draw_w / 2, center_y - draw_h / 2, draw_w, draw_h)
        painter.save()
        painter.setClipRect(canvas)
        painter.drawPixmap(target, pixmap, QRectF(0, 0, pixmap.width(), pixmap.height()))
        painter.restore()

    def _draw_overlay(self, painter: QPainter, canvas: QRectF, item: RenderObject):
        rect = self._object_display_rect(item)
        if isinstance(item, (ImageObject, StickerObject)):
            path = item.asset.path if item.asset else None
            pixmap = QPixmap(path) if path else QPixmap()
            if pixmap.isNull():
                return
            scaled = pixmap.scaled(rect.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            target = QRectF(rect.left(), rect.top(), scaled.width(), scaled.height())
            painter.save()
            painter.setOpacity(max(0.0, min(1.0, item.opacity)))
            if abs(item.transform.rotation) > 0.01:
                painter.translate(target.center())
                painter.rotate(item.transform.rotation)
                painter.translate(-target.center())
            painter.drawPixmap(target, scaled)
            painter.restore()
            return
        if isinstance(item, ShapeObject):
            painter.save()
            pen = QPen(QColor(item.stroke), max(1.0, float(item.stroke_width) * canvas.width() / max(1, self._canvas_ratio[0])))
            painter.setPen(pen)
            painter.setBrush(QColor(item.fill) if item.fill else Qt.BrushStyle.NoBrush)
            if abs(item.transform.rotation) > 0.01:
                painter.translate(rect.center())
                painter.rotate(item.transform.rotation)
                painter.translate(-rect.center())
            if item.shape_type == "circle":
                painter.drawEllipse(rect)
            elif item.shape_type == "arrow":
                painter.drawLine(rect.bottomLeft(), rect.topRight())
                head = min(rect.width(), rect.height()) * 0.25
                painter.drawPolygon(QPolygonF((rect.topRight(), QPointF(rect.right() - head, rect.top()), QPointF(rect.right(), rect.top() + head))))
            else:
                painter.drawRect(rect)
            painter.restore()

    def _draw_text(self, painter: QPainter, canvas: QRectF, text: TextObject):
        rect = self._display_rect(text)
        style = text.style
        resolution = resolve_font_selection(style.font_family)
        self._last_resolved_font = resolution
        font_path = str(resolution.path or "")
        font_id = self._font_id_cache.get(font_path, -1)
        family = self._font_family_cache.get(font_path)
        if family is None and font_path:
            font_id = QFontDatabase.addApplicationFont(font_path)
            loaded = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
            family = loaded[0] if loaded else resolution.family
            self._font_family_cache[font_path] = family
            self._font_id_cache[font_path] = font_id
        family = family or resolution.family or self.font().family()
        self._last_qt_font_family = family
        self._last_qt_font_id = font_id
        font = QFont(family)
        base_pixel_size = max(8, int(style.font_size * canvas.width() / max(1, self._canvas_ratio[0])))
        font.setPixelSize(base_pixel_size)
        weight = max(100, min(1000, int(style.font_weight)))
        # PySide6 绑定要求 QFont.Weight 枚举，不能直接传 CSS 数值整数。
        font.setWeight(
            QFont.Weight.Black
            if weight >= 850
            else QFont.Weight.Bold
            if weight >= 650
            else QFont.Weight.Normal
        )
        # 与 Pillow renderer 的独立文字块 fit 保持同一行为：默认只搜索
        # 1~2 行，优先整行，禁止自动生成 1~2 字尾行。Qt 使用 QFontMetrics
        # 做同样的真实字宽测量，避免画布和预览在换行上再次漂移。
        explicit = "\n" in text.text
        source = [part.strip() for part in text.text.replace("\r\n", "\n").split("\n") if part.strip()] or [" "]
        max_width_px = max(24.0, rect.width())
        max_height_px = max(24.0, rect.height())
        max_lines = max(2, min(8, int(text.wrap.max_lines))) if explicit else 2
        selected_lines = source[:max_lines] if explicit else None
        selected_font = QFont(font)
        for pixel_size in range(base_pixel_size, max(12, int(base_pixel_size * 0.45)) - 1, -2):
            candidate_font = QFont(font)
            candidate_font.setPixelSize(pixel_size)
            metrics = QFontMetrics(candidate_font)
            candidates = [tuple(source)] if explicit else [(text.text.strip(),)]
            if not explicit and len(text.text.strip()) > 1:
                value = text.text.strip()
                candidates.extend((value[:index], value[index:]) for index in range(1, len(value)))
            fitting: list[tuple[tuple[float, ...], tuple[str, ...]]] = []
            for candidate in candidates:
                if len(candidate) > max_lines or any(not item for item in candidate):
                    continue
                widths = [metrics.horizontalAdvance(item) for item in candidate]
                gap = max(8.0, pixel_size * max(0.08, min(0.30, float(style.line_spacing) - 1.0)))
                total_height = sum(metrics.height() for _ in candidate) + gap * (len(candidate) - 1)
                if max(widths, default=0) > max_width_px or total_height > max_height_px:
                    continue
                if len(candidate) > 1 and not explicit and len(candidate[-1]) <= 2:
                    continue
                balance = abs(widths[0] - widths[-1]) if len(candidate) > 1 else 0
                fitting.append(((float(len(candidate) - 1), float(balance)), candidate))
            if fitting:
                _score, selected_lines = min(fitting, key=lambda item: item[0])
                selected_font = candidate_font
                break
        lines = list(selected_lines or source[:max_lines])
        font = selected_font
        line_height = max(1.0, font.pixelSize() * float(style.line_spacing))
        align = text.align if text.align in {"left", "center", "right"} else "left"
        effective_stroke_width = 4 if text.copy_role == "A" and style.stroke_width >= 6 else style.stroke_width
        stroke_width = max(0.0, float(effective_stroke_width) * canvas.width() / max(1, self._canvas_ratio[0]))

        def build_path(offset_x: float = 0.0, offset_y: float = 0.0) -> QPainterPath:
            path = QPainterPath()
            metrics = painter.fontMetrics()
            for index, line in enumerate(lines):
                line_width = metrics.horizontalAdvance(line)
                if align == "center":
                    x = rect.center().x() - line_width / 2
                elif align == "right":
                    x = rect.right() - line_width
                else:
                    x = rect.left()
                baseline = rect.top() + metrics.ascent() + index * line_height
                path.addText(x + offset_x, baseline + offset_y, font, line)
            return path

        painter.save()
        painter.setFont(font)
        if abs(text.transform.rotation) > 0.01:
            painter.translate(rect.center())
            painter.rotate(text.transform.rotation)
            painter.translate(-rect.center())
        if style.shadow:
            shadow = build_path(2.0, 3.0)
            painter.fillPath(shadow, QColor(0, 0, 0, 184))
            painter.strokePath(shadow, QPen(QColor(0, 0, 0, 184), stroke_width + 1.0))
        glyphs = build_path()
        painter.fillPath(glyphs, QColor(style.fill_color))
        painter.strokePath(glyphs, QPen(QColor(style.stroke_color), max(1.0, stroke_width)))
        painter.restore()

    def paintEvent(self, event):
        if self._background_pixmap.isNull() and self._pixmap.isNull():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.palette().window())
        canvas = self._canvas_rect()
        self._draw_background(painter, canvas)
        texts = self._find_texts()
        overlays = self._find_overlay_objects()
        for item in sorted(overlays, key=lambda value: value.z_index):
            if item.visible:
                self._draw_overlay(painter, canvas, item)
        for item in sorted(texts, key=lambda value: value.z_index):
            if item.visible:
                self._draw_text(painter, canvas, item)
        text = self._find_text()
        if text and self._selected_object == text.id:
            rect = self._display_rect(text).adjusted(-4, -4, 4, 4)
            painter.setPen(QPen(QColor(255, 255, 255, 210), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)
            if self._hover_handle or self._mode == "text-resize":
                painter.setBrush(QColor(255, 255, 255, 230))
                point = rect.bottomRight()
                painter.drawRect(QRectF(point.x() - 3, point.y() - 3, 6, 6))
            if self._guide_vertical:
                painter.setPen(QPen(QColor(66, 215, 255, 220), 1))
                painter.drawLine(QPointF(canvas.center().x(), canvas.top()), QPointF(canvas.center().x(), canvas.bottom()))
            if self._guide_horizontal:
                painter.setPen(QPen(QColor(66, 215, 255, 220), 1))
                painter.drawLine(QPointF(canvas.left(), canvas.center().y()), QPointF(canvas.right(), canvas.center().y()))
        elif overlay := next((item for item in overlays if item.id == self._selected_object), None):
            painter.setPen(QPen(QColor(255, 219, 77, 220), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self._object_display_rect(overlay).adjusted(-4, -4, 4, 4))
        elif self._selected_object == "background" or (self._find_background() and self._selected_object == self._find_background().id):
            painter.setPen(QPen(Qt.GlobalColor.cyan, 2, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(canvas.adjusted(1, 1, -1, -1))
        painter.end()
