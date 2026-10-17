"""字幕工作台的小部件：状态标签、队列按钮、字幕表格模型、就地编辑器与委托。"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import QBrush, QColor, QTextCursor
from PySide6.QtWidgets import (
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from autoslice.desktop.qt_preview import motion
from autoslice.desktop.qt_preview.theme import COLORS, MONO_FAMILIES, SIZES
from autoslice.desktop.subtitles import SubtitleDocument


class _StatusLabel(QLabel):
    """空闲时从工作栏收起；后台任务或反馈出现时才占用空间。"""

    def setText(self, text: str) -> None:
        if not text and self.text():
            motion.fade_out_snapshot(self)
        appearing = bool(text) and not self.isVisible()
        super().setText(text)
        self.setVisible(bool(text))
        if appearing:
            motion.fade_in(self)


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


class SubtitleTableModel(QAbstractTableModel):
    edited = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.document: SubtitleDocument | None = None
        self.playing_index: int | None = None
        self.pending_ids: set[int] | None = None

    def visible_entries(self):
        entries = self.document.entries if self.document else []
        return [item for item in entries if item.index in self.pending_ids] if self.pending_ids is not None else entries

    def entry_at(self, row):
        return self.visible_entries()[row]

    def set_pending_filter(self, cue_ids):
        self.beginResetModel()
        self.pending_ids = set(cue_ids) if cue_ids is not None else None
        self.endResetModel()

    def set_document(self, document: SubtitleDocument | None):
        self.beginResetModel()
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
        return 0 if parent.isValid() or self.document is None else len(self.visible_entries())

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
        rect = option.rect
        if p > 0.01:
            painter.fillRect(rect, motion.faded(COLORS.playback, p))
        if h * (1 - s) > 0.01:
            painter.fillRect(rect, motion.faded(COLORS.hover, h * (1 - s)))
        if s > 0.01:
            painter.fillRect(rect, motion.faded(COLORS.row_selected, s))
            if index.column() == 0:
                length = rect.height() * motion.ease(s)
                painter.fillRect(QRectF(rect.left(), rect.center().y() - length / 2, 2, length),
                                 motion.faded(COLORS.accent, min(1.0, s * 1.4)))

    @staticmethod
    def _key(index):
        return (index.row(), index.column())

    def createEditor(self, parent, option, index):
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
        option = QStyleOptionViewItem(option)
        painter.save()
        self._paint_row(painter, option, index)
        painter.restore()
        if index.column() < 2:
            option.font.setPixelSize(SIZES.text_small)
            if index.column() == 0:
                option.displayAlignment = Qt.AlignmentFlag.AlignCenter
            else:
                # 等宽数字，时间码逐行竖向对齐
                option.font.setFamilies(list(MONO_FAMILIES))
            option.palette.setColor(option.palette.ColorRole.Text, QColor(COLORS.subtle if index.column() == 0 else COLORS.muted))
            option.palette.setColor(option.palette.ColorRole.HighlightedText, QColor(COLORS.muted))
        if self._editing == self._key(index):
            background = QStyleOptionViewItem(option)
            self.initStyleOption(background, index)
            background.text = ""
            super().paint(painter, background, index)
        else:
            super().paint(painter, option, index)
        painter.save()
        painter.setPen(QColor(COLORS.divider))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
        painter.restore()

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
        model.finish_live_text(index, editor.toPlainText())


def _prepare_inline_editor(editor: QPlainTextEdit, font) -> None:
    """字幕行内编辑框：贴合表格行、不换行、选中行底色。两条编辑入口共用。"""

    editor.setFont(font)
    editor.document().setDocumentMargin(0)
    editor.setTabChangesFocus(True)
    editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    editor.setContentsMargins(0, 0, 0, 0)
    editor.setViewportMargins(0, 0, 0, 0)
    editor.setAutoFillBackground(True)
    editor.viewport().setAutoFillBackground(True)
    editor.setStyleSheet(
        f"QPlainTextEdit {{ background: {COLORS.row_selected}; color: {COLORS.text}; "
        f"border: none; padding: 10px 6px 0 6px; "
        f"selection-background-color: {COLORS.accent_pressed}; }}"
        "QPlainTextEdit:focus { border: none; }"
    )
    editor.viewport().setStyleSheet(
        f"background: {COLORS.row_selected}; border: none;"
    )
