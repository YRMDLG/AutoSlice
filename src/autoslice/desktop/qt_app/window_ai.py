"""AI 校对：面板、问题列表、差异展示、采纳/跳过/回退与后台检查。

混入 DesktopWindow，排在 PreviewWindow 之前；状态都在窗口上，不单独实例化。
"""

from __future__ import annotations

import copy
import difflib
import html
import threading

from PySide6.QtCore import Qt
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

from autoslice.desktop.ai_review import document_hash
from autoslice.desktop.qt_preview.theme import COLORS, SIZES
from autoslice.desktop.qt_preview.window import label, line
from autoslice.transcription.contracts import srt_timestamp_seconds

from .window_widgets import _ElidedQueueButton


class AiReviewMixin:
    def _ai_panel(self):
        panel = QWidget()
        panel.setObjectName("aiPanel")
        panel.setMinimumWidth(SIZES.ai_collapsed_width)
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        self.ai_toggle = QPushButton("‹")
        self.ai_toggle.setObjectName("quiet")
        self.ai_toggle.setFixedSize(32, 32)
        self.ai_toggle.clicked.connect(lambda: self._set_ai_open(not self._ai_open))
        header.addWidget(self.ai_toggle)
        self.ai_header = label("AI 建议", "sectionTitle")
        header.addWidget(self.ai_header)
        header.addStretch()
        self.ai_run = QPushButton("AI 检查")
        self.ai_run.setObjectName("quiet")
        self.ai_run.setFixedHeight(28)
        self.ai_run.setToolTip("重新检查会再次调用 AI 并消耗额度")
        self.ai_run.clicked.connect(self._start_ai_check)
        header.addWidget(self.ai_run)
        column.addLayout(header)
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
        self.ai_accept.clicked.connect(self._accept_ai)
        actions.addWidget(self.ai_accept, 2)
        self.ai_skip = QPushButton("跳过")
        self.ai_skip.setObjectName("secondary")
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
        self.ai_collapsed_label = label("AI", "badge")
        column.addWidget(self.ai_collapsed_label, alignment=Qt.AlignmentFlag.AlignTop)
        return panel

    def _set_ai_open(self, open_):
        super()._set_ai_open(open_)
        self.ai_run.setVisible(open_)

    def _set_filter(self, pending):
        self._commit_editor()
        self._pending_filter = pending
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
            self.nav_badge.setVisible(badge_count > 0)
        self.ai_collapsed_label.setText(f"AI {count}" if count else "AI")
        self.ai_run.setEnabled(bool(self.document))
        self.ai_run.setText("取消检查" if self._ai_running else
                            "重新检查" if self.ai_session else "AI 检查")
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
            self.model.set_pending_filter({item.cue_id for item in pending})

    @staticmethod
    def _diff_html(original, suggested):
        def wrap_text(value):
            return "&#8203;".join(html.escape(char) for char in value)

        pieces = []
        for kind, a, b, c, d in difflib.SequenceMatcher(None, original, suggested).get_opcodes():
            old = wrap_text(original[a:b])
            new = wrap_text(suggested[c:d])
            if kind == "equal":
                pieces.append(new)
            else:
                if old:
                    pieces.append(f'<span style="color:{COLORS.ai_removed};text-decoration:line-through">{old}</span>')
                if new:
                    pieces.append(f'<span style="color:{COLORS.ai_added};font-weight:600">{new}</span>')
        return "".join(pieces).replace("\n", "<br>")

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
        entry = next((entry for entry in self.document.entries if entry.index == item.cue_id), None) if self.document else None
        conflict = entry is None or entry.text != item.original_text
        self.ai_card.show()
        self.ai_cue.setText(f"第 {item.cue_id} 条")
        self.ai_cue.show()
        self.ai_original.setText(item.original_text)
        self.ai_original.setToolTip(item.original_text)
        self.ai_original.show()
        self.ai_diff.setText(self._diff_html(item.original_text, item.suggested_text))
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
        if not self.document or not self.ai_session or not self._ai_selected:
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
        if not self.document or not self.ai_session or not self._ai_selected:
            return
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
            self._session["video_path"] = getattr(self, "_ai_return_video_path", "")
            self._select_real_project(project)
        if self.document and self.ai_session and self.ai_session.pending:
            self._set_ai_open(True)
            self._select_ai(self.ai_session.pending[0].suggestion_id)
        self._ai_return_count = 0
        self.ai_return.hide()
        self._render_ai()

    def _start_ai_check(self):
        if self._ai_running:
            self._ai_cancel.set()
            self.ai_note.setText("正在取消检查…")
            return
        if not self.document or not self.project:
            return
        self._commit_editor()
        if self.ai_session:
            choice = QMessageBox.question(self, "重新检查字幕",
                                          "重新检查会再次调用 AI 并消耗额度，确定继续吗？")
            if choice != QMessageBox.StandardButton.Yes:
                return
        snapshot = copy.copy(self.document)
        snapshot.entries = list(self.document.entries)
        project = self.project
        force = self.ai_session is not None
        self._ai_running = True
        self._ai_cancel = threading.Event()
        cancellation = self._ai_cancel
        self._ai_generation += 1
        generation = self._ai_generation
        self.app_status.setText(f"{project.title} · AI 检查中")
        self.ai_note.setText("后台检查中，可切换页面。")
        self._render_ai()
        def action():
            def progress(_message, _step, _total):
                if cancellation.is_set():
                    raise RuntimeError("检查已取消")
                self.ai_progress.emit(generation, project.title, _step, _total)
            result = self.ai_service.check(snapshot, project.title, force=force,
                                          progress_callback=progress)
            if cancellation.is_set():
                raise RuntimeError("检查已取消")
            self.ai_service.save(snapshot, project.title, result)
            return result
        self._run(action, lambda result, error: self._ai_finished(
            generation, project, snapshot, result, error))

    def _show_ai_progress(self, generation, project_title, step, total):
        if generation == self._ai_generation and self._ai_running:
            self.app_status.setText(f"{project_title} · AI 检查中"
                                    + (f" ({step}/{total} 批)" if total > 0 else ""))

    def _ai_finished(self, generation, project, snapshot, result, error):
        if generation != self._ai_generation:
            return
        self._ai_running = False
        if error:
            message = str(error)
            cancelled = "检查已取消" in message
            self.app_status.setText(f"{project.title} · {'检查已取消' if cancelled else 'AI 检查失败'}")
            self.ai_note.setText("检查已取消，可稍后主动重试。" if cancelled else
                                 "未配置 AI，请在设置页配置后主动重试。" if "未配置" in message else
                                 f"检查失败：{message}")
        else:
            self._show_transient_status("✓ AI 检查完成")
            self.ai_note.setText("检查完成。建议需逐条确认。")
            if self.document and self.document.video.path == snapshot.video.path and document_hash(self.document.entries) == result.content_hash:
                self.ai_session = result
                self._ai_selected = None
                self._render_ai()
                if result.pending:
                    self._set_ai_open(True)
                    if self.pages.currentIndex() != 0:
                        self._ai_return_project = project.id
                        self._ai_return_video_path = snapshot.video.path
                        self._ai_return_count = len(result.pending)
                        self.ai_return.setText(f"查看 {project.title} 的 {len(result.pending)} 条建议")
                        self.ai_return.show()
            elif self.document and self.document.video.path == snapshot.video.path:
                self.app_status.setText(f"{project.title} · 字幕已变化，建议未应用")
                self.ai_note.setText("检查期间字幕已变化，请主动重新检查。")
            elif result.pending:
                self._ai_return_project = project.id
                self._ai_return_video_path = snapshot.video.path
                self._ai_return_count = len(result.pending)
                self.ai_return.setText(f"查看 {project.title} 的 {len(result.pending)} 条建议")
                self.ai_return.show()
        self._render_ai()
