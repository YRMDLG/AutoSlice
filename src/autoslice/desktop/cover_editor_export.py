"""预览渲染、导出、批量出图、导出记录与“下一个”。

混入 CoverEditorWidget；只用 self 上的状态，不单独实例化。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import (
    QPixmap,
)

from .cover_batch_dialog import BatchTarget, CoverBatchDialog
from .cover_export_dialog import CoverExportDialog


class CoverExportMixin:
    def _render_preview(self):
        if self.video is None or not self.draft.image_path:
            return
        if self._busy:
            self._preview_dirty = True
            return
        self._preview_dirty = False
        self._busy = True
        # 画布本身就是所见即所得的预览；后台渲染静默进行，不打扰编辑。
        self._preview_request_generation += 1
        request_generation = self._preview_request_generation
        canvas_key = self._canvas_key
        document = self.document
        # CoverDocument 是预览的唯一输入；只有兼容旧调用方的无文档路径
        # 才需要读取控件草稿。
        draft = self.draft if document is not None else self._read_draft()
        self.draft = draft
        video = self.video
        self._run(
            lambda: self.service.render_preview_document(video, document, canvas_key=canvas_key) if document is not None else self.service.render_preview(video, draft, canvas_key=canvas_key),
            lambda result, error: self._preview_ready(
                request_generation, canvas_key, result, error
            ),
        )

    def _preview_ready(self, request_generation: int, canvas_key: str, result, error):
        if (
            request_generation != self._preview_request_generation
            or canvas_key != self._canvas_key
        ):
            return
        self._busy = False
        if error:
            self.canvas.setText("预览失败")
            self._set_notice(f"封面预览失败：{error}", "error")
            self.export_button.setEnabled(False)
            return
        self._preview_path = Path(result)
        self._set_preview(self._preview_path)
        self.export_button.setEnabled(True)
        self.export_both_button.setEnabled(True)
        self._set_notice("")
        if self._preview_dirty:
            self._preview_timer.start()
        elif self._pending_export:
            self._pending_export = False
            self._start_export()

    def _set_preview(self, path: Path):
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.canvas.setText("预览图片无法读取")
            return
        self.canvas.set_preview(pixmap)
        if self.draft.image_path and self.draft.image_path != self._background_source:
            # 底图只在换帧时解码一次，改字不再重复读大图。
            source_pixmap = QPixmap(self.draft.image_path)
            if not source_pixmap.isNull():
                self.canvas.set_background_pixmap(source_pixmap)
                self._background_source = self.draft.image_path
        if self.document is not None:
            self.canvas.set_document(self.document, self._canvas_key)
        self._update_title_rect()

    def _export(self):
        if self.project is None or self.video is None:
            return
        self._save_draft()
        self.export_button.setEnabled(False)
        self._pending_export = True
        self._preview_timer.stop()
        # 强制把当前草稿重新渲染一次；导出从这次预览完成回调启动，确保
        # 导出的 JPG 与画布最后显示的构图使用同一份状态。
        self._render_preview()

    def _export_both(self):
        if self.project is None or self.video is None or self.document is None:
            return
        self._save_draft()
        self.export_button.setEnabled(False)
        self.export_both_button.setEnabled(False)
        document = self.document
        project, video = self.project, self.video
        self._set_notice("正在分别导出 4:3 与 16:9…", "info")
        self.status_changed.emit("正在分别导出 4:3 与 16:9…")
        self._run(
            lambda: self.service.export_both(project, video, document),
            self._export_both_ready,
        )

    def _export_both_ready(self, result, error):
        self.export_button.setEnabled(self._preview_path is not None)
        self.export_both_button.setEnabled(self._preview_path is not None)
        if error:
            message = f"双比例导出失败：{error}"
            self._set_notice(message, "error")
            self.status_changed.emit(message)
            return
        names = "、".join(Path(item).name for item in result)
        self.export_summary.setText(f"已导出 4:3 + 16:9 · 项目目录 · {names}")
        self.export_summary.setToolTip("\n".join(str(Path(item)) for item in result))
        self._set_notice("")
        self.status_changed.emit(f"双比例封面已导出：{names}")

    def _start_export(self):
        if self.project is None or self.video is None:
            self._pending_export = False
            return
        draft = self.draft if self.document is not None else self._read_draft()
        self._set_notice(f"正在导出 {self._canvas_label()} 主封面…", "info")
        self.status_changed.emit(f"正在导出 {self._canvas_label()} 主封面…")
        canvas_key = self._canvas_key
        self._run(
            lambda: self.service.export_document(self.project, self.video, self.document, canvas_key=canvas_key) if self.document is not None else self.service.export(self.project, self.video, draft, canvas_key=canvas_key),
            self._export_ready,
        )

    def _export_ready(self, result, error):
        self.export_button.setEnabled(self._preview_path is not None)
        self.export_both_button.setEnabled(self._preview_path is not None)
        if error:
            message = f"封面导出失败：{error}"
            self._set_notice(message, "error")
            self.status_changed.emit(message)
            return
        output_name = Path(result).name
        size = "1440×1080" if self._canvas_key == "4x3" else "1920×1080"
        output_dir = Path(self.project.directory) if self.project is not None else Path(result).parent
        self.export_summary.setText(
            f"已导出 · {self._canvas_label()} · {size} · 项目目录 · {output_name}"
        )
        self.export_summary.setToolTip(str(output_dir / output_name))
        message = f"封面已导出：{output_name}（{self._canvas_label()} · {size}）"
        self._set_notice("")
        self.status_changed.emit(message)

    def set_project_list(self, projects) -> None:
        """主窗口扫描后的投稿项目，供批量出图使用。"""

        self._batch_projects = tuple(projects or ())
        self.batch_button.setEnabled(any(project.videos for project in self._batch_projects))

    def _open_batch(self):
        self.flush_draft()
        targets = tuple(
            BatchTarget(
                project, video,
                exported=bool(self.service.existing_exports(project, video)),
                has_draft=self.service.has_draft(project, video),
            )
            for project in self._batch_projects
            for video in project.videos
        )
        if not targets:
            self.status_changed.emit("投稿目录里没有可出图的视频")
            return
        dialog = CoverBatchDialog(targets, self.service.auto_cover, self._run, self)
        dialog.finished_batch.connect(
            lambda done, failed: self.status_changed.emit(f"批量出图完成：成功 {done} 个，失败 {failed} 个")
        )
        dialog.exec()

    def _open_export_history(self):
        CoverExportDialog(self.service.export_history(), self).exec()

    def _request_next_video(self):
        self.flush_draft()
        self.next_video_requested.emit()
