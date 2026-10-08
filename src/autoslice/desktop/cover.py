"""AutoCover 桌面编辑器。

桌面层只维护交互状态和后台任务；媒体取帧、草稿持久化以及 Pillow 渲染
继续由 ``cover_service`` 负责。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import (
    QKeySequence,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QWidget,
)

from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.projects import ProjectVideo, SubmissionProject

from .cover_autolayout import CoverScheme
from .cover_draft import CoverDraft
from .cover_editor_ai import CoverAIMixin
from .cover_editor_export import CoverExportMixin
from .cover_editor_frames import CoverFramesMixin
from .cover_editor_objects import CoverObjectsMixin
from .cover_editor_schemes import CoverSchemesMixin
from .cover_editor_text import CoverTextMixin
from .cover_editor_ui import CoverLayoutMixin
from .cover_frames import CoverFrame
from .cover_history import CoverHistory
from .cover_model import (
    BackgroundObject,
    CoverDocument,
    ImageObject,
    ShapeObject,
    StickerObject,
    TextObject,
    set_object_visible,
    set_profile_override,
)
from .cover_service import CoverService
from .jobs import BackgroundJob


class CoverEditorWidget(
    CoverLayoutMixin,
    CoverFramesMixin,
    CoverObjectsMixin,
    CoverTextMixin,
    CoverSchemesMixin,
    CoverExportMixin,
    CoverAIMixin,
    QWidget,
):
    """真正可鼠标操作的 AutoCover 编辑器。"""

    status_changed = Signal(str)
    # 封面页“下一个”：由主窗口切到下一个视频/项目。
    next_video_requested = Signal()

    def __init__(self, storage: DesktopStorage, parent=None):
        super().__init__(parent)
        self.service = CoverService(storage)
        self.project: SubmissionProject | None = None
        self.video: ProjectVideo | None = None
        self.draft = CoverDraft("")
        self.document: CoverDocument | None = None
        self._background_source: str | None = None
        self._context_generation = 0
        self._current_playhead = 0.0
        self._frame_extract_pending = False
        self._frame_request_generation = 0
        self._nearby_request_generation = 0
        self._copy_variants = ()
        self._copy_variant_index = -1
        self._selected_text_id: str | None = None
        self._canvas_key = "4x3"
        self._jobs: set[BackgroundJob] = set()
        self.history = CoverHistory()
        self._frame_locked = False
        self._selected_frame_timestamp: float | None = None
        self._wider_frames: tuple[tuple[Path, float], ...] = ()
        self._nearby_pending: set[float] = set()
        self._nearby_results: list[CoverFrame] = []
        self._nearby_error = None
        self._schemes: tuple[CoverScheme, ...] = ()
        self._batch_projects: tuple[SubmissionProject, ...] = ()
        self._inline_target: str | None = None
        self._inline_original = ""
        self._video_duration = 0.0
        # 视频重新导出过：从这个时间重新取帧，并沿用原排版（不再自动排版）。
        self._reuse_layout_timestamp: float | None = None
        self._scheme_batch = 0
        self._scheme_generation = 0
        # 作品库用：最近套用的快速方案，以及套用后又改了几步。
        self._applied_scheme = ""
        self._edits_after_scheme = 0
        # AI 请求的代际（切项目即作废迟到结果）与“再点一次换一批”的轮次。
        self._ai_generation = 0
        self._ai_round = 0
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(500)
        self._draft_timer.timeout.connect(self._save_draft)
        self._build()
        # 封面页自己的撤销/重做；文案框获得焦点时由输入框处理文字撤销。
        for sequence, action in (
            (QKeySequence.StandardKey.Undo, self._undo),
            (QKeySequence.StandardKey.Redo, self._redo),
            (QKeySequence("Ctrl+Y"), self._redo),
        ):
            shortcut = QShortcut(sequence, self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(action)

    def _canvas_selection_changed(self, title_selected: bool):
        """根据画布选择切换右侧上下文面板。"""

        title_selected = bool(title_selected)
        if not title_selected:
            self._update_canvas_hint()
        if self.document is not None:
            if title_selected:
                text_ids = [item.id for item in self.document.objects if isinstance(item, TextObject)]
                selected = self._selected_text_id if self._selected_text_id in text_ids else next(
                    (item.id for item in self.document.objects if isinstance(item, TextObject) and item.copy_role == "B"),
                    text_ids[0] if text_ids else None,
                )
                self._selected_text_id = selected
            else:
                selected = next((item.id for item in self.document.objects if isinstance(item, BackgroundObject)), None)
            self.document = replace(self.document, selected_object_id=selected)
        if hasattr(self, "canvas"):
            self.canvas.set_selected_object(
                self._selected_text_id if title_selected and self._selected_text_id else "background"
            )
        # 切换右侧面板
        if title_selected:
            self.panel_title.setText("封面文案")
            self.panel_stack.setCurrentWidget(self.copy_controls)
            self._sync_selected_text_controls()
        else:
            self.panel_title.setText("底图取景")
            self.panel_stack.setCurrentWidget(self.bg_controls)
        self._sync_overlay_controls()

    def _canvas_object_selected(self, object_id: str):
        if self.document is None:
            return
        selected = next((item for item in self.document.objects if item.id == object_id), None)
        if isinstance(selected, TextObject):
            self._selected_text_id = selected.id
            self.document = replace(self.document, selected_object_id=selected.id)
            self._canvas_selection_changed(True)
        elif isinstance(selected, BackgroundObject):
            self.document = replace(self.document, selected_object_id=selected.id)
            self._canvas_selection_changed(False)
        elif isinstance(selected, (ImageObject, StickerObject, ShapeObject)):
            self.document = replace(self.document, selected_object_id=selected.id)
            self.panel_title.setText("素材")
            self.panel_stack.setCurrentWidget(self.asset_controls)
            self.canvas.set_selected_object(selected.id)
            self._sync_overlay_controls()

    def _commit_document_change(self, before: CoverDocument | None = None) -> None:
        """原子提交文档，并让所有界面和异步任务转向同一份状态。"""

        if self.document is None:
            return
        self.draft = CoverDraft.from_document(self.document)
        # CoverDocument 是唯一主状态。候选、撤销和手势提交后先把控件重读
        # 为新文档，再启动任何延迟任务，避免旧控件反向覆盖 profile override。
        self._apply_draft()
        self._sync_overlay_controls()
        self._record_history()
        self._draft_timer.start()

    def _record_history(self):
        """记一条撤销历史，撤销/重做按钮跟着可用状态。"""

        self.history.commit(self.document)
        self._edits_after_scheme += 1
        self.undo_button.setEnabled(self.history.can_undo)
        self.redo_button.setEnabled(self.history.can_redo)

    def _undo(self):
        self._restore_history(self.history.undo())

    def _redo(self):
        self._restore_history(self.history.redo())

    def _restore_history(self, document: CoverDocument | None):
        """撤销/重做：整份文档快照回到界面，控件和后台任务一起跟上。"""

        if document is None:
            return
        self.document = document
        self.draft = CoverDraft.from_document(self.document)
        self._apply_draft()
        self._sync_overlay_controls()
        self.undo_button.setEnabled(self.history.can_undo)
        self.redo_button.setEnabled(self.history.can_redo)
        self._draft_timer.start()

    def _canvas_object_changed(self, item, profile_key: str):
        """接收手势提交后的对象变换，保持 CoverDocument 为唯一主状态。"""

        if self.document is None or not hasattr(item, "id"):
            return
        if profile_key not in self.document.profiles:
            return
        self.document = replace(
            set_profile_override(self.document, profile_key, item),
            active_profile=profile_key,
            selected_object_id=item.id,
        )
        if not getattr(item, "visible", True):
            if isinstance(item, TextObject):
                self.document = set_object_visible(self.document, item.id, False)
            elif isinstance(item, (ImageObject, StickerObject, ShapeObject)):
                self.document = replace(
                    self.document,
                    objects=tuple(obj for obj in self.document.objects if obj.id != item.id),
                    selected_object_id=None,
                )
        self.draft = CoverDraft.from_document(self.document)
        self._record_history()
        self.canvas.set_document(self.document, self._canvas_key)
        self._refresh_hidden_button()
        if isinstance(item, TextObject):
            # Canvas 手势先发 object_changed、再发位置兼容信号；先同步字号，
            # 避免后续旧兼容入口用右侧面板的旧值覆盖画布刚调整的字号。
            self._show_text_metrics(item)
            self._refresh_canvas()
        elif isinstance(item, (ImageObject, StickerObject, ShapeObject)):
            self._sync_overlay_controls()
        self._draft_timer.start()

    def _canvas_object_preview_changed(self, item, _profile_key: str):
        """只更新轻量控件显示；拖动帧不得写文档、保存或渲染。"""

        if isinstance(item, TextObject):
            self._show_text_metrics(item)
        elif isinstance(item, BackgroundObject):
            self.zoom_spin.blockSignals(True)
            try:
                self.zoom_spin.setValue(float(item.scale))
            finally:
                self.zoom_spin.blockSignals(False)

    def _set_canvas_key(self, canvas_key: str):
        if canvas_key not in {"4x3", "16x9"} or canvas_key == self._canvas_key:
            return
        self._finish_inline_edit(True)
        self.sync_ratio_button.setText("同步到 4:3" if canvas_key == "16x9" else "同步到 16:9")
        if self.document is not None:
            self.document = replace(self.document, active_profile=canvas_key)
            self.draft = CoverDraft.from_document(self.document)
            self._draft_timer.start()
        self._canvas_key = canvas_key
        for key, button in self.canvas_ratio_buttons.items():
            button.setChecked(key == canvas_key)
        self._update_canvas_hint()
        self._update_export_summary()
        if self.document is not None:
            self._apply_draft()
            self._sync_overlay_controls()
        if self._background_source is None:
            self.canvas.setText(f"加载底图后在这里预览 {self._canvas_label()} 画布")

    def set_context(self, project: SubmissionProject | None, video: ProjectVideo | None):
        """切换项目/视频：没有就清空页面；有则载入草稿，再排队取帧或预览。"""

        self._finish_inline_edit(True)
        if project is None or video is None:
            self._clear_context()
            return
        if self.project is not None and self.project.id == project.id and self.video is not None and self.video.path == video.path:
            return
        if self.project is not None and self.video is not None:
            self._save_draft()
        self._enter_context(project, video)

    def _reset_work_tracking(self):
        self._applied_scheme = ""
        self._edits_after_scheme = 0

    def _cancel_background_work(self):
        """在途的取帧、附近帧回调全部作废；迟到结果按代际丢弃。"""

        self._frame_extract_pending = False
        self._frame_request_generation += 1
        self._nearby_request_generation += 1
        self._ai_generation += 1
        self._ai_round = 0

    def _set_editing_enabled(self, enabled: bool):
        for button in (
            self.asset_menu_button, self.shape_menu_button, self.add_text_button,
            self.next_button, self.sync_ratio_button,
        ):
            button.setEnabled(enabled)
        self.sync_ratio_button.setText("同步到 16:9")
        # 视频时长在后台读到后才启用拖动选帧。
        self.frame_slider.setEnabled(False)
        self.duration_label.setText("")

    def _clear_context(self):
        """没有项目：清空画布和状态，右侧面板收起，画布占满。"""

        self._context_generation += 1
        self.project = None
        self.video = None
        self.document = None
        self.history.reset(None)
        self._reset_work_tracking()
        self._cancel_background_work()
        self._copy_variants = ()
        self._copy_variant_index = -1
        self._reuse_layout_timestamp = None
        self._selected_frame_timestamp = None
        self.project_label.setText("请选择项目")
        self.video_label.setText("请选择视频")
        self.draft_status.setText("未加载草稿")
        self._refresh_nearby_frame_strip(0.0)
        self._update_export_summary()
        self._update_canvas_hint()
        self._show_background(None)
        self.canvas.setText("请先在字幕页选择投稿项目和视频")
        self._set_notice("")
        self._set_editing_enabled(False)
        for button in (
            self.extract_button, self.current_frame_button, self.export_button,
            self.export_both_button, self.undo_button, self.redo_button,
        ):
            button.setEnabled(False)
        self.hidden_button.setVisible(False)
        self.frame_lock_button.setChecked(False)
        self._clear_schemes()
        self._hide_panel()
        self.panel_toggle.setEnabled(False)

    def _enter_context(self, project: SubmissionProject, video: ProjectVideo):
        """进入项目：载入（或新建）草稿，同步控件，然后预览或取帧。"""

        self._context_generation += 1
        self.project, self.video = project, video
        self.project_label.setText(project.title if len(project.title) <= 32 else project.title[:32] + "…")
        self.project_label.setToolTip(project.title)
        self.video_label.setText(video.name)
        self._set_editing_enabled(True)
        self._set_notice("")
        self._update_export_summary()
        video_path = Path(video.path)
        video_available = video_path.is_file()
        self.extract_button.setEnabled(video_available)
        self.current_frame_button.setEnabled(video_available)
        self.extract_button.setToolTip("从指定时间取帧" if video_available else f"当前视频不存在：{video_path}")
        self._show_panel()
        self.panel_toggle.setEnabled(True)
        self.panel_toggle.setChecked(False)
        self._cancel_background_work()
        self._canvas_key = "4x3"
        for key, button in self.canvas_ratio_buttons.items():
            button.setChecked(key == self._canvas_key)
        self._update_canvas_hint()
        self._copy_variants = self.service.basic_copy_variants(
            project.title,
            subtitle_context=self.service.subtitle_context(video, self._current_playhead),
        )
        self.document, read = self.service.load_document(project, video)
        self._reuse_layout_timestamp = (
            float(self.document.source.selected_timestamp) if read.status == "source_changed" else None
        )
        self.history.reset(self.document)
        self._reset_work_tracking()
        self._frame_locked = bool(self.document.source.frame_locked)
        self.frame_lock_button.blockSignals(True)
        self.frame_lock_button.setChecked(self._frame_locked)
        self.frame_lock_button.setText("已锁帧" if self._frame_locked else "锁帧")
        self.frame_lock_button.blockSignals(False)
        self.draft = CoverDraft.from_document(self.document)
        self._selected_frame_timestamp = (
            float(self.draft.selected_timestamp) if self.draft.image_path else max(0.0, self._current_playhead)
        )
        self._copy_variant_index = next(
            (
                index for index, candidate in enumerate(self._copy_variants)
                if candidate.text == self.draft.title or candidate.headline == self.draft.title
            ),
            -1,
        )
        self._apply_draft()
        # 有项目时默认显示文字面板
        self._canvas_selection_changed(True)
        self._show_draft_status(read.status, video_path if not video_available else None)
        self._refresh_nearby_frame_strip(
            self.draft.selected_timestamp if self.draft.image_path else self._current_playhead
        )
        self._scheme_batch = 0
        self._load_video_duration()
        if self.draft.image_path:
            self._queue_nearby_thumbnails(self.draft.selected_timestamp)
            self._refresh_schemes()
        else:
            self._clear_schemes()
            self.canvas.setText("正在准备当前帧…" if self.isVisible() else "进入封面页后自动加载当前帧")
            if self.isVisible() and not self._frame_extract_pending:
                QTimer.singleShot(0, self._use_current_frame)

    def _show_draft_status(self, status: str, missing_video: Path | None):
        """把草稿读取结果说成人话；视频不可用优先提示。"""

        if missing_video is not None:
            self.draft_status.setText("视频不可用")
            self.draft_status.setToolTip(str(missing_video))
            self._set_notice(f"当前视频不可用：{missing_video}", "error")
        elif status == "ready":
            self.draft_status.setText("已恢复草稿")
            self.draft_status.setToolTip("")
        elif status == "missing":
            self.draft_status.setText("新草稿 · 自动保存")
            self.draft_status.setToolTip("")
        elif status == "source_changed":
            self.draft_status.setText("视频已更新 · 沿用原排版")
            self.draft_status.setToolTip("视频文件改过（比如重新导出）：文字、样式和素材沿用原来的，从新视频同一时刻重新取帧")
            self.status_changed.emit(
                f"视频文件更新过：已沿用原来的文字、样式和素材，从新视频的 {self._reuse_layout_timestamp:.2f} 秒重新取帧"
            )
        else:
            reason = {"invalid": "草稿文件损坏", "incompatible": "草稿版本过旧", "source_missing": "找不到原视频"}.get(status, status)
            self.draft_status.setText(f"{reason} · 已新建")
            self._set_notice(f"{reason}，已按新封面开始；旧草稿文件仍保留", "warning")

    def _apply_draft(self):
        widgets = (self.title_edit, self.font_spin, self.zoom_spin)
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self.title_edit.setPlainText(self.draft.title)
            self.font_spin.setValue(self.draft.font_size)
            self.zoom_spin.setValue(self.draft.background_scale)
            self.timestamp_edit.setValue(self.draft.selected_timestamp)
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        self._show_background(self.draft.image_path or None)
        self.canvas.set_document(self.document, self._canvas_key)
        self._refresh_hidden_button()
        if self.document is not None:
            selected = next(
                (item for item in self.document.objects if item.id == self.document.selected_object_id),
                None,
            )
            if isinstance(selected, TextObject):
                self._selected_text_id = selected.id
                self._sync_selected_text_controls()
            elif self._selected_text_id is None:
                text = next(
                    (item for item in self.document.objects if isinstance(item, TextObject) and item.copy_role == "B"),
                    None,
                ) or next((item for item in self.document.objects if isinstance(item, TextObject)), None)
                self._selected_text_id = text.id if text else None
        self._refresh_canvas()

    def _show_background(self, path: str | None):
        """底图路径变了才重新解码（换帧、导入、撤销、换项目）；改字不重复读大图。有底图才能导出和用 AI。"""

        if path != self._background_source:
            pixmap = QPixmap(path) if path else QPixmap()
            self.canvas.set_background_pixmap(pixmap)
            self._background_source = path if not pixmap.isNull() else None
            if path and pixmap.isNull():
                self.canvas.setText("底图无法读取")
                self._set_notice(f"底图无法读取：{path}", "error")
        ready = self._background_source is not None
        for button in (self.export_button, self.export_both_button, self.ai_scheme_button, self.ai_critique_button):
            button.setEnabled(ready)

    def _read_draft(self) -> CoverDraft:
        self._store_text_controls()
        self._store_background_controls()
        if self.document is not None:
            self.draft = CoverDraft.from_document(self.document)
        return self.draft

    def _draft_changed(self):
        if self.project is None or self.video is None:
            return
        before = self.document
        self._store_text_controls()
        self._store_background_controls()
        self.draft = CoverDraft.from_document(self.document) if self.document else self._read_draft()
        if self.document is not None and before != self.document:
            self._record_history()
        self._refresh_canvas()
        self._refresh_hidden_button()
        self._draft_timer.start()

    def _gesture_finished(self):
        # object_changed 已在 release 提交并启动防抖保存；释放时不同步写盘，
        # 连续拖动只在停手后写一次。
        self._draft_timer.start()

    def flush_draft(self):
        """关窗或离开前补写尚在防抖中的草稿。"""

        if self._draft_timer.isActive():
            self._draft_timer.stop()
            self._save_draft()

    def _save_draft(self):
        if self.project is None or self.video is None:
            return
        try:
            if self.document is None:
                return
            # 文档已经在控件信号/画布手势提交时写入；延迟保存只能消费
            # 这份快照，不能再次从可能过期的控件反向重建它。
            self.draft = CoverDraft.from_document(self.document)
            self.service.save_document(self.project, self.video, self.document)
            self.draft_status.setText("已保存")
        except (OSError, ValueError) as exc:
            message = f"封面草稿保存失败：{exc}"
            self._set_notice(message, "error")
            self.status_changed.emit(message)

    def _run(self, action, callback):
        generation = self._context_generation
        job = BackgroundJob(action)
        self._jobs.add(job)

        def done(result, error):
            try:
                if generation == self._context_generation:
                    callback(result, error)
            finally:
                self._jobs.discard(job)

        job.signals.finished.connect(done)
        QThreadPool.globalInstance().start(job)

    def showEvent(self, event):
        super().showEvent(event)
        if self.video is not None and self.draft.image_path:
            QTimer.singleShot(0, lambda: self._queue_nearby_thumbnails(self.draft.selected_timestamp))

    def _refresh_canvas(self):
        """文档变了：画布按当前比例重读一遍。"""

        if self.document is not None:
            self.canvas.set_document(self.document, self._canvas_key)



