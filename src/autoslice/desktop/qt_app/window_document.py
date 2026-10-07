"""字幕文档：编辑记录、草稿、保存、删除、撤销/重做与渲染导出。

混入 DesktopWindow，排在 PreviewWindow 之前；状态都在窗口上，不单独实例化。
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from autoslice.desktop.ai_review import document_hash


class SubtitleDocumentMixin:
    def _edited(self):
        if self.ai_session and self.document and self.project:
            self.ai_session.content_hash = document_hash(self.document.entries)
            self._save_ai_session()
            self._show_ai_detail()
        self._update_status()
        if self.document and not self.document.dirty:
            self._draft_timer.stop()
            try:
                self._remove_draft(self.document)
            except OSError as exc:
                self.app_status.setText(f"草稿清理失败：{exc}")
        else:
            self._draft_timer.start()
        self._preview_timer.start()

    def _write_draft(self):
        if not self.document or not self.project:
            return
        if not self.document.dirty:
            self._remove_draft(self.document)
            return
        try:
            self.storage.save_draft(
                "subtitle", self.project.directory, self.document.source_path,
                self.document.draft_payload(self.selected_index),
                dependencies=[self.document.corrected_path], baseline=self._baseline,
            )
            self._save_session()
        except OSError as exc:
            self.app_status.setText(f"草稿保存失败：{exc}")

    def _remove_draft(self, document):
        self.storage.draft_path("subtitle", self.project.directory,
                                document.source_path).unlink(missing_ok=True)

    def _resolve_unsaved(self):
        if self._saving or self._loading:
            return False
        self._commit_editor()
        if not self.document or not self.document.dirty:
            return True
        self._draft_timer.stop()
        self._write_draft()
        dialog = QMessageBox(self)
        dialog.setWindowTitle("未保存的字幕")
        dialog.setText("当前字幕有未正式保存的修改。可先返回点击“保存校对字幕”，或选择离开时如何处理草稿。")
        keep = dialog.addButton("保留草稿并离开", QMessageBox.ButtonRole.DestructiveRole)
        discard = dialog.addButton("放弃修改并离开", QMessageBox.ButtonRole.DestructiveRole)
        dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked is discard:
            self._remove_draft(self.document)
            return True
        return clicked is keep

    def save(self):
        if self._saving or not self.document:
            return
        self._commit_editor()
        self._draft_timer.stop()
        self._write_draft()
        self._saving = True
        document = self.document
        self._update_status()
        self.table.setEnabled(False)
        self.timeline.setEnabled(False)
        self._run(document.save, lambda result, error: self._saved(document, result, error))

    def _saved(self, document, result, error):
        self._saving = False
        self.table.setEnabled(True)
        self.timeline.setEnabled(True)
        if error:
            self._save_then_render = False
            QMessageBox.warning(self, "保存失败", str(error))
        elif self.document is document:
            self._remove_draft(document)
            self._baseline = self.storage.capture_baseline(
                document.source_path, dependencies=[document.corrected_path]
            )
            self._show_transient_status(f"✓ 已保存 · {Path(result).name}")
        self._update_status()
        if not error and self._save_then_render and self.document is document:
            self._save_then_render = False
            QTimer.singleShot(0, self._start_render)
        else:
            self._save_then_render = False
        if not error and self.document is document:
            self.save_button.setText("✓ 已保存")
            QTimer.singleShot(1800, lambda: self.save_button.setText("保存字幕")
                              if self.save_button.text() == "✓ 已保存" else None)

    def _delete(self):
        if self.document is None or self._saving:
            return
        self._commit_editor()
        selected = set(self.selection.selected)
        if not selected and self.selected_index is not None:
            selected = {self.selected_index}
        if not selected:
            return
        try:
            self.document.delete_many(selected)
        except ValueError as exc:
            QMessageBox.warning(self, "无法删除", str(exc))
            return
        self.selection.clear()
        self.selected_index = None
        self.model.set_document(self.document)
        self.timeline.select_cue(None, center=False)
        self.timeline.update()
        self._edited()

    def _undo(self):
        if self._saving:
            return
        self._commit_editor()
        before = document_hash(self.document.entries) if self.document else None
        if self.ai_session and self._ai_skip_undo and self._ai_skip_undo[-1][0] == before:
            _, suggestion_id = self._ai_skip_undo.pop()
            self._ai_skip_redo.append((before, suggestion_id))
            self.ai_session.mark(suggestion_id, "pending", self.document.entries)
            self._save_ai_session()
            self._render_ai()
            return
        if self.document and self.document.undo():
            after = document_hash(self.document.entries)
            if self.ai_session and self._ai_undo and self._ai_undo[-1][:2] == (after, before):
                transition = self._ai_undo.pop()
                self._ai_redo.append(transition)
                self.ai_session.mark(transition[2], "pending", self.document.entries)
            self.model.set_document(self.document)
            self.timeline.update()
            self._select_cue(self.selected_index)
            self._edited()
            self._render_ai()

    def _redo(self):
        if self._saving:
            return
        self._commit_editor()
        before = document_hash(self.document.entries) if self.document else None
        if self.ai_session and self._ai_skip_redo and self._ai_skip_redo[-1][0] == before:
            _, suggestion_id = self._ai_skip_redo.pop()
            self._ai_skip_undo.append((before, suggestion_id))
            self.ai_session.mark(suggestion_id, "skipped", self.document.entries)
            self._save_ai_session()
            self._render_ai()
            return
        if self.document and self.document.redo():
            after = document_hash(self.document.entries)
            if self.ai_session and self._ai_redo and self._ai_redo[-1][:2] == (before, after):
                transition = self._ai_redo.pop()
                self._ai_undo.append(transition)
                self.ai_session.mark(transition[2], "accepted", self.document.entries)
            self.model.set_document(self.document)
            self.timeline.update()
            self._select_cue(self.selected_index)
            self._edited()
            self._render_ai()

    def _start_render(self, _checked=False):
        if not self.document or not self.project or self._saving:
            return
        self._commit_editor()
        project = self.project
        if project.id in self._render_jobs:
            self.app_status.setText(f"{project.title} · 已在压制中")
            return
        document = self.document
        if document.dirty or not document.corrected_path.is_file():
            choice = QMessageBox.question(
                self, "保存并压制字幕",
                "压制只使用正式保存的校对字幕。是否先保存当前字幕，再开始后台压制？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if choice == QMessageBox.StandardButton.Yes:
                self._save_then_render = True
                self.save()
            return
        key = project.id
        cancellation = threading.Event()
        self._render_jobs[key] = {"title": project.title, "cancel": cancellation,
                                  "percent": 0, "percent_enabled": True}
        self._render_result = None
        self.render_location.hide()
        self.app_status.setText(f"{project.title} · 正在压制…")
        self.app_status.setToolTip("")
        self._update_status()
        video_path = document.video.path
        corrected_path = document.corrected_path
        digest = document.corrected_fingerprint

        def action():
            def progress(message, step, total):
                percent = step if total == 100 and step < 100 else None
                self.render_progress.emit(key, percent, message)
            return self.render_service.render(
                video_path, corrected_path, digest,
                progress_callback=progress, cancel_event=cancellation,
            )

        self._run(action, lambda result, error: self._render_finished(key, result, error))

    def _show_render_progress(self, key, percent, message):
        job = self._render_jobs.get(key)
        if job is None:
            return
        if "NVENC" in message:
            job["percent_enabled"] = False
        elif percent is not None and job["percent_enabled"]:
            job["percent"] = max(job["percent"], percent)
        suffix = f" {job['percent']}%" if percent is not None and job["percent_enabled"] else ""
        self.app_status.setText(f"{job['title']} · 正在压制{suffix}")

    def _render_finished(self, key, result, error):
        job = self._render_jobs.pop(key, None)
        if job is None:
            return
        if error:
            if job["cancel"].is_set():
                self.app_status.setText(f"{job['title']} · 压制已停止")
            else:
                detail = str(error)
                if isinstance(error, FileNotFoundError):
                    hint = "请检查 FFmpeg 和 ffprobe 是否可用"
                elif "字体" in detail:
                    hint = "请安装默认字幕字体后重试"
                elif isinstance(error, PermissionError):
                    hint = "请检查投稿目录的写入权限"
                else:
                    hint = "请检查视频与校对字幕后重试"
                self.app_status.setText(f"{job['title']} · 压制失败：{hint}")
                self.app_status.setToolTip(f"{detail}\n详细记录：{self.storage.logs / 'subtitle-render.log'}")
        else:
            self._render_result = Path(result["output_video_path"])
            self._show_transient_status(f"{job['title']} · ✓ 压制完成 · {self._render_result.name}")
            self.app_status.setToolTip(str(self._render_result))
            self.render_location.setToolTip(str(self._render_result))
            name = self._render_result.name
            self.render_location.setText(f"定位：{name[:16]}{'…' if len(name) > 16 else ''}")
            self.render_location.show()
        if self._render_jobs:
            active = next(iter(self._render_jobs.values()))
            self.app_status.setText(f"{active['title']} · 正在压制…")
        self._update_status()
        if self._close_after_render and not self._render_jobs:
            QTimer.singleShot(0, self.close)

    def _locate_render_output(self):
        path = self._render_result
        if path is None or not path.is_file():
            QMessageBox.warning(self, "输出文件不存在", "成片可能已被移动，请检查投稿目录。")
            return
        if os.name == "nt":
            subprocess.Popen(["explorer.exe", "/select,", str(path)])
        else:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
