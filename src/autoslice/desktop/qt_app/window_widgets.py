"""字幕工作台的小部件：状态标签、队列按钮、字幕表格模型、就地编辑器与委托。"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from autoslice.desktop.qt_preview.theme import COLORS, SIZES
from autoslice.desktop.subtitles import SubtitleDocument


class _StatusLabel(QLabel):
    """空闲时从工作栏收起；后台任务或反馈出现时才占用空间。"""

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setVisible(bool(text))


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

    def eventFilter(self, editor, event):
        if isinstance(editor, QPlainTextEdit) and event.type() == QEvent.Type.FocusOut:
            # 不让 QStyledItemDelegate 因短暂 focus out 自动关闭 editor。
            # 是否结束字幕编辑由 DesktopWindow 的“点击编辑器外部”规则统一决定。
            return False
        return super().eventFilter(editor, event)

    def paint(self, painter, option, index):
        option = QStyleOptionViewItem(option)
        if index.column() < 2:
            option.font.setPixelSize(SIZES.text_small)
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
        if index.column() == 0 and option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect.left(), option.rect.top(), 2, option.rect.height(), QColor(COLORS.accent_pressed))
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
