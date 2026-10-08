"""画布手势：拖动、缩放/旋转/改宽手柄、中心吸附、键盘微调和提交。

混入 CoverCanvas；常量和状态都在 CoverCanvas 上。
"""

from __future__ import annotations

import math
from dataclasses import replace

from PySide6.QtCore import QPointF, Qt

from autoslice_cover.document_layout import (
    BACKGROUND_SCALE_MAX,
    BACKGROUND_SCALE_MIN,
    focus_after_drag,
)

from .cover_model import (
    BackgroundObject,
    ImageObject,
    RenderObject,
    ShapeObject,
    StickerObject,
    TextObject,
    resize_text_style,
    set_profile_override,
)


class CanvasGestureMixin:
    def _set_local_effective(self, obj: object):
        if self._document is None:
            return
        if self._profile_key not in self._document.profiles:
            return
        self._document = set_profile_override(self._document, self._profile_key, obj)

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
        # 画布自己的手势自己请求重画，不依赖编辑器回调间接刷新。
        self.update()

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
        # 拖动中也要重画：此前靠编辑器的旧回调间接刷新，回调删掉后背景要等松手才跟上。
        self.update()

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

    @classmethod
    def _snap_to_center(
        cls,
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
        snap_x = abs((candidate_x - centered_x) * frame_width) <= cls._CENTER_SNAP_THRESHOLD_PX
        snap_y = abs((candidate_y - centered_y) * frame_height) <= cls._CENTER_SNAP_THRESHOLD_PX
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

    def _scaled_text(self, obj: TextObject, font_size: int) -> TextObject:
        """按新字号等比缩放文字：描边、行宽一起跟随，左上角不动。"""

        size = int(self._clamp(font_size, self._MIN_FONT_SIZE, self._MAX_FONT_SIZE))
        actual = size / max(1, obj.style.font_size)
        width = self._clamp(obj.rect.width * actual, 0.02, 3.0)
        return replace(
            obj,
            style=resize_text_style(obj.style, size),
            rect=replace(obj.rect, width=width, height=self._clamp(obj.rect.height * actual, 0.02, 3.0)),
            wrap=replace(obj.wrap, max_width=min(1.0, width)),
        )

    def _scale_gesture(self, point: QPointF):
        """右下角：以左上角为锚点等比缩放；文字改字号和行宽，素材改缩放。"""

        obj, (rect, center, angle) = self._start_object, self._start_frame
        anchor = self._rotate(rect.topLeft(), center, angle)
        start = math.hypot(self._press.x() - anchor.x(), self._press.y() - anchor.y())
        now = math.hypot(point.x() - anchor.x(), point.y() - anchor.y())
        ratio = self._clamp(now / max(8.0, start), 0.15, 6.0)
        if isinstance(obj, TextObject):
            self._emit_text(self._scaled_text(obj, round(obj.style.font_size * ratio)), commit=False)
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
        norm = self._free_norm(point)
        hit = self._hit_object(point)
        # 画布外只认对象本身（大字可能超出画布）；点空白处不切到背景。
        if norm is None or (hit is None and not self._canvas_rect().contains(point)):
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if hit is not None and getattr(hit, "locked", False):
            # 锁定对象只能选中查看和改属性，不能拖动。
            self._selected_object = hit.id
            self.selected_changed.emit(isinstance(hit, TextObject))
            self.selected_object_changed.emit(hit.id)
            self._press = point
            self.update()
            event.accept()
            return
        text = hit if isinstance(hit, TextObject) else None
        overlay = hit if isinstance(hit, (ImageObject, StickerObject, ShapeObject)) else None
        if text is not None:
            self._selected_object = text.id
            self._mode = "text"
            self._start_transform, self._start_rect = text.transform, text.rect
            self._start_font_size = int(text.style.font_size)
            self._drag_offset = QPointF(norm[0] - self._start_transform.x, norm[1] - self._start_transform.y)
            self.selected_changed.emit(True)
            self.selected_object_changed.emit(text.id)
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
            self._start_background = (background.pan_x, background.pan_y, background.scale) if background else None
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
            norm = self._free_norm(point)
            if obj is not None and norm is not None:
                candidate_x = norm[0] - self._drag_offset.x()
                candidate_y = norm[1] - self._drag_offset.y()
                # 吸附以贴合文字的框为准：框中心对齐画面中心。
                box, area = self._text_box(obj)
                width_px, height_px = self._export_size()
                offset_x = (box.left - area.left) / width_px
                offset_y = (box.top - area.top) / height_px
                display = self._display_rect(obj)
                # 鼠标不夹在画布内，文字可以拖出边缘；只保证至少一小截留在画面里。
                box_left, box_top = self._keep_visible(
                    candidate_x + offset_x, candidate_y + offset_y,
                    display.width() / max(1.0, image.width()), display.height() / max(1.0, image.height()),
                )
                box_x, box_y, snap_x, snap_y = self._snap_to_center(
                    box_left,
                    box_top,
                    display.width(),
                    display.height(),
                    image.width(),
                    image.height(),
                )
                self._set_alignment_guides(snap_x, snap_y)
                updated = replace(obj, transform=replace(obj.transform, x=box_x - offset_x, y=box_y - offset_y))
                self._emit_text(updated, commit=False)
                self.update()
        elif self._mode == "object":
            norm = self._free_norm(point)
            obj = next((item for item in self._find_overlay_objects() if item.id == self._selected_object), None)
            if obj is not None and norm is not None:
                rect = self._object_display_rect(obj)
                candidate_x, candidate_y = self._keep_visible(
                    norm[0] - self._drag_offset.x(), norm[1] - self._drag_offset.y(),
                    rect.width() / max(1.0, image.width()), rect.height() / max(1.0, image.height()),
                )
                self._emit_overlay(replace(obj, transform=replace(obj.transform, x=candidate_x, y=candidate_y)), commit=False)
        else:
            dx = (point.x() - self._press.x()) / max(1.0, image.width())
            dy = (point.y() - self._press.y()) / max(1.0, image.height())
            obj = self._find_background()
            if obj and self._start_background is not None:
                px, py, scale = self._start_background
                width, height = self._export_size()
                drawn = self._background_box(replace(obj, scale=scale))
                # focus 语义与导出一致，换算后画面跟手移动。
                pan_x = focus_after_drag(px, dx * width, width, drawn.width) if drawn else px
                pan_y = focus_after_drag(py, dy * height, height, drawn.height) if drawn else py
                self._emit_background(replace(obj, pan_x=pan_x, pan_y=pan_y, scale=scale), commit=False)
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self._hit_object(event.position())
            if isinstance(hit, TextObject):
                self.edit_requested.emit(hit.id)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

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
            self._settle_timer.start()
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
        if obj is None:
            return
        value = max(BACKGROUND_SCALE_MIN, min(BACKGROUND_SCALE_MAX, obj.scale + steps * 0.1))
        self._emit_background(replace(obj, scale=value))
        self.update()
        event.accept()

    _NUDGE_KEYS = {
        Qt.Key.Key_Left: (-1, 0),
        Qt.Key.Key_Right: (1, 0),
        Qt.Key.Key_Up: (0, -1),
        Qt.Key.Key_Down: (0, 1),
    }
    _DELETE_KEYS = (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
    _GROW_KEYS = (Qt.Key.Key_Plus, Qt.Key.Key_Equal)
    _SHRINK_KEYS = (Qt.Key.Key_Minus, Qt.Key.Key_Underscore)

    def _keyboard_target(self) -> RenderObject | None:
        """键盘操作的对象：选中的素材，或选中且可见的文字；手势进行中不响应。"""

        if self._mode is not None:
            return None
        overlay = next((item for item in self._find_overlay_objects() if item.id == self._selected_object), None)
        if overlay is not None:
            return overlay
        text = self._find_text()
        if text is not None and text.visible and self._selected_object == text.id:
            return text
        return None

    def keyPressEvent(self, event):
        """方向键微调（Shift 加大步长）、Delete 隐藏、+/- 改字号；锁定对象只提示。"""

        target = self._keyboard_target()
        key = event.key()
        large = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if target is None:
            super().keyPressEvent(event)
            return
        if target.locked and (key in self._DELETE_KEYS or key in self._NUDGE_KEYS):
            event.accept()
            self.locked_hint.emit()
            return
        if key in self._DELETE_KEYS:
            event.accept()
            self._emit_any(replace(target, visible=False))
            background = self._find_background()
            self._selected_object = background.id if background else "background"
            self.selected_changed.emit(False)
            self.selected_object_changed.emit(self._selected_object)
            self.update()
            return
        if key in self._NUDGE_KEYS:
            event.accept()
            dx, dy = self._NUDGE_KEYS[key]
            step = 0.02 if large else 0.005
            transform = target.transform
            self._set_alignment_guides(False, False)
            self._emit_any(replace(target, transform=replace(
                transform,
                x=self._clamp(transform.x + dx * step, -1.0, 1.0),
                y=self._clamp(transform.y + dy * step, -1.0, 1.0),
            )))
            return
        if isinstance(target, TextObject) and (key in self._GROW_KEYS or key in self._SHRINK_KEYS):
            event.accept()
            step = 12 if large else 4
            self._emit_text(self._scaled_text(target, target.style.font_size + (step if key in self._GROW_KEYS else -step)))
            return
        super().keyPressEvent(event)

    def _emit_any(self, obj: RenderObject):
        if isinstance(obj, TextObject):
            self._emit_text(obj)
        else:
            self._emit_overlay(obj)

    def _update_hover_state(self, point: QPointF):
        handle = self._handle_at(point)
        self._hover_handle = handle[0] if handle else None
        if handle is not None:
            self.setCursor(self._HANDLE_CURSORS.get(handle[0], Qt.CursorShape.ArrowCursor))
        elif self._hit_object(point) is not None:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.unsetCursor()
