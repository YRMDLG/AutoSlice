"""AI 校对：面板、问题列表、差异展示、采纳/跳过/回退与后台检查。

混入 DesktopWindow，排在 PreviewWindow 之前；状态都在窗口上，不单独实例化。
"""

from __future__ import annotations

import copy
import difflib
import html
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.ai_review import AIReviewSession, document_hash
from autoslice.desktop.homophone import available as homophone_available
from autoslice.desktop.qt_preview import motion
from autoslice.desktop.qt_preview.theme import COLORS, SIZES
from autoslice.desktop.qt_preview.window import label, line, tool_icon
from autoslice.transcription.contracts import srt_timestamp_seconds

from .window_widgets import _ElidedQueueButton


class AiReviewMixin:
    def _ai_panel(self):
        panel = QWidget()
        panel.setObjectName("aiPanel")
        panel.setMinimumWidth(SIZES.ai_collapsed_width)
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        # 与投稿项目、工作区工具栏同高
        header_bar = QWidget()
        header_bar.setFixedHeight(58)
        header = QHBoxLayout(header_bar)
        header.setContentsMargins(8, 0, 12, 0)
        header.setSpacing(8)
        self.ai_toggle = QPushButton()
        self.ai_toggle.setObjectName("quiet")
        tool_icon(self.ai_toggle, "chevron_left")
        self.ai_toggle.setFixedSize(32, 32)
        self.ai_toggle.clicked.connect(lambda: self._set_ai_open(not self._ai_open))
        header.addWidget(self.ai_toggle)
        self.ai_header = label("AI 建议", "sectionTitle")
        header.addWidget(self.ai_header)
        header.addStretch()
        self.ai_run = QPushButton("AI 检查")
        self.ai_run.setObjectName("quiet")
        self.ai_run.setFixedHeight(28)
        self.ai_run.setToolTip("把字幕发给 AI 检查错字，会消耗额度；只生成待确认建议")
        self.ai_run.clicked.connect(self._ai_run_clicked)
        header.addWidget(self.ai_run)
        column.addWidget(header_bar)
        column.addWidget(line())
        self.ai_content = QScrollArea()
        self.ai_content.setWidgetResizable(True)
        self.ai_content.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        ai_body = QWidget()
        content = QVBoxLayout(ai_body)
        content.setContentsMargins(16, 12, 16, 14)
        content.setSpacing(8)
        self.ai_note = label("AI 只生成待确认建议，不修改字幕。", "muted")
        self.ai_note.setWordWrap(True)
        content.addWidget(self.ai_note)
        # 没配 AI 时给直达入口，不让人自己去设置页底下找
        self.ai_setup = QPushButton("去设置 AI 接口")
        self.ai_setup.setObjectName("secondary")
        self.ai_setup.clicked.connect(self._open_ai_settings)
        content.addWidget(self.ai_setup, 0, Qt.AlignmentFlag.AlignLeft)
        self.ai_setup.hide()
        # 免费的本地检查带字放在正文里：表头放不下两个带字按钮，只有闪电图标新手认不出
        local_row = QHBoxLayout()
        local_row.setSpacing(8)
        self.ai_local = QPushButton("本地检查")
        self.ai_local.setObjectName("secondary")
        tool_icon(self.ai_local, "bolt")
        self.ai_local.setToolTip("本地检查（免费、秒出、不用 AI）：主播固定纠错、你以前改过的错字、发音和术语相同写法不同的字（如“紫雨亭”→“子语庭”）")
        self.ai_local.clicked.connect(self._start_local_check)
        local_row.addWidget(self.ai_local)
        local_row.addWidget(label("免费，不用 AI", "subtle"))
        local_row.addStretch()
        content.addLayout(local_row)
        # 固定纠错、记住的改法很可靠，一条条点太慢：一次采纳，一步撤回
        self.ai_apply_local = QPushButton("全部采纳本地纠错")
        self.ai_apply_local.setObjectName("secondary")
        self.ai_apply_local.setToolTip("主播固定纠错和已经记住的校对改法一次采纳；同音字、观察中的改法仍要逐条看。"
                                       "只改界面里的字幕，保存前不写文件；Ctrl+Z 一步撤回")
        self.ai_apply_local.clicked.connect(self._apply_local_batch)
        content.addWidget(self.ai_apply_local, 0, Qt.AlignmentFlag.AlignLeft)
        self.ai_apply_local.hide()
        # 复查只在做过一次 AI 检查后才有意义，之前整行藏起来
        self.ai_scope_row = QWidget()
        scope_row = QHBoxLayout(self.ai_scope_row)
        scope_row.setContentsMargins(0, 0, 0, 0)
        scope_row.setSpacing(8)
        self.ai_selected_check = QPushButton("复查选中")
        self.ai_selected_check.setToolTip("仅检查选中的字幕，保留其它已确认结果")
        self.ai_selected_check.clicked.connect(lambda: self._start_ai_check("selected"))
        scope_row.addWidget(self.ai_selected_check)
        self.ai_changed_check = QPushButton("复查改动")
        self.ai_changed_check.setToolTip("只检查上次 AI 检查后改动或新增的字幕")
        self.ai_changed_check.clicked.connect(lambda: self._start_ai_check("changed"))
        scope_row.addWidget(self.ai_changed_check)
        scope_row.addStretch()
        content.addWidget(self.ai_scope_row)
        self.ai_scope_row.hide()

        self.ai_card = QWidget()
        self.ai_card.setObjectName("aiSuggestionCard")
        card = QVBoxLayout(self.ai_card)
        card.setContentsMargins(12, 10, 12, 10)
        card.setSpacing(6)

        meta = QHBoxLayout()
        meta.setSpacing(8)
        self.ai_cue = label("", "subtle")
        self.ai_cue.setObjectName("aiMeta")
        meta.addWidget(self.ai_cue)
        meta.addStretch()
        self.ai_confidence = label("", "subtle")
        self.ai_confidence.setObjectName("aiMeta")
        meta.addWidget(self.ai_confidence)
        card.addLayout(meta)

        self.ai_original_label = label("原文", "subtle")
        self.ai_original_label.setObjectName("aiFieldLabel")
        card.addWidget(self.ai_original_label)
        self.ai_original = label("", "muted")
        self.ai_original.setObjectName("aiOriginal")
        self.ai_original.setTextFormat(Qt.TextFormat.RichText)
        self.ai_original.setWordWrap(True)
        self.ai_original.setMinimumWidth(0)
        self.ai_original.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.ai_original.setMaximumHeight(40)
        card.addWidget(self.ai_original)

        self.ai_diff_label = label("建议", "subtle")
        self.ai_diff_label.setObjectName("aiFieldLabel")
        card.addWidget(self.ai_diff_label)
        self.ai_diff = QLabel("")
        self.ai_diff.setObjectName("aiDiff")
        self.ai_diff.setWordWrap(True)
        self.ai_diff.setTextFormat(Qt.TextFormat.RichText)
        self.ai_diff.setMinimumWidth(0)
        self.ai_diff.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.ai_diff.setMaximumHeight(48)
        card.addWidget(self.ai_diff)

        self.ai_reason_label = label("原因", "subtle")
        self.ai_reason_label.setObjectName("aiFieldLabel")
        card.addWidget(self.ai_reason_label)
        self.ai_reason = label("", "subtle")
        self.ai_reason.setObjectName("aiReason")
        self.ai_reason.setWordWrap(True)
        self.ai_reason.setMinimumWidth(0)
        self.ai_reason.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.ai_reason.setMaximumHeight(54)
        card.addWidget(self.ai_reason)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.ai_accept = QPushButton("采纳")
        self.ai_accept.setObjectName("primary")
        self.ai_accept.setToolTip("采纳这条建议并看下一条（Alt+A）")
        self.ai_accept.clicked.connect(self._accept_ai)
        actions.addWidget(self.ai_accept, 2)
        self.ai_skip = QPushButton("跳过")
        self.ai_skip.setObjectName("secondary")
        self.ai_skip.setToolTip("不改这条，看下一条（Alt+S）")
        self.ai_skip.clicked.connect(self._skip_ai)
        actions.addWidget(self.ai_skip, 1)
        card.addLayout(actions)
        content.addWidget(self.ai_card)
        self.ai_card.hide()

        self.ai_queue_header = label("待处理 0", "subtle")
        self.ai_queue_header.setObjectName("aiQueueHeader")
        content.addWidget(self.ai_queue_header)
        self.ai_queue_header.hide()
        self.ai_queue = QScrollArea()
        self.ai_queue.setWidgetResizable(True)
        self.ai_queue.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.ai_queue.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.ai_queue.setMaximumHeight(132)
        self.ai_queue_widget = QWidget()
        self.ai_queue_layout = QVBoxLayout(self.ai_queue_widget)
        self.ai_queue_layout.setContentsMargins(0, 0, 0, 0)
        self.ai_queue_layout.setSpacing(2)
        self.ai_queue_layout.addStretch()
        self.ai_queue.setWidget(self.ai_queue_widget)
        content.addWidget(self.ai_queue)
        self.ai_queue.hide()
        content.addStretch(1)
        self.ai_content.setWidget(ai_body)
        column.addWidget(self.ai_content, 1)
        self.ai_collapsed_holder = QWidget()
        holder = QVBoxLayout(self.ai_collapsed_holder)
        holder.setContentsMargins(0, 12, 0, 0)
        self.ai_collapsed_label = label("AI", "badge")
        holder.addWidget(self.ai_collapsed_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        holder.addStretch()
        column.addWidget(self.ai_collapsed_holder, 1)
        # 过渡中内容隐藏时由占位吸收高度，标题行保持在顶部
        column.addStretch()
        return panel

    def _ai_chrome(self, open_):
        super()._ai_chrome(open_)
        if hasattr(self, "ai_collapsed_holder"):
            self.ai_collapsed_holder.setVisible(open_ is False)
        if hasattr(self, "ai_run"):
            self.ai_run.setVisible(bool(open_))

    def _ai_fade_targets(self, open_):
        return [self.ai_content, self.ai_header, self.ai_run] if open_ else [self.ai_collapsed_holder]

    def _set_filter(self, pending):
        self._commit_editor()
        self._pending_filter = pending
        (self.filter_pending if pending else self.filter_all).setChecked(True)
        ids = {item.cue_id for item in self.ai_session.pending} if pending and self.ai_session else set()
        self.model.set_pending_filter(ids if pending else None)
        self._select_cue(self.selected_index)

    def _render_ai(self):
        if not hasattr(self, "ai_queue_layout"):
            return
        pending = self.ai_session.pending if self.ai_session else []
        count = len(pending)
        self.filter_pending.setText(f"待确认 {count}")
        badge_count = max(count, self._ai_return_count)
        self.nav_buttons[0].setToolTip(f"字幕校对 · 待确认 {badge_count}" if badge_count else "字幕校对")
        if hasattr(self, "nav_badge"):
            self.nav_badge.setText(str(badge_count))
            self.nav_badge.adjustSize()
            motion.reveal(self.nav_badge, badge_count > 0)
        self.ai_collapsed_label.setText(f"AI {count}" if count else "AI")
        self.ai_run.setEnabled(bool(self.document) and not self._saving)
        self.ai_local.setEnabled(bool(self.document) and not self._ai_running and not self._saving)
        for button in (self.ai_selected_check, self.ai_changed_check):
            button.setEnabled(bool(self.document) and not self._ai_running and not self._saving)
        batch = self._batch_local()
        self.ai_apply_local.setText(f"全部采纳本地纠错（{len(batch)} 条）")
        self.ai_apply_local.setVisible(bool(batch))
        self.ai_apply_local.setEnabled(bool(self.document) and not self._saving)
        rechecking = bool(self.ai_session) and not self.ai_session.local_only
        self.ai_scope_row.setVisible(rechecking and bool(self.document))
        if rechecking:
            self.ai_setup.hide()
        left = len(self.ai_session.unchecked(self.document.entries)) if rechecking and self.document else 0
        self.ai_run.setText("取消检查" if self._ai_running else
                            "继续检查" if left else "重新检查" if rechecking else "AI 检查")
        self.ai_run.setToolTip(
            f"还有 {left} 条没查（上次没查完，或查过之后改过），只发这些" if left and not self._ai_running else
            "把字幕发给 AI 检查错字，会消耗额度；只生成待确认建议")
        if self._ai_running:
            self.ai_run.setIcon(QIcon())
        else:
            tool_icon(self.ai_run, "refresh" if rechecking else "sparkle")
        pending_ids = {item.suggestion_id for item in pending}
        if self._ai_selected not in pending_ids:
            self._ai_selected = pending[0].suggestion_id if pending else None
        selected_position = next((i for i, item in enumerate(pending, 1)
                                  if item.suggestion_id == self._ai_selected), 0)
        while self.ai_queue_layout.count() > 1:
            item = self.ai_queue_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for item in pending:
            summary = (f"{item.original_text} → {item.suggested_text}"
                       if item.original_text != item.suggested_text else item.original_text)
            full_text = f"第 {item.cue_id} 条  {summary}  · 待确认"
            button = _ElidedQueueButton(full_text)
            button.setObjectName("aiQueueItem")
            button.setToolTip(
                f"原文：{item.original_text}\n建议：{item.suggested_text}\n原因：{item.reason}"
            )
            button.setAccessibleName(f"第 {item.cue_id} 条待处理问题")
            button.setProperty("suggestion_id", item.suggestion_id)
            button.setCheckable(True)
            button.setChecked(item.suggestion_id == self._ai_selected)
            button.setFixedHeight(30)
            button.clicked.connect(lambda _checked=False, sid=item.suggestion_id: self._select_ai(sid))
            self.ai_queue_layout.insertWidget(self.ai_queue_layout.count() - 1, button)
        motion.install_tree(self.ai_queue_widget)
        self.ai_header.setText(f"AI 建议  {selected_position} / {count}" if self.ai_session else "AI 建议")
        self.ai_queue_header.setText(f"待处理 {count}")
        self.ai_queue_header.setVisible(count > 0)
        self.ai_queue.setVisible(count > 0)
        if count:
            self.ai_queue.setFixedHeight(min(132, count * 32 + 2))
        if self.ai_session and not count and not self._ai_running:
            self.ai_note.setText("已处理所有建议。需要时可重新检查。")
        routine_notes = ("AI 只生成待确认建议，不修改字幕。", "检查完成。建议需逐条确认。",
                         "已恢复待确认建议。")
        self.ai_note.setVisible(not count or self._ai_running or
                                self.ai_note.text() not in routine_notes)
        self._show_ai_detail()
        if getattr(self, "_pending_filter", False):
            self._commit_editor()
            self.model.set_pending_filter({item.cue_id for item in pending})

    @staticmethod
    def _diff_sides(original, suggested):
        """(原文, 建议) 两行各自成句：原文里要删的字标红加底色，建议里新加的字标绿。

        删的、加的挤在一行会读成“要教交他就教交这个呗”，像病句。
        """

        def wrap_text(value):
            return "&#8203;".join(html.escape(char) for char in value)

        old_pieces, new_pieces = [], []
        for kind, a, b, c, d in difflib.SequenceMatcher(None, original, suggested).get_opcodes():
            old = wrap_text(original[a:b])
            new = wrap_text(suggested[c:d])
            if kind == "equal":
                old_pieces.append(old)
                new_pieces.append(new)
                continue
            if old:
                old_pieces.append(f'<span style="color:{COLORS.ai_removed};background-color:{COLORS.ai_removed_bg};'
                                  f'text-decoration:line-through">{old}</span>')
            if new:
                new_pieces.append(f'<span style="color:{COLORS.ai_added};font-weight:600">{new}</span>')
        return tuple("".join(pieces).replace("\n", "<br>") for pieces in (old_pieces, new_pieces))

    def _show_ai_detail(self):
        item = self.ai_session.find(self._ai_selected) if self.ai_session and self._ai_selected else None
        if item is None:
            self.ai_card.hide()
            self.ai_diff.hide()
            self.ai_cue.hide()
            self.ai_original.hide()
            self.ai_reason.hide()
            self.ai_confidence.hide()
            self.ai_accept.hide()
            self.ai_skip.hide()
            return
        if getattr(self, "_ai_shown", None) != item.suggestion_id:
            self._ai_shown = item.suggestion_id
            motion.fade_in(self.ai_content.widget(), motion.FADE, start=0.25)
        entry = next((entry for entry in self.document.entries if entry.index == item.cue_id), None) if self.document else None
        conflict = entry is None or entry.text != item.original_text
        self.ai_card.show()
        self.ai_cue.setText(f"第 {item.cue_id} 条")
        self.ai_cue.show()
        old_html, new_html = self._diff_sides(item.original_text, item.suggested_text)
        self.ai_original.setText(old_html)
        self.ai_original.setToolTip(item.original_text)
        self.ai_original.show()
        self.ai_diff.setText(new_html)
        self.ai_diff.setToolTip(f"建议：{item.suggested_text}")
        self.ai_diff.show()
        reason = item.reason + ("\n字幕已修改或删除；建议基于旧文本，请重新判断或跳过。" if conflict else "")
        self.ai_reason.setText(reason)
        self.ai_reason.setToolTip(reason)
        self.ai_reason.show()
        self.ai_confidence.setText(f"置信度 {item.confidence:.0%}" if item.confidence is not None else "")
        self.ai_confidence.setVisible(item.confidence is not None)
        self.ai_accept.setEnabled(not conflict and not self._saving)
        self.ai_accept.show()
        self.ai_skip.show()

    def _select_ai(self, suggestion_id):
        # 建议按钮动态创建，不依赖初始化时安装的鼠标过滤器来结束正文编辑。
        self._commit_editor()
        self._ai_selected = suggestion_id
        if self.ai_session:
            pending = self.ai_session.pending
            selected_position = next((i for i, item in enumerate(pending, 1)
                                      if item.suggestion_id == suggestion_id), 0)
            self.ai_header.setText(f"AI 建议  {selected_position} / {len(pending)}")
        for index in range(self.ai_queue_layout.count() - 1):
            button = self.ai_queue_layout.itemAt(index).widget()
            if button is not None:
                button.setChecked(button.property("suggestion_id") == suggestion_id)
        self._show_ai_detail()
        if not self.ai_session or not self.document:
            return
        item = self.ai_session.find(suggestion_id)
        entry = next((entry for entry in self.document.entries if entry.index == item.cue_id), None)
        if entry is not None:
            self.selection.choose(entry.index, self._cue_order())
            self.selected_index = entry.index
            if getattr(self, "_pending_filter", False):
                self.model.set_pending_filter({s.cue_id for s in self.ai_session.pending})
            self._select_cue(entry.index)
            self._seek_to(srt_timestamp_seconds(entry.start))
            self._save_session()

    def _accept_ai(self):
        if self._saving or not self.document or not self.ai_session or not self._ai_selected:
            return
        self._commit_editor()
        item = self.ai_session.find(self._ai_selected)
        entry = next((entry for entry in self.document.entries if entry.index == item.cue_id), None)
        if entry is None or entry.text != item.original_text:
            self._show_ai_detail()
            return
        before = document_hash(self.document.entries)
        self.document.edit_text(item.cue_id, item.suggested_text)
        after = document_hash(self.document.entries)
        self._ai_undo.append((before, after, item.suggestion_id))
        self._ai_redo.clear()
        self.ai_session.mark(item.suggestion_id, "accepted", self.document.entries)
        self._save_ai_session()
        self.model.set_document(self.document)
        self._edited()
        self._render_ai()
        if self.ai_session.pending:
            next_item = next((candidate for candidate in self.ai_session.pending
                              if candidate.cue_id > item.cue_id), self.ai_session.pending[0])
            self._select_ai(next_item.suggestion_id)
        else:
            self._select_cue(item.cue_id)

    def _skip_ai(self):
        if self._saving or not self.document or not self.ai_session or not self._ai_selected:
            return
        self._commit_editor()
        current = self.ai_session.find(self._ai_selected)
        self._ai_skip_undo.append((document_hash(self.document.entries), self._ai_selected))
        self._ai_skip_redo.clear()
        self.ai_session.mark(self._ai_selected, "skipped", self.document.entries)
        self._save_ai_session()
        self._render_ai()
        if self.ai_session.pending:
            next_item = next((candidate for candidate in self.ai_session.pending
                              if candidate.cue_id > current.cue_id), self.ai_session.pending[0])
            self._select_ai(next_item.suggestion_id)

    def _batch_local(self):
        """能一次采纳的本地建议：主播固定纠错和已记住的改法；同音字、观察中的改法要人看，不算。"""

        if not self.ai_session or not self.document:
            return []
        seen, result = set(), []
        for item in self.ai_session.pending:
            reliable = item.source == "fixed_replacement" or (item.source == "memory" and "观察中" not in item.reason)
            if reliable and "疑似识别听错" not in item.reason and item.cue_id not in seen:
                seen.add(item.cue_id)
                result.append(item)
        return result

    def _apply_local_batch(self):
        if self._saving or not self.document or not self.ai_session:
            return
        self._commit_editor()
        items = self._batch_local()
        texts = {entry.index: entry.text for entry in self.document.entries}
        ready = [item for item in items if texts.get(item.cue_id) == item.original_text]
        conflicts = len(items) - len(ready)
        if not ready:
            self.ai_note.setText("这些本地纠错对应的字幕都已经改过了，没有可以直接采纳的；可以逐条看看。")
            self.ai_note.show()
            return
        listing = "\n".join(f"第 {item.cue_id} 条：{item.original_text} → {item.suggested_text}" for item in ready[:12])
        if len(ready) > 12:
            listing += f"\n……等共 {len(ready)} 条"
        choice = QMessageBox.question(
            self, "全部采纳本地纠错",
            f"将按本地纠错改掉下面 {len(ready)} 条字幕（只改界面里的字幕，保存前不写文件，Ctrl+Z 一步撤回）：\n\n"
            f"{listing}" + (f"\n\n另有 {conflicts} 条原文已经被你改过，跳过不动。" if conflicts else ""),
        )
        if choice != QMessageBox.StandardButton.Yes:
            return
        before = document_hash(self.document.entries)
        self.document.edit_texts({item.cue_id: item.suggested_text for item in ready})
        after = document_hash(self.document.entries)
        # 撤销时整批一起回到待确认
        self._ai_undo.append((before, after, tuple(item.suggestion_id for item in ready)))
        self._ai_redo.clear()
        for item in ready:
            self.ai_session.mark(item.suggestion_id, "accepted", self.document.entries)
        self._save_ai_session()
        self.model.set_document(self.document)
        self._edited()
        self._render_ai()
        self._select_cue(self.selected_index)
        self._show_transient_status(f"已采纳 {len(ready)} 条本地纠错（Ctrl+Z 撤销）"
                                    + (f"，{conflicts} 条原文改过已跳过" if conflicts else ""), 6000)

    def _save_ai_session(self):
        if self.ai_session and self.document and self.project:
            try:
                self.ai_service.save(self.document, self.project.title, self.ai_session)
            except (OSError, ValueError) as exc:
                self.ai_note.setText(f"建议状态暂未保存：{exc}")

    def _return_to_ai(self):
        self._select_page(0)
        project = next((p for p in self.service.snapshot.projects if p.id == self._ai_return_project), None)
        if project:
            if not self._open_task_video(project.id, getattr(self, "_ai_return_video_path", "")):
                return
        if self.document and self.ai_session and self.ai_session.pending:
            self._set_ai_open(True)
            self._select_ai(self.ai_session.pending[0].suggestion_id)
        self._ai_return_count = 0
        motion.reveal(self.ai_return, False)
        self._render_ai()

    def _start_local_check(self):
        """本地检查：纯本地、立刻出结果，建议并进同一个待确认队列，不需要配置 AI。"""

        if self._saving or not self.document or not self.project or self._ai_running:
            return
        self._commit_editor()
        available = homophone_available()
        found = self.ai_service.local_suggestions(self.document, self.project.title, homophones=available)
        if not available and not found:
            self.ai_note.setText("本地检查：固定纠错和校对记忆没发现要改的地方；同音字这项需要 pypinyin（在运行 AutoSlice 的 Python 里执行 pip install pypinyin）")
            self.ai_note.show()
            return
        session = self.ai_session
        known = {(item.cue_id, item.suggested_text) for item in session.suggestions} if session else set()
        fresh = [item for item in found if (item.cue_id, item.suggested_text) not in known]
        if fresh:
            content = document_hash(self.document.entries)
            if session is None:
                session = AIReviewSession(fresh, content[:20], content, local_only=True)
            else:
                session.suggestions = [*session.suggestions, *fresh]
                session.content_hash = content
            self.ai_session = session
            self._ai_selected = None
            self._save_ai_session()
        self._render_ai()
        if fresh:
            self._set_ai_open(True)
            fresh_ids = {item.suggestion_id for item in fresh}
            self._follow_first_pending(fresh_ids)
            self.ai_note.setText(f"本地检查：找到 {len(fresh)} 处待确认改字，逐条确认即可。" + ("" if available else "（没装 pypinyin，同音字这项没查）"))
        else:
            self.ai_note.setText("本地检查：固定纠错、校对记忆和同音字都没发现要改的地方。" if available else "本地检查：固定纠错和校对记忆没发现要改的地方（没装 pypinyin，同音字这项没查）。")
        self.ai_note.show()

    def _follow_first_pending(self, among=None):
        """卡片显示哪条，字幕列表和时间轴就跟到哪条；正在改字或不在字幕页时不打扰。"""

        if not self.ai_session or self.pages.currentIndex() != 0 or self._subtitle_editor_open():
            return
        target = next((item for item in self.ai_session.pending
                       if among is None or item.suggestion_id in among), None)
        if target is not None:
            self._select_ai(target.suggestion_id)

    def _ai_configured(self):
        try:
            self.ai_service.config_loader()
        except (OSError, ValueError):
            return False
        return True

    def _show_ai_unconfigured(self):
        self.ai_note.setText("还没配置 AI 接口：AI 检查要先在设置页填好接口地址和密钥。"
                             "下面的“本地检查”不用 AI，可以先用它查一遍。")
        self._set_ai_open(True)
        self.ai_note.show()
        self.ai_setup.show()

    def _open_ai_settings(self):
        """跳到设置页的“AI 接口”一节，光标放进还没填的那一格。"""

        self._select_page(2)
        scroll, panel = getattr(self, "settings_scroll", None), getattr(self, "ai_settings_panel", None)
        if scroll is None or panel is None:
            return

        def reveal():
            scroll.ensureWidgetVisible(panel, 0, 24)
            panel.focus_first_missing()

        QTimer.singleShot(0, reveal)

    def _ai_run_clicked(self, _checked=False):
        """按钮入口：第一次全量检查前先确认，免得一点就开始花额度（重新检查在下面另有确认）。"""

        if not self._ai_running and not self._ai_configured():
            # 没配置就别问“会消耗额度，开始吗”，点了“是”才报错
            self._show_ai_unconfigured()
            return
        self.ai_setup.hide()
        first_run = (not self._ai_running and not self._saving and self.document is not None
                     and self.project is not None and (self.ai_session is None or self.ai_session.local_only))
        if first_run:
            count = len(self.document.entries)
            choice = QMessageBox.question(
                self, "AI 检查字幕",
                f"将把 {count} 条字幕分批发给 AI 检查错字，会消耗额度。\n"
                "检查只生成待确认建议，不会直接改字幕。开始吗？",
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
        self._start_ai_check()

    def _start_ai_check(self, scope="all"):
        if self._saving:
            return
        if self._ai_running:
            self._ai_cancel.set()
            self.ai_note.setText("正在取消检查…")
            return
        if not self.document or not self.project:
            return
        self._commit_editor()
        cue_ids = None
        if scope == "selected":
            cue_ids = set(self.selection.selected)
            if not cue_ids:
                self.ai_note.setText("请先在字幕列表或时间轴选中需要复查的字幕。")
                return
        elif scope == "changed":
            if not self.ai_session or not self.ai_session.reviewed_entries:
                self.ai_note.setText("请先完成一次 AI 检查，再使用“复查改动”。")
                return
            cue_ids = self.ai_session.changed_indices(self.document.entries)
            if not cue_ids:
                self.ai_note.setText("上次检查后没有新的字幕改动。")
                return
        rechecking = self.ai_session is not None and not self.ai_session.local_only
        # 全量检查：上次没查完（或查过后改了字）就只查剩下的；都查过了才是整份重查
        left = self.ai_session.unchecked(self.document.entries) if rechecking and cue_ids is None else []
        force = rechecking and not left
        if left:
            choice = QMessageBox.question(self, "继续检查字幕",
                                          f"还有 {len(left)} 条字幕没查（上次没查完，或查过之后改过），"
                                          "只把这些发给 AI，会消耗额度。继续吗？")
            if choice != QMessageBox.StandardButton.Yes:
                return
        elif rechecking:
            choice = QMessageBox.question(self, "重新检查字幕",
                                          f"将复查 {len(cue_ids) if cue_ids is not None else len(self.document.entries)} 条字幕，会调用 AI 并消耗额度，确定继续吗？")
            if choice != QMessageBox.StandardButton.Yes:
                return
        snapshot = copy.copy(self.document)
        snapshot.entries = list(self.document.entries)
        project = self.project
        previous = copy.deepcopy(self.ai_session)
        self._ai_running = True
        self._ai_cancel = threading.Event()
        cancellation = self._ai_cancel
        self._ai_generation += 1
        generation = self._ai_generation
        self._task_update("ai", kind="AI 校对", project=project, video_path=snapshot.video.path,
                          message="正在检查字幕…", cancel=cancellation.set)
        self.app_status.setText(f"{project.title} · AI 检查中")
        self.ai_note.setText("后台检查中，可切换页面。")
        self._render_ai()
        def action():
            def progress(_message, _step, _total):
                if cancellation.is_set():
                    raise RuntimeError("检查已取消")
                self.ai_progress.emit(generation, project.title, _step, _total)

            def partial(session):
                # 每批查完就送回界面线程：边查边显示，当场落盘，取消、关窗都不丢
                self.ai_partial.emit(generation, snapshot, project.title, session)

            return self.ai_service.check(snapshot, project.title, force=force, progress_callback=progress,
                                         cue_ids=cue_ids, previous=previous, partial_callback=partial)
        self._run(action, lambda result, error: self._ai_finished(
            generation, project, snapshot, result, error))

    def _absorb_ai_result(self, snapshot, project_title, session) -> bool:
        """把查完的结果并进这个视频的建议会话并保存；返回是不是当前打开的视频。

        并而不是替换：检查途中用户采纳 / 跳过的状态留着，改过的句子只作废对不上的那几条。
        用户已经切到别的视频时，并进那个视频存在磁盘上的会话。
        """

        if self.document is not None and self.document.video.path == snapshot.video.path:
            if self.ai_session is None or self.ai_session.local_only and not session.local_only:
                local = self.ai_session
                self.ai_session = copy.deepcopy(session)
                self.ai_session.suggestions = []
                if local is not None:
                    self.ai_session.suggestions = list(local.suggestions)
            self.ai_session.absorb(session, self.document.entries)
            self._save_ai_session()
            return True
        try:
            stored, _status = self.ai_service.load(snapshot, project_title)
        except (OSError, ValueError):
            stored = None
        if stored is None:
            stored = session
        else:
            stored.absorb(session, snapshot.entries)
        try:
            self.ai_service.save(snapshot, project_title, stored)
        except (OSError, ValueError):
            pass
        return False

    def _ai_partial_ready(self, generation, snapshot, project_title, session):
        if generation != self._ai_generation:
            return
        here = self._absorb_ai_result(snapshot, project_title, session)
        if not here:
            return
        first = getattr(self, "_ai_followed", None) != generation and bool(self.ai_session.pending)
        self._render_ai()
        if first:
            # 第一批出结果就打开面板、跟到第一条：不用等全部查完
            self._ai_followed = generation
            self._set_ai_open(True)
            self._follow_first_pending()

    def _show_ai_progress(self, generation, project_title, step, total):
        if generation == self._ai_generation and self._ai_running:
            self._task_update("ai", message=f"正在检查 {step}/{total} 批" if total else "正在检查字幕…")
            self.app_status.setText(f"{project_title} · AI 检查中"
                                    + (f" ({step}/{total} 批)" if total > 0 else ""))

    def _ai_finished(self, generation, project, snapshot, result, error):
        if generation != self._ai_generation:
            return
        self._task_update("ai", state="cancelled" if error and self._ai_cancel and self._ai_cancel.is_set() else
                          "error" if error else "done",
                          message=str(error) if error else f"检查完成，{len(result.pending)} 条待确认建议")
        self._ai_running = False
        if error:
            message = str(error)
            cancelled = "检查已取消" in message
            self.app_status.setText(f"{project.title} · {'检查已取消' if cancelled else 'AI 检查失败'}")
            here = self.document is not None and self.document.video.path == snapshot.video.path
            left = len(self.ai_session.unchecked(self.document.entries)) if here and self.ai_session else 0
            kept = (f"已查完的建议都留着，还有 {left} 条没查，点“继续检查”只查这些。"
                    if left and left < len(self.document.entries) else "")
            self.ai_note.setText(("检查已取消。" + kept if kept else "检查已取消，可稍后主动重试。") if cancelled else
                                 "未配置 AI，请在设置页配置后主动重试。" if "未配置" in message else
                                 f"检查失败：{message}" + (f" {kept}" if kept else ""))
            self.ai_setup.setVisible("未配置" in message)
        else:
            self._show_transient_status("✓ AI 检查完成")
            self.ai_note.setText("检查完成。建议需逐条确认。")
            if self._absorb_ai_result(snapshot, project.title, result):
                pending = self.ai_session.pending
                self._render_ai()
                if pending:
                    self._set_ai_open(True)
                    if getattr(self, "_ai_followed", None) != generation:
                        self._follow_first_pending()
                    if self.pages.currentIndex() != 0:
                        self._ai_return_project = project.id
                        self._ai_return_video_path = snapshot.video.path
                        self._ai_return_count = len(pending)
                        self.ai_return.setText(f"查看 {project.title} 的 {len(pending)} 条建议")
                        motion.reveal(self.ai_return)
            elif result.pending:
                self._ai_return_project = project.id
                self._ai_return_video_path = snapshot.video.path
                self._ai_return_count = len(result.pending)
                self.ai_return.setText(f"查看 {project.title} 的 {len(result.pending)} 条建议")
                motion.reveal(self.ai_return)
        self._render_ai()
