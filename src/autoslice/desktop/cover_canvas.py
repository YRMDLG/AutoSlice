"""AutoCover 的对象化编辑画布。

画布只处理显示和鼠标手势，持久化由 ``CoverEditorWidget`` 通过
``CoverDocument`` 完成。为了兼容 Phase 1 的调用方，本模块仍保留标题/底图
位置变化信号和 ``set_title_rect`` 等旧入口；新代码应使用 ``set_document``。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import QLabel

from autoslice_cover.document_layout import (
    BACKGROUND_SCALE_MAX,
    BACKGROUND_SCALE_MIN,
    Box,
    TextLayout,
)
from autoslice_cover.fonts import FontResolution

from .cover_canvas_gestures import CanvasGestureMixin
from .cover_canvas_paint import CanvasPaintMixin
from .cover_layout import (
    overlay_geometry,
    shape_geometry,
    text_layout,
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


class CoverCanvas(CanvasPaintMixin, CanvasGestureMixin, QLabel):
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
    # 双击文字：编辑器把焦点交给文案框并全选。
    edit_requested = Signal(str)
    # 对锁定对象做了拖动/删除等操作：编辑器提示先解锁。
    locked_hint = Signal()

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
        # 分层位图缓存：{层名: (键, 位图, 左上角)}；选中对象单独成层，拖动只平移。
        self._layer_cache: dict[str, tuple[tuple, QPixmap, QPointF]] = {}
        self._active_base: RenderObject | None = None
        # 释放鼠标后先沿用平移的选中层，空闲时再按精确位置重画，释放不卡顿。
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(160)
        self._settle_timer.timeout.connect(self.update)
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

    def _free_norm(self, point: QPointF) -> tuple[float, float] | None:
        """不夹到画布内的归一化坐标：对象可以拖出画布边缘。"""

        image = self._canvas_rect()
        if image.isNull():
            return None
        return (
            (point.x() - image.left()) / max(1.0, image.width()),
            (point.y() - image.top()) / max(1.0, image.height()),
        )

    # 拖出画布时至少留这么多在画面里，避免整块拖丢。
    _KEEP_VISIBLE = 0.05

    def _keep_visible(self, left: float, top: float, width: float, height: float) -> tuple[float, float]:
        keep = self._KEEP_VISIBLE
        return (
            self._clamp(left, keep - width, 1.0 - keep),
            self._clamp(top, keep - height, 1.0 - keep),
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
        if frame is None or getattr(obj, "locked", False):
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


