"""字幕查找替换：字幕卡里的查找条（区分大小写、只在待确认里），命中后跳到那句并居中，替换可撤销。

混入 DesktopWindow，排在 PreviewWindow 之前；状态都在窗口上，不单独实例化。
只改界面里的字幕文档（进撤销、草稿），保存校对字幕时才写文件。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.qt_preview import motion
from autoslice.desktop.qt_preview.icons import icon as desktop_icon
from autoslice.desktop.qt_preview.theme import COLORS
from autoslice.desktop.qt_preview.window import label, tool_icon
from autoslice.transcription.contracts import srt_timestamp_seconds


class _FindKeys(QObject):
    """查找框里的按键：Enter 下一处、Shift+Enter 上一处、Esc 关闭。"""

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window

    def eventFilter(self, obj, event) -> bool:
        if event.type() != QEvent.Type.KeyPress:
            return False
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if obj is self._window.replace_input:
                self._window._replace_one()
            else:
                self._window._find_step(-1 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1)
            return True
        if key == Qt.Key.Key_Escape:
            self._window._close_find()
            return True
        return False


class _InlineCount(QObject):
    """查找框里的命中计数：放在输入框内右侧（设计稿），输入框给它让出右边距。"""

    WIDTH = 64

    def __init__(self, edit: QLineEdit, count) -> None:
        super().__init__(edit)
        self.edit, self.count = edit, count
        count.setParent(edit)
        count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        count.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        edit.setTextMargins(0, 0, self.WIDTH, 0)
        edit.installEventFilter(self)
        self._place()

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Resize:
            self._place()
        return False

    def _place(self) -> None:
        self.count.setGeometry(self.edit.width() - self.WIDTH - 10, 0, self.WIDTH, self.edit.height())


class FindReplaceMixin:
    def _build_find_toggle(self):
        button = QPushButton()
        button.setObjectName("tool")
        button.setProperty("square", True)
        button.setCheckable(True)
        button.setFixedSize(32, 32)
        button.setToolTip("查找和替换（Ctrl+F）")
        button.setAccessibleName("查找和替换")
        tool_icon(button, "search", checkable=True)
        button.toggled.connect(lambda on: self._open_find() if on else self._close_find())
        self.find_toggle = button
        return button

    def _build_find_bar(self):
        bar = QWidget()
        bar.setObjectName("findBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        column = QVBoxLayout(bar)
        column.setContentsMargins(10, 10, 10, 10)
        column.setSpacing(8)
        keys = _FindKeys(self)
        find_row = QHBoxLayout()
        find_row.setSpacing(6)
        # 设计稿：框内左边放大镜、右边等宽小字计数（无结果时红字），不带清除按钮
        self.find_input = QLineEdit()
        self.find_input.setPlaceholderText("查找字幕里的字")
        self.find_input.addAction(desktop_icon("search", COLORS.subtle, 14), QLineEdit.ActionPosition.LeadingPosition)
        self.find_input.textChanged.connect(lambda _text: self._refind())
        self.find_input.installEventFilter(keys)
        find_row.addWidget(self.find_input, 1)
        self.find_count = label("", "mono")
        self._find_count_place = _InlineCount(self.find_input, self.find_count)
        self.find_prev = QPushButton()
        self.find_next = QPushButton()
        for button, name, tip, step in ((self.find_prev, "chevron_up", "上一处（Shift+Enter）", -1),
                                        (self.find_next, "chevron_down", "下一处（Enter）", 1)):
            button.setObjectName("square")
            button.setFixedSize(32, 32)
            button.setToolTip(tip)
            button.setAccessibleName(tip)
            tool_icon(button, name)
            button.clicked.connect(lambda _checked=False, delta=step: self._find_step(delta))
            find_row.addWidget(button)
        close = QPushButton()
        close.setObjectName("tool")
        close.setProperty("square", True)
        close.setFixedSize(32, 32)
        close.setToolTip("关闭查找（Esc）")
        tool_icon(close, "close")
        close.clicked.connect(self._close_find)
        find_row.addWidget(close)
        column.addLayout(find_row)
        replace_row = QHBoxLayout()
        replace_row.setSpacing(6)
        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("替换为")
        self.replace_input.addAction(desktop_icon("replace", COLORS.subtle, 14), QLineEdit.ActionPosition.LeadingPosition)
        self.replace_input.installEventFilter(keys)
        replace_row.addWidget(self.replace_input, 1)
        self.replace_one_button = QPushButton("替换")
        self.replace_one_button.clicked.connect(self._replace_one)
        replace_row.addWidget(self.replace_one_button)
        self.replace_all_button = QPushButton("全部替换")
        self.replace_all_button.setObjectName("primary")
        self.replace_all_button.clicked.connect(self._replace_all)
        replace_row.addWidget(self.replace_all_button)
        column.addLayout(replace_row)
        option_row = QHBoxLayout()
        option_row.setSpacing(6)
        self.find_case = QPushButton("区分大小写")
        self.find_pending_only = QPushButton("只在待确认里")
        for button in (self.find_case, self.find_pending_only):
            button.setObjectName("tool")
            button.setCheckable(True)
            button.setFixedHeight(24)
            button.toggled.connect(lambda _on: self._refind())
            option_row.addWidget(button)
        option_row.addStretch()
        hint = label("Enter 下一处 · 只改界面，保存才写入文件", "subtle")
        option_row.addWidget(hint)
        column.addLayout(option_row)
        bar.hide()
        self.find_bar = bar
        self._find_hits: list[tuple[int, int]] = []
        self._find_current = 0
        return bar

    # ── 打开 / 关闭 ──
    def _open_find(self):
        if not hasattr(self, "find_bar"):
            return
        if self.pages.currentIndex() != 0:
            self._select_page(0)
        self._commit_editor()
        if not self.find_toggle.isChecked():
            self.find_toggle.blockSignals(True)
            self.find_toggle.setChecked(True)
            self.find_toggle.blockSignals(False)
        motion.reveal(self.find_bar, True)
        selected = self._selected_plain_text()
        if selected and not self.find_input.text():
            self.find_input.setText(selected)
        self.find_input.setFocus()
        self.find_input.selectAll()
        self._refind()

    def _close_find(self):
        if not hasattr(self, "find_bar"):
            return
        motion.reveal(self.find_bar, False)
        self.find_toggle.blockSignals(True)
        self.find_toggle.setChecked(False)
        self.find_toggle.blockSignals(False)
        self._find_hits = []
        self.model.set_find(None)
        self.table.setFocus()

    def _selected_plain_text(self) -> str:
        editor = self._active_subtitle_editor() if hasattr(self, "_active_subtitle_editor") else None
        if editor is not None:
            return editor.textCursor().selectedText().strip()
        return ""

    # ── 查找 ──
    def _find_query(self):
        return self.find_input.text() if hasattr(self, "find_input") else ""

    def _find_targets(self):
        """参与查找的字幕：只在待确认里 → AI 待确认的句子；列表正在筛选待确认时也只查这些。"""

        if not self.document:
            return []
        entries = self.document.entries
        pending = None
        if self.find_pending_only.isChecked() or getattr(self, "_pending_filter", False):
            pending = {item.cue_id for item in self.ai_session.pending} if self.ai_session else set()
        return [entry for entry in entries if pending is None or entry.index in pending]

    def _find_matches(self):
        query = self._find_query()
        if not query:
            return []
        sensitive = self.find_case.isChecked()
        needle = query if sensitive else query.casefold()
        hits = []
        for entry in self._find_targets():
            haystack = entry.text if sensitive else entry.text.casefold()
            start = haystack.find(needle)
            while start >= 0:
                hits.append((entry.index, start))
                start = haystack.find(needle, start + len(needle))
        return hits

    def _refind(self):
        if not hasattr(self, "find_bar") or not self.find_bar.isVisibleTo(self):
            return
        self._find_hits = self._find_matches()
        self._find_current = 0
        self._show_find_state()
        if self._find_hits:
            self._find_jump(0)

    def _show_find_state(self):
        query = self._find_query()
        hits = self._find_hits
        if not query:
            self.find_count.setText("")
        elif hits:
            self.find_count.setText(f"{self._find_current + 1} / {len(hits)}")
        else:
            self.find_count.setText("无结果")
        self.find_count.setObjectName("danger" if query and not hits else "mono")
        self.find_count.style().unpolish(self.find_count)
        self.find_count.style().polish(self.find_count)
        for button in (self.find_prev, self.find_next, self.replace_one_button, self.replace_all_button):
            button.setEnabled(bool(hits) and not self._saving)
        current = hits[self._find_current] if hits else None
        self.model.set_find(query or None, self.find_case.isChecked(), current)

    def _find_step(self, delta):
        if not self._find_hits:
            return
        self._find_jump((self._find_current + delta) % len(self._find_hits))

    def _find_jump(self, position):
        """跳到第 position 处命中：选中那句、定位到它的开头并暂停，列表把它滚到中间。"""

        self._find_current = position
        cue_id, _start = self._find_hits[position]
        self._show_find_state()
        entry = next((item for item in self.document.entries if item.index == cue_id), None)
        if entry is None:
            return
        self.selection.choose(cue_id, self._cue_order())
        self.selected_index = cue_id
        self._select_cue(cue_id)
        self._seek_to(srt_timestamp_seconds(entry.start))
        row = self._row_of_cue(cue_id)
        if row >= 0:
            index = self.model.index(row, 2)
            self.table.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
            QTimer.singleShot(0, self.table, lambda: motion.pulse(self.table.viewport(), self.table.visualRect(index)))

    # ── 替换 ──
    def _replace_text(self, text: str, start: int, length: int) -> str:
        return text[:start] + self.replace_input.text() + text[start + length:]

    def _replace_one(self):
        if self._saving or not self.document or not self._find_hits:
            return
        self._commit_editor()
        cue_id, start = self._find_hits[self._find_current]
        entry = next((item for item in self.document.entries if item.index == cue_id), None)
        if entry is None:
            return
        length = len(self._find_query())
        self.document.edit_text(cue_id, self._replace_text(entry.text, start, length))
        self._after_replace(1, {cue_id})
        keep = min(self._find_current, max(0, len(self._find_matches()) - 1))
        self._find_hits = self._find_matches()
        if self._find_hits:
            self._find_jump(keep)
        else:
            self._show_find_state()

    def _replace_all(self):
        if self._saving or not self.document or not self._find_hits:
            return
        self._commit_editor()
        length = len(self._find_query())
        changes: dict[int, str] = {}
        count = 0
        texts = {entry.index: entry.text for entry in self.document.entries}
        # 同一句里从后往前换，前面命中的位置不受影响
        for cue_id, start in sorted(self._find_hits, key=lambda hit: (hit[0], -hit[1])):
            current = changes.get(cue_id, texts[cue_id])
            changes[cue_id] = self._replace_text(current, start, length)
            count += 1
        changed = self.document.edit_texts(changes)
        self._after_replace(count, set(changes), sentences=changed)
        self._find_hits = self._find_matches()
        self._find_current = 0
        self._show_find_state()

    def _after_replace(self, count: int, cue_ids: set[int], sentences: int | None = None):
        selected = self.selected_index
        self.model.set_document(self.document)
        if selected is not None:
            self._select_cue(selected)
        self._edited()
        sentences = len(cue_ids) if sentences is None else sentences
        message = f"已替换 {count} 处" if count == 1 else f"已在 {sentences} 条字幕里替换 {count} 处"
        self._toast(message, undo=True)
