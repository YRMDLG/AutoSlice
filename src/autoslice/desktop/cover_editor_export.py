"""导出、批量出图、导出记录与“下一个”。

混入 CoverEditorWidget；只用 self 上的状态，不单独实例化。
"""

from __future__ import annotations

from pathlib import Path

from .cover_batch_dialog import BatchTarget, CoverBatchDialog
from .cover_export_dialog import CoverExportDialog


class CoverExportMixin:
    def _export(self):
        if self.project is None or self.video is None:
            return
        self._save_draft()
        self.export_button.setEnabled(False)
        self._start_export()

    def _export_both(self):
        if self.project is None or self.video is None or self.document is None:
            return
        self._save_draft()
        self.export_button.setEnabled(False)
        self.export_both_button.setEnabled(False)
        document = self.document
        project, video = self.project, self.video
        work = self._work_meta()
        self._set_notice("正在分别导出 4:3 与 16:9…", "info")
        self.status_changed.emit("正在分别导出 4:3 与 16:9…")
        self._run(
            lambda: self.service.export_both(project, video, document, work=work),
            self._export_both_ready,
        )

    def _export_both_ready(self, result, error):
        self.export_button.setEnabled(self._background_source is not None)
        self.export_both_button.setEnabled(self._background_source is not None)
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

    def _work_meta(self) -> dict:
        """给作品库的信息：套用过哪个快速方案、之后又改了几步、最初给的基础文案。"""

        basic = self._copy_variants[0] if self._copy_variants else None
        return {
            "scheme": self._applied_scheme,
            "edits_after_scheme": self._edits_after_scheme if self._applied_scheme else 0,
            "basic_copy": (basic.context, basic.headline) if basic is not None else ("", ""),
        }

    def _start_export(self):
        if self.project is None or self.video is None or self.document is None:
            return
        self._set_notice(f"正在导出 {self._canvas_label()} 主封面…", "info")
        self.status_changed.emit(f"正在导出 {self._canvas_label()} 主封面…")
        canvas_key = self._canvas_key
        work = self._work_meta()
        self._run(
            lambda: self.service.export_document(self.project, self.video, self.document, canvas_key=canvas_key, work=work),
            self._export_ready,
        )

    def _export_ready(self, result, error):
        self.export_button.setEnabled(self._background_source is not None)
        self.export_both_button.setEnabled(self._background_source is not None)
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
