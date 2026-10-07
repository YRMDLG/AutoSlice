"""画布绘制：底图、分层位图缓存、文字描边阴影、素材与形状、选中框和手柄。

混入 CoverCanvas；常量和状态都在 CoverCanvas 上。
"""

from __future__ import annotations

import math
from dataclasses import replace

from PySide6.QtCore import QPointF, QRectF, Qt
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
    QTransform,
)

from autoslice_cover.document_layout import (
    BACKGROUND_FILL,
    SHADOW_ALPHA,
    Box,
    TextLayout,
    arrow_head_size,
    backdrop_box,
    backdrop_radius,
    load_font,
    shadow_offset,
)
from autoslice_cover.document_render import rgba

from .cover_layout import (
    asset_path,
    background_geometry,
    font_config,
    resolved_font,
    text_layout,
    text_paint,
)
from .cover_model import (
    BackgroundObject,
    ImageObject,
    RenderObject,
    ShapeObject,
    StickerObject,
    TextObject,
)
from .qt_preview.theme import COLORS


def _qcolor(value: str | None, default: str = "#000000") -> QColor:
    # 模型颜色为 #RRGGBBAA，Qt 的同长度写法是 #AARRGGBB，统一走同一解析。
    return QColor(*rgba(value, default))


class CanvasPaintMixin:
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
        if getattr(obj, "locked", False):
            # 锁定：灰色虚线框、没有手柄。
            painter.setPen(QPen(QColor(200, 205, 212, 220), 1.5, Qt.PenStyle.DashLine))
        else:
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

    def _background_box(self, obj: BackgroundObject | None) -> Box | None:
        pixmap = self._background_pixmap
        if pixmap.isNull():
            return None
        size = self._export_size()
        source = (pixmap.width(), pixmap.height())
        return background_geometry(obj, size, source) if obj is not None else None

    def _draw_background(self, painter: QPainter):
        """导出像素坐标系内绘制；放置矩形与导出共用。"""

        width, height = self._export_size()
        pixmap = self._background_pixmap
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
        self._last_resolved_font = resolved_font(text.style.font_family)
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

    @staticmethod
    def _paints(item: RenderObject) -> bool:
        return bool(item.visible) and not (isinstance(item, TextObject) and not item.text.strip())

    def _paint_layer(
        self, name: str, objects: tuple[RenderObject, ...], bounds: QRectF, *, clip: bool,
    ) -> tuple[QPixmap, QPointF] | None:
        """把对象画进对齐设备像素的透明位图；内容与位置不变时直接复用。

        绘制仍在导出像素坐标系内完成，只是目标从窗口换成位图，结果与直接绘制一致。
        """

        if not objects or bounds.isEmpty():
            return None
        canvas = self._canvas_rect()
        dpr = max(1.0, float(self.devicePixelRatioF()))
        left = math.floor(bounds.left() * dpr) / dpr
        top = math.floor(bounds.top() * dpr) / dpr
        width = max(1, math.ceil(bounds.right() * dpr) - math.floor(bounds.left() * dpr))
        height = max(1, math.ceil(bounds.bottom() * dpr) - math.floor(bounds.top() * dpr))
        export_width, export_height = self._export_size()
        key = (
            left, top, width, height, dpr, canvas.getRect(), (export_width, export_height),
            clip, font_config(), objects,
        )
        cached = self._layer_cache.get(name)
        if cached is not None and cached[0] == key:
            return cached[1], cached[2]
        pixmap = QPixmap(width, height)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(dpr, dpr)
        painter.translate(canvas.left() - left, canvas.top() - top)
        painter.scale(canvas.width() / max(1, export_width), canvas.height() / max(1, export_height))
        if clip:
            painter.setClipRect(QRectF(0, 0, export_width, export_height))
        for item in objects:
            if isinstance(item, TextObject):
                self._draw_text(painter, item)
            else:
                self._draw_overlay(painter, item)
        painter.end()
        pixmap.setDevicePixelRatio(dpr)
        origin = QPointF(left, top)
        self._layer_cache[name] = (key, pixmap, origin)
        return pixmap, origin

    def _object_bounds(self, obj: RenderObject) -> QRectF:
        """对象绘制范围（屏幕坐标）：选中框外扩描边、阴影和底条余量，并计入旋转。"""

        frame = self._frame(obj)
        if frame is None:
            return self._canvas_rect()
        rect, center, angle = frame
        if isinstance(obj, TextObject):
            paint = text_paint(obj)
            margin = obj.style.font_size * 0.5 + paint.stroke_width + paint.outer_stroke_width + 8
        else:
            margin = 8 + float(getattr(obj, "stroke_width", 0) or 0)
        margin = margin / self._screen_to_export() + 2
        rect = rect.adjusted(-margin, -margin, margin, margin)
        if abs(angle) >= 0.05:
            rect = QTransform().translate(center.x(), center.y()).rotate(angle).translate(-center.x(), -center.y()).mapRect(rect)
        return rect

    @staticmethod
    def _same_except_position(base: RenderObject, current: RenderObject) -> bool:
        if base.id != current.id or type(base) is not type(current):
            return False
        moved = replace(current, transform=replace(current.transform, x=base.transform.x, y=base.transform.y))
        return moved == base

    def _draw_object_layers(self, painter: QPainter, canvas: QRectF):
        """选中对象之下、选中对象、之上分三层缓存；拖动时只平移选中层。"""

        objects = tuple(item for item in self._layered_objects() if self._paints(item))
        index = next((i for i, item in enumerate(objects) if item.id == self._selected_object), None)
        layers = []
        if index is None:
            layers.append(self._paint_layer("below", objects, canvas, clip=True))
            self._active_base = None
        else:
            active = objects[index]
            base = self._active_base
            reuse = base is not None and self._same_except_position(base, active)
            if reuse and self._mode is None and base != active and not self._settle_timer.isActive():
                # 手势结束后空闲时按真实位置重画，静止画面与导出逐像素一致。
                reuse = False
            if not reuse:
                base = self._active_base = active
            layers.append(self._paint_layer("below", objects[:index], canvas, clip=True))
            sprite = self._paint_layer("active", (base,), self._object_bounds(base), clip=False)
            if sprite is not None:
                shift = QPointF(
                    (active.transform.x - base.transform.x) * canvas.width(),
                    (active.transform.y - base.transform.y) * canvas.height(),
                )
                layers.append((sprite[0], sprite[1] + shift))
            layers.append(self._paint_layer("above", objects[index + 1:], canvas, clip=True))
        painter.save()
        painter.setClipRect(canvas)
        for layer in layers:
            if layer is not None:
                painter.drawPixmap(layer[1], layer[0])
        painter.restore()

    def paintEvent(self, event):
        if self._background_pixmap.isNull():
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
        painter.restore()
        self._draw_object_layers(painter, canvas)
        selected = self._selected_editable()
        if self._guide_vertical:
            painter.setPen(QPen(QColor(66, 215, 255, 220), 1))
            painter.drawLine(QPointF(canvas.center().x(), canvas.top()), QPointF(canvas.center().x(), canvas.bottom()))
        if self._guide_horizontal:
            painter.setPen(QPen(QColor(66, 215, 255, 220), 1))
            painter.drawLine(QPointF(canvas.left(), canvas.center().y()), QPointF(canvas.right(), canvas.center().y()))
        if selected is not None:
            self._draw_selection(painter, selected)
        elif self._selected_object == "background" or (self._find_background() and self._selected_object == self._find_background().id):
            painter.setPen(QPen(Qt.GlobalColor.cyan, 2, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(canvas.adjusted(1, 1, -1, -1))
        painter.end()
