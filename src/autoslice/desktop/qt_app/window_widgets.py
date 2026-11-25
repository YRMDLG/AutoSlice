"""字幕工作台的小部件：状态标签、队列按钮、字幕表格模型、就地编辑器与委托。"""

from __future__ import annotations

import difflib
import time

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QObject,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.qt_preview import motion
from autoslice.desktop.qt_preview.theme import COLORS, MONO_FAMILIES, SIZES, STYLE
from autoslice.desktop.subtitles import SubtitleDocument


class _StatusLabel(QLabel):
    """空闲时从工作栏收起；后台任务或反馈出现时才占用空间。"""

    def setText(self, text: str) -> None:
        fading = getattr(self, "_fading", None)
        if text and fading is not None:
            # 旧文字的淡出残影还没散，新文字就到了：先收掉，免得盖住新文字开头（走查 M13）
            try:
                fading.hide()
                fading.deleteLater()
            except RuntimeError:
                pass
            self._fading = None
        if not text and self.text():
            self._fading = motion.fade_out_snapshot(self)
        appearing = bool(text) and not self.isVisible()
        super().setText(text)
        self.setVisible(bool(text))
        if appearing:
            motion.fade_in(self)
        # 设置页没有顶栏（设计稿）：那里的反馈改用顶部提示条
        window = self.window()
        bar = self.parentWidget()
        if text and bar is not None and bar.isHidden() and hasattr(window, "_toast"):
            window._toast(text)


class _ElidedQueueButton(QPushButton):
    """待处理队列的单行按钮，按实际控件宽度显示右侧省略号。"""

    def __init__(self, text: str, parent=None):
        self._full_text = str(text)
        super().__init__(self._full_text, parent)
        self._update_elided_text()

    @property
    def full_text(self) -> str:
        return self._full_text

    def _update_elided_text(self) -> None:
        margin = self.style().pixelMetric(QStyle.PixelMetric.PM_ButtonMargin, None, self)
        available = max(0, self.contentsRect().width() - margin * 2)
        display = self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, available
        )
        if self.text() != display:
            super().setText(display)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided_text()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
            QEvent.Type.EnabledChange,
        ):
            self._update_elided_text()


class _AiStat(QWidget):
    """AI 检查卡里的一个数字：20px 等宽数字 + 小字说明；数字变化时从下往上翻入。"""

    clicked = Signal()

    def __init__(self, caption: str, *, accent: bool = False, muted: bool = False, parent=None):
        super().__init__(parent)
        from autoslice.desktop.qt_preview.shell import RollingLabel

        self.accent = accent
        self.muted = muted
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        role = "accent_ink" if accent else "muted" if muted else "text"
        self.value = RollingLabel("0", pixels=20, color_role=role, mono=True, weight=500)
        self.caption = QLabel(caption)
        self.caption.setObjectName("hintLine")
        column.addWidget(self.value)
        column.addWidget(self.caption)
        self._current = None

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def set_value(self, number: int) -> None:
        if number == self._current:
            return
        self._current = number
        self.value.setText(str(number))


class _InlineAiWatcher(QObject):
    """字幕表格视口尺寸变化时，重新摆放行内建议。"""

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self, self._window._place_inline_ai)
        return False


# 字幕行几何（设计稿）：上内边距 10、正文行 22、时间行 15、左右内边距 12、行间距 2
ROW_PAD_TOP = 10
TEXT_LINE = 22
TIME_TOP = 33
TIME_LINE = 15
ROW_SIDE = 12
ROW_GAP = 2
EDIT_PAD_X = 10


class RowEffects(QObject):
    """字幕行的一次性动效（设计稿）：

    - 筛选切换：逐行进场（夜航 200ms 上移 4px，晨雾 460ms 翻入，行间隔 18ms）
    - 改字保存：整行闪一下（夜航强调色 20% 底 1000ms，晨雾淡绿底 + 左侧绿条 1300ms）
    - 拆分 / 合并出来的新行：从上方弹入（夜航 520ms，晨雾 620ms）
    - 采纳建议：一道绿光扫过整行（760ms），换掉的字向上飘走、新字从下方弹上来并发光

    只影响绘制，状态早已生效；减少动态效果时全部跳过。
    """

    def __init__(self, table) -> None:
        super().__init__(table)
        self._table = table
        self._enter_start: float | None = None
        self._effects: dict[int, dict] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    # 触发
    def play_enter(self) -> None:
        if motion.enabled():
            self._enter_start = time.monotonic()
            self._start()

    def play(self, cue_id: int, kind: str, **data) -> None:
        if motion.enabled() and cue_id is not None:
            self._effects[cue_id] = {"kind": kind, "start": time.monotonic(), **data}
            self._start()

    def _start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
        self._table.viewport().update()

    def _tick(self) -> None:
        now = time.monotonic()
        rows = self._table.model().rowCount() if self._table.model() is not None else 0
        if self._enter_start is not None and now - self._enter_start > (motion.profile().row_enter
                                                                         + 18 * min(rows, 30)) / 1000 + .05:
            self._enter_start = None
        for key in [key for key, value in self._effects.items() if now - value["start"] > self._life(value)]:
            del self._effects[key]
        if self._enter_start is None and not self._effects:
            self._timer.stop()
        self._table.viewport().update()

    @staticmethod
    def _life(effect) -> float:
        return {"flash": 1.3, "born": .7, "accept": 1.8}.get(effect["kind"], 1.0)

    # 查询（绘制时用）
    @staticmethod
    def _progress(start: float, delay: float, duration: float, curve) -> float:
        t = (time.monotonic() - start - delay) / max(1e-3, duration)
        if t <= 0:
            return 0.0
        if t >= 1:
            return 1.0
        return curve.valueForProgress(t)

    def transform(self, row: int, cue_id: int) -> tuple[float, float]:
        """这一行此刻的 (纵向位移, 透明度)。"""

        dy, opacity = 0.0, 1.0
        p = motion.profile()
        if self._enter_start is not None:
            visible_row = row - (self._table.rowAt(0) if self._table.rowAt(0) >= 0 else 0)
            v = self._progress(self._enter_start, max(0, visible_row) * .018, p.row_enter / 1000, motion.OUT)
            dy += (-p.row_shift if not STYLE.dark else p.row_shift) * (1 - v)
            opacity *= v
        effect = self._effects.get(cue_id)
        if effect and effect["kind"] == "born":
            v = self._progress(effect["start"], 0, .52 if STYLE.dark else .62, motion.spring())
            dy += (-8 if STYLE.dark else -10) * (1 - v)
            opacity *= min(1.0, max(0.0, v))
        return dy, opacity

    def effect(self, cue_id: int) -> dict | None:
        return self._effects.get(cue_id)

    def paint_overlay(self, painter, rect: QRectF, cue_id: int, shape) -> None:
        """整行叠加层：改字闪光、采纳扫光。"""

        effect = self._effects.get(cue_id)
        if not effect:
            return
        if effect["kind"] == "flash":
            if STYLE.dark:
                v = self._progress(effect["start"], 0, 1.0, motion.OUT)
                painter.fillPath(shape, motion.faded(COLORS.accent, .20 * (1 - v)))
            else:
                v = self._progress(effect["start"], 0, 1.3, motion.OUT)
                painter.fillPath(shape, motion.faded(COLORS.ai_added_bg, 1 - v))
                painter.fillRect(QRectF(rect.left(), rect.top() + 1, 3, rect.height() - 2),
                                 motion.faded("#8CC9A6", 1 - v))
        elif effect["kind"] == "accept":
            v = self._progress(effect["start"], 0, .76, motion.OUT)
            if v >= 1:
                return
            band = rect.width() * .45
            x = rect.left() - band + (rect.width() * 2.4 * .45 + band) * v
            gradient = QLinearGradient(x, 0, x + band, 0)
            glow = QColor(COLORS.success)
            glow.setAlphaF(0.0)
            gradient.setColorAt(0.0, glow)
            glow.setAlphaF(.13 * (1 - v) * (1 if STYLE.dark else 2))
            gradient.setColorAt(0.5, glow)
            glow.setAlphaF(0.0)
            gradient.setColorAt(1.0, glow)
            painter.save()
            painter.setClipPath(shape)
            painter.fillRect(rect, QBrush(gradient))
            painter.restore()

    def swap_state(self, cue_id: int):
        """采纳换字的进度：(旧字飘走进度, 新字弹入进度, 发光褪去进度)；没有就是 None。"""

        effect = self._effects.get(cue_id)
        if not effect or effect["kind"] != "accept":
            return None
        out = self._progress(effect["start"], 0, .36, motion.OUT)
        rise = self._progress(effect["start"], .08, .44, motion.spring())
        glow = self._progress(effect["start"], .14, 1.6, motion.STANDARD)
        return out, rise, glow, effect


class SelectionFrame(QWidget):
    """字幕列表里选中行的圆角选中框：只画一圈强调色细线，不铺大片强调色。

    盖在表格视口上、对鼠标透明；选中、滚动、行高变化时重画。
    """

    INSET = 0.0

    def __init__(self, table) -> None:
        super().__init__(table.viewport())
        self.table = table
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._fade = motion.Channel(self, self.update, 1.0)
        self._sweep = motion.Channel(self, self.update, 1.0)
        self.ripple_at: QPointF | None = None
        self._rows: tuple = ()
        table.viewport().installEventFilter(self)
        table.verticalScrollBar().valueChanged.connect(lambda _value: self.update())
        table.verticalHeader().sectionResized.connect(lambda *_args: self.update())
        self._watch_selection()
        self.setGeometry(table.viewport().rect())
        self.show()

    def _watch_selection(self) -> None:
        model = self.table.selectionModel()
        if model is not None:
            model.selectionChanged.connect(self._selection_changed)
        self.table.model().modelReset.connect(self.update)
        self.table.model().layoutChanged.connect(self.update)

    def _selection_changed(self, *_args) -> None:
        rows = tuple(sorted({index.row() for index in self.table.selectionModel().selectedIndexes()}))
        if rows != self._rows:
            self._rows = rows
            self._fade.snap(0.0)
            self._fade.to(1.0, motion.SELECT)
            self._sweep.snap(0.0)
            self._sweep.to(1.0, 640 if STYLE.dark else 720, motion.OUT)
        self.update()

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Resize:
            self.setGeometry(self.table.viewport().rect())
            self.raise_()
        elif event.type() == QEvent.Type.MouseButtonPress:
            self.ripple_at = QPointF(event.position())
        return False

    def paintEvent(self, _event) -> None:
        selection = self.table.selectionModel()
        if selection is None:
            return
        rows = sorted({index.row() for index in selection.selectedIndexes()})
        if not rows:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(COLORS.accent_line)
        color.setAlphaF(max(0.0, min(1.0, self._fade.value)))
        sweep = self._sweep.value
        painter.setBrush(Qt.BrushStyle.NoBrush)
        model = self.table.model()
        last = model.columnCount() - 1
        for row in rows:
            first = self.table.visualRect(model.index(row, 0))
            end = self.table.visualRect(model.index(row, last))
            if not first.isValid() or first.bottom() < 0 or first.top() > self.height():
                continue
            rect = QRectF(first.left(), first.top(), end.right() - first.left() + 1, first.height())
            rect = rect.adjusted(self.INSET + 0.5, ROW_GAP / 2 + 0.5, -self.INSET - 0.5, -ROW_GAP / 2 - 0.5)
            radius = STYLE.row_radius - 0.5
            if not STYLE.dark and sweep < 1 and self.ripple_at is not None and rect.contains(self.ripple_at):
                # 晨雾：从点下的位置漾开一圈淡强调色
                path = QPainterPath()
                path.addRoundedRect(rect, radius, radius)
                painter.save()
                painter.setClipPath(path)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(motion.faded(COLORS.accent_line, .35 * (1 - sweep)))
                reach = rect.width() * 1.1 * sweep
                painter.drawEllipse(self.ripple_at, reach, reach)
                painter.restore()
                painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(color, 1))
            painter.drawRoundedRect(rect, radius, radius)
            if STYLE.dark and sweep < 1:
                # 夜航：一段亮强调色沿选中框转一圈后淡出（设计稿 conic trace）
                gradient = QConicalGradient(rect.center(), 90 - 360 * sweep)
                clear = QColor(COLORS.accent)
                clear.setAlphaF(0.0)
                bright = QColor(COLORS.accent)
                bright.setAlphaF(1.0 if sweep < .75 else max(0.0, (1 - sweep) / .25))
                gradient.setColorAt(0.0, clear)
                gradient.setColorAt(1 - 330 / 360, bright)
                gradient.setColorAt(1 - 240 / 360, clear)
                gradient.setColorAt(1.0, clear)
                painter.setPen(QPen(QBrush(gradient), 1.5))
                painter.drawRoundedRect(rect, radius, radius)


class SubtitleTableModel(QAbstractTableModel):
    edited = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.document: SubtitleDocument | None = None
        self.playing_index: int | None = None
        self.pending_ids: set[int] | None = None
        self.tags: dict[int, str] = {}
        # 查找高亮：(查找词, 区分大小写, 当前命中 (字幕编号, 起点))；None 表示不在查找
        self.find_state: tuple | None = None
        # 行内建议展开时，原文里要删掉的字：{字幕编号: ((起, 止), ...)}
        self.removed_marks: dict[int, tuple] = {}
        self._filter_cache = None  # (键, 位置列表, 对应字幕编号)

    def _filtered_rows(self):
        """“待确认”筛选下各行在 entries 里的位置。

        每次取格子都重筛整份字幕时，3000 条画一屏要约 100 毫秒；这里按列表身份、长度和筛选集合缓存。
        条目内容始终现读，改字、改时间不受缓存影响；拆分、合并、删除、撤销会改变长度或换列表而重算。
        """

        entries = self.document.entries
        key = (id(entries), len(entries), id(self.pending_ids))
        if self._filter_cache is None or self._filter_cache[0] != key:
            positions = [i for i, item in enumerate(entries) if item.index in self.pending_ids]
            self._filter_cache = (key, positions, [entries[i].index for i in positions])
        return self._filter_cache

    def visible_entries(self):
        entries = self.document.entries if self.document else []
        if self.pending_ids is None or not entries:
            return entries if self.pending_ids is None else []
        _key, positions, _ids = self._filtered_rows()
        return [entries[i] for i in positions]

    def entry_at(self, row):
        if self.pending_ids is None or self.document is None:
            return self.visible_entries()[row]
        _key, positions, ids = self._filtered_rows()
        entry = self.document.entries[positions[row]]
        if entry.index != ids[row]:
            # 同长度整体替换等少见情况：编号对不上就重算一次
            self._filter_cache = None
            _key, positions, ids = self._filtered_rows()
            entry = self.document.entries[positions[row]]
        return entry

    def set_tags(self, tags: dict[int, str]) -> None:
        """字幕行右侧的小标记（AI 建议状态）；只影响显示。"""

        if tags == self.tags:
            return
        self.tags = dict(tags)
        if self.document is not None and self.rowCount():
            self.dataChanged.emit(self.index(0, 2), self.index(self.rowCount() - 1, 2),
                                  [Qt.ItemDataRole.DisplayRole])

    def set_find(self, query: str | None, sensitive: bool = False, current=None) -> None:
        state = (query, sensitive, current) if query else None
        if state == self.find_state:
            return
        self.find_state = state
        if self.document is not None and self.rowCount():
            self.dataChanged.emit(self.index(0, 2), self.index(self.rowCount() - 1, 2),
                                  [Qt.ItemDataRole.DisplayRole])

    def set_removed(self, marks: dict[int, tuple]) -> None:
        if marks == self.removed_marks:
            return
        self.removed_marks = dict(marks)
        if self.document is not None and self.rowCount():
            self.dataChanged.emit(self.index(0, 2), self.index(self.rowCount() - 1, 2),
                                  [Qt.ItemDataRole.DisplayRole])

    def tag_for(self, cue_index: int) -> str:
        tag = self.tags.get(cue_index, "")
        if not tag and cue_index == self.playing_index:
            return "播放中"
        return tag

    def set_pending_filter(self, cue_ids):
        self.beginResetModel()
        self._filter_cache = None
        self.pending_ids = set(cue_ids) if cue_ids is not None else None
        self.endResetModel()

    def set_document(self, document: SubtitleDocument | None):
        self.beginResetModel()
        self._filter_cache = None
        self.document = document
        self.playing_index = None
        self.endResetModel()

    def set_playing_index(self, cue_index: int | None):
        if cue_index == self.playing_index or self.document is None:
            return
        old = self.playing_index
        self.playing_index = cue_index
        for item in (old, cue_index):
            row = next((i for i, entry in enumerate(self.visible_entries())
                        if entry.index == item), None)
            if row is not None:
                self.dataChanged.emit(self.index(row, 0), self.index(row, 2),
                                      [Qt.ItemDataRole.BackgroundRole])

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid() or self.document is None:
            return 0
        if self.pending_ids is None:
            return len(self.document.entries)
        return len(self._filtered_rows()[1]) if self.document.entries else 0

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 3

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return ("", "时间", "字幕正文")[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self.document is None:
            return None
        entry = self.entry_at(index.row())
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return (str(entry.index), f"{entry.start}  →  {entry.end}", entry.text)[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole and index.column() == 2:
            return "单击直接编辑；Ctrl+Z / Ctrl+Y 撤销或重做"
        if role == Qt.ItemDataRole.BackgroundRole and entry.index == self.playing_index:
            return QColor(COLORS.playback)
        return None

    def flags(self, index):
        flags = super().flags(index)
        return flags | Qt.ItemFlag.ItemIsEditable if index.column() == 2 else flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or index.column() != 2 or self.document is None:
            return False
        if self.entry_at(index.row()).text == str(value):
            return True
        self.document.edit_text(self.entry_at(index.row()).index, str(value))
        self.dataChanged.emit(index, index, [
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.EditRole,
        ])
        self.edited.emit()
        return True

    def set_live_text(self, index, value):
        """编辑器输入期间只更新文档，不反向刷新 editor，避免 caret 被重置。"""
        if not index.isValid() or index.column() != 2 or self.document is None:
            return False
        value = str(value)
        entry = self.entry_at(index.row())
        if entry.text == value:
            return True
        self.document.edit_text(entry.index, value)
        self.edited.emit()
        return True

    def finish_live_text(self, index, value):
        """结束编辑时再让 cell 重绘一次；活动 editor 不参与这次回写。"""
        if not index.isValid() or index.column() != 2 or self.document is None:
            return False
        value = str(value)
        entry = self.entry_at(index.row())
        if entry.text != value:
            self.document.edit_text(entry.index, value)
            self.edited.emit()
        self.dataChanged.emit(index, index, [
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.EditRole,
        ])
        return True


class InlineSubtitleEditor(QPlainTextEdit):
    """字幕正文专用 editor：明确支持鼠标拖选局部文字。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selection_anchor = None
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self._selection_anchor = self.textCursor().position()

    def mouseMoveEvent(self, event):
        if (self._selection_anchor is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            position = self.cursorForPosition(event.position().toPoint()).position()
            cursor = self.textCursor()
            cursor.setPosition(self._selection_anchor)
            cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(cursor)
            self.ensureCursorVisible()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self._selection_anchor = None


class SubtitleTextDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._table = parent
        self._editing = None
        self._hover_row = -1
        self._rows = motion.KeyedAnimator(self, lambda: self._table.viewport().update())
        self.effects = RowEffects(parent)
        self._edit_before: dict = {}
        # 记下视口对象；过滤器里只比身份，避免销毁阶段访问已删除的表格
        self._viewport = parent.viewport()
        self._viewport.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self._viewport:
            # 视口：只跟踪悬停行供行动画使用，不拦截事件
            kind = event.type()
            if kind in (QEvent.Type.MouseMove, QEvent.Type.Leave):
                row = self._table.rowAt(int(event.position().y())) if kind == QEvent.Type.MouseMove else -1
                if row != self._hover_row:
                    self._hover_row = row
                    self._table.viewport().update()
            return False
        if isinstance(obj, QPlainTextEdit) and event.type() == QEvent.Type.FocusOut:
            # 不让 QStyledItemDelegate 因短暂 focus out 自动关闭 editor。
            # 是否结束字幕编辑由 DesktopWindow 的“点击编辑器外部”规则统一决定。
            return False
        return super().eventFilter(obj, event)

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        # 行背景统一在 paint 中绘制并过渡
        option.backgroundBrush = QBrush()

    def _paint_row(self, painter, option, index):
        model = index.model()
        try:
            entry = model.entry_at(index.row())
        except (AttributeError, IndexError):
            return
        hovered = index.row() == self._hover_row
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        playing = entry.index == getattr(model, "playing_index", None)
        key = entry.index
        h = self._rows.value((key, "hover"), 1.0 if hovered else 0.0,
                             motion.HOVER_IN if hovered else motion.HOVER_OUT)
        s = self._rows.value((key, "select"), 1.0 if selected else 0.0, motion.SELECT)
        p = self._rows.value((key, "play"), 1.0 if playing else 0.0, motion.SELECT)
        shape = _row_shape(QRectF(option.rect), index.column(), model.columnCount() - 1)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if p > 0.01:
            painter.fillPath(shape, motion.faded(COLORS.playback, p * (1 - s)))
        if h * (1 - s) * (1 - p) > 0.01:
            painter.fillPath(shape, motion.faded(COLORS.hover, h * (1 - s) * (1 - p)))
        if s > 0.01:
            # 选中行只保留选中框（SelectionFrame 画圆角细线），底色几乎同卡片，不铺强调色
            painter.fillPath(shape, motion.faded(COLORS.row_selected, s))

    @staticmethod
    def _key(index):
        return (index.row(), index.column())

    @staticmethod
    def _runs(model, entry, text: str) -> list[tuple[int, int, str]]:
        """正文里要特别标出的字：(起, 止, 种类)。种类：removed 要删的字、hit 查找命中、cur 当前命中。"""

        runs = [(a, b, "removed") for a, b in getattr(model, "removed_marks", {}).get(entry.index, ())]
        state = getattr(model, "find_state", None)
        if state:
            query, sensitive, current = state
            haystack, needle = (text, query) if sensitive else (text.casefold(), query.casefold())
            start = haystack.find(needle) if needle else -1
            while start >= 0:
                runs.append((start, start + len(needle), "cur" if current == (entry.index, start) else "hit"))
                start = haystack.find(needle, start + len(needle))
        return sorted(runs)

    def _font(self, pixels: int, mono: bool = False) -> QFont:
        font = QFont(self._table.font() if self._table is not None else QFont())
        font.setPixelSize(pixels)
        if mono:
            font.setFamilies(list(MONO_FAMILIES))
        return font

    def _paint_text(self, painter, rect: QRectF, entry, model) -> None:
        """正文一行：查找命中、要删的字分段着色；放不下时末尾省略。"""

        text = entry.text.replace("\n", " ")
        font = self._font(SIZES.text_body)
        metrics = QFontMetrics(font)
        swap = self.effects.swap_state(entry.index)
        if swap is not None and swap[3].get("after") == entry.text:
            self._paint_swap(painter, rect, text, font, metrics, swap)
            return
        runs = self._runs(model, entry, text)
        painter.save()
        painter.setFont(font)
        painter.setClipRect(rect)
        if not runs:
            painter.setPen(QColor(COLORS.text))
            painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(rect.width())))
            painter.restore()
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        height = metrics.height() + 2
        top = rect.center().y() - height / 2
        colors = {}
        for a, b, kind in runs:
            x = rect.left() + metrics.horizontalAdvance(text[:a])
            width = metrics.horizontalAdvance(text[a:b])
            box = QRectF(x - 1, top, width + 2, height)
            path = QPainterPath()
            path.addRoundedRect(box, 3, 3)
            if kind == "cur":
                painter.fillPath(path, QColor(COLORS.accent))
                fg = COLORS.on_accent
            elif kind == "hit":
                painter.fillPath(path, motion.faded(COLORS.accent, 0.2))
                painter.fillRect(QRectF(box.left(), box.bottom() - 1.5, box.width(), 1.5),
                                 motion.faded(COLORS.accent, 0.7))
                fg = COLORS.text
            else:
                painter.fillPath(path, QColor(COLORS.ai_removed_bg))
                fg = COLORS.ai_removed
            for i in range(a, b):
                colors[i] = fg
        # 按颜色分段画字：同色连续的字一次画完
        cursor = 0
        while cursor < len(text):
            fg = colors.get(cursor, COLORS.text)
            stop = cursor + 1
            while stop < len(text) and colors.get(stop, COLORS.text) == fg:
                stop += 1
            x = rect.left() + metrics.horizontalAdvance(text[:cursor])
            painter.setPen(QColor(fg))
            painter.drawText(QRectF(x, rect.top(), rect.right() - x + 400, rect.height()),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text[cursor:stop])
            cursor = stop
        painter.restore()

    def _paint_swap(self, painter, rect: QRectF, text: str, font, metrics, swap) -> None:
        out, rise, glow, effect = swap
        before = str(effect.get("before", "")).replace("\n", " ")
        painter.save()
        painter.setFont(font)
        painter.setClipRect(rect.adjusted(0, -6, 0, 6))
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        height = rect.height()
        for kind, a, b, c, d in difflib.SequenceMatcher(None, before, text).get_opcodes():
            x = rect.left() + metrics.horizontalAdvance(text[:c])
            new = text[c:d]
            if kind == "equal":
                painter.setOpacity(1.0)
                painter.setPen(QColor(COLORS.text))
                painter.drawText(QRectF(x, rect.top(), 600, height), align, new)
                continue
            old = before[a:b]
            if old and out < 1:
                painter.setOpacity(1 - out)
                painter.setPen(QColor(COLORS.ai_removed))
                painter.drawText(QRectF(x, rect.top() - height * .75 * out, 400, height), align, old)
            if new:
                painter.setOpacity(max(0.0, min(1.0, rise)))
                painter.setPen(motion.mix("#C2F5D8" if STYLE.dark else COLORS.ai_added, COLORS.text, glow))
                painter.drawText(QRectF(x, rect.top() + height * .75 * (1 - rise), 400, height), align, new)
        painter.restore()

    @staticmethod
    def _tag_color(tag: str, selected: bool) -> str:
        if tag == "已采纳":
            return COLORS.success
        if tag == "待确认":
            if not STYLE.dark:
                return COLORS.accent_ink
            return COLORS.muted if selected else COLORS.text_secondary
        return COLORS.muted

    def _paint_tag(self, painter, rect: QRectF, entry, model, selected: bool) -> float:
        """正文列右侧画状态标记（与正文首行齐平），返回占用的宽度。"""

        tag = model.tag_for(entry.index) if hasattr(model, "tag_for") else ""
        if not tag:
            return 0.0
        font = self._font(SIZES.text_small)
        width = QFontMetrics(font).horizontalAdvance(tag)
        painter.save()
        painter.setFont(font)
        painter.setPen(QColor(self._tag_color(tag, selected)))
        painter.drawText(QRectF(rect.right() - width, rect.top(), width, rect.height()),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, tag)
        painter.restore()
        return width + 12

    def createEditor(self, parent, option, index):
        try:
            self._edit_before[self._key(index)] = index.model().entry_at(index.row()).text
        except (AttributeError, IndexError):
            pass
        editor = QPlainTextEdit(parent)
        _prepare_inline_editor(editor, option.font)
        editor.setProperty("autoslice_editor_initialised", False)
        editor.installEventFilter(parent.window())
        self._editing = self._key(index)
        try:
            self._table.viewport().update(self._table.visualRect(index))
        except RuntimeError:
            pass
        editor.destroyed.connect(lambda: self._clear_editing(index))
        editor.textChanged.connect(
            lambda: index.model().set_live_text(index, editor.toPlainText())
        )
        return editor

    def _clear_editing(self, index):
        if self._editing == self._key(index):
            self._editing = None
        try:
            self._table.viewport().update(self._table.visualRect(index))
        except RuntimeError:
            pass

    def paint(self, painter, option, index):
        """一行两层：上面序号 + 正文 + 标记，下面等宽时间码；行内建议展开时只在原行高里画字。"""

        option = QStyleOptionViewItem(option)
        model = index.model()
        try:
            entry = model.entry_at(index.row())
        except (AttributeError, IndexError):
            return
        dy, opacity = self.effects.transform(index.row(), entry.index)
        if opacity < .999 or abs(dy) > .01:
            painter.save()
            painter.setOpacity(painter.opacity() * max(0.0, opacity))
            painter.translate(0, dy)
            self._paint_cell(painter, option, index, model, entry)
            painter.restore()
        else:
            self._paint_cell(painter, option, index, model, entry)

    def _paint_cell(self, painter, option, index, model, entry):
        painter.save()
        self._paint_row(painter, option, index)
        shape = _row_shape(QRectF(option.rect), index.column(), model.columnCount() - 1)
        self.effects.paint_overlay(painter, QRectF(option.rect), entry.index, shape)
        painter.restore()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        cell = QRectF(option.rect)
        line = QRectF(cell.left(), cell.top() + ROW_PAD_TOP, cell.width(), TEXT_LINE)
        if index.column() == 0:
            painter.save()
            painter.setFont(self._font(SIZES.text_small, mono=True))
            painter.setPen(QColor(COLORS.text if selected else COLORS.subtle))
            painter.drawText(QRectF(cell.left() + ROW_SIDE, line.top(), 24, line.height()),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, str(entry.index))
            painter.restore()
            return
        if index.column() != 2 or self._editing == self._key(index):
            return
        text_rect = QRectF(line.left(), line.top(), line.width() - ROW_SIDE, line.height())
        taken = self._paint_tag(painter, text_rect, entry, model, selected)
        text_rect.setRight(text_rect.right() - taken)
        self._paint_text(painter, text_rect, entry, model)
        painter.save()
        painter.setFont(self._font(SIZES.text_small, mono=True))
        painter.setPen(QColor(COLORS.subtle))
        painter.drawText(QRectF(cell.left(), cell.top() + TIME_TOP, cell.width() - ROW_SIDE, TIME_LINE),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         short_span(entry.start, entry.end))
        painter.restore()

    def updateEditorGeometry(self, editor, option, index):
        """编辑框盖住正文和时间两行，左边让出内边距，文字起点和原来的正文对齐。"""

        rect = option.rect
        editor.setGeometry(rect.left() - EDIT_PAD_X, rect.top() + 4,
                           rect.width() - ROW_SIDE + EDIT_PAD_X, SIZES.subtitle_row_height - 8)

    def setEditorData(self, editor, index):
        if editor.property("autoslice_editor_initialised"):
            return
        editor.blockSignals(True)
        try:
            editor.setPlainText(str(index.data(Qt.ItemDataRole.EditRole) or ""))
            editor.setProperty("autoslice_editor_initialised", True)
        finally:
            editor.blockSignals(False)

    def setModelData(self, editor, model, index):
        before = self._edit_before.pop(self._key(index), None)
        model.finish_live_text(index, editor.toPlainText())
        try:
            entry = model.entry_at(index.row())
        except (AttributeError, IndexError):
            return
        if before is not None and before != entry.text:
            # 改字落定：整行闪一下（设计稿 edFlash / 晨雾 inkWash）
            self.effects.play(entry.index, "flash")


def _row_shape(rect: QRectF, column: int, last: int) -> QPainterPath:
    """一行由几个单元格拼成：只在最左、最右两端收圆角并内缩，中间格子直角相接。"""

    inset = SelectionFrame.INSET
    radius = STYLE.row_radius
    rect = rect.adjusted(inset if column == 0 else 0, ROW_GAP / 2, -inset if column == last else 0, -ROW_GAP / 2)
    path = QPainterPath()
    if column not in (0, last):
        path.addRect(rect)
        return path
    path.addRoundedRect(rect, radius, radius)
    if column == 0 and last != 0:
        square = QPainterPath()
        square.addRect(QRectF(rect.center().x(), rect.top(), rect.width() / 2 + 1, rect.height()))
        path = path.united(square)
    elif column == last and last != 0:
        square = QPainterPath()
        square.addRect(QRectF(rect.left() - 1, rect.top(), rect.width() / 2 + 1, rect.height()))
        path = path.united(square)
    return path


def _prepare_inline_editor(editor: QPlainTextEdit, font) -> None:
    """字幕行内编辑框：强调色细框、输入底色，单行不换行；两条编辑入口共用。"""

    # 全局样式表的 12px 会盖过 setFont，编辑态字号要和正文一致，并垂直居中不跳位
    body = QFont(font)
    body.setPixelSize(SIZES.text_body)
    top = max(0, (SIZES.subtitle_row_height - 8 - 2 - QFontMetrics(body).height()) // 2)
    border = COLORS.accent if STYLE.dark else COLORS.accent_line
    editor.setFont(body)
    editor.document().setDocumentMargin(0)
    editor.setTabChangesFocus(True)
    editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    editor.setContentsMargins(0, 0, 0, 0)
    editor.setViewportMargins(0, 0, 0, 0)
    editor.setAutoFillBackground(False)
    editor.viewport().setAutoFillBackground(False)
    editor.setStyleSheet(
        f"QPlainTextEdit {{ background: {COLORS.well}; color: {COLORS.text}; "
        f"border: 1px solid {border}; border-radius: 8px; "
        f"padding: {top}px {EDIT_PAD_X - 1}px 0 {EDIT_PAD_X - 1}px; font-size: {SIZES.text_body}px; "
        f"selection-background-color: {COLORS.accent_pressed if STYLE.dark else COLORS.accent_soft}; }}"
        f"QPlainTextEdit:focus {{ border: 1px solid {border}; }}"
    )
    editor.viewport().setStyleSheet("background: transparent; border: none;")


def short_span(start: str, end: str) -> str:
    """字幕行里的时间：起点写全，终点和起点同一时分时只写秒，“01:12:03.420 → 05.180”。"""

    first, last = start.replace(",", "."), end.replace(",", ".")
    if first[:6] == last[:6] and len(last) > 6:
        last = last[6:]
    return f"{first} → {last}"
