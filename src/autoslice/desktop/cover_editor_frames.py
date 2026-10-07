"""取帧与底图：附近帧、全片挑选、拖动选帧、锁帧、导入底图与取景缩放。

混入 CoverEditorWidget；只用 self 上的状态，不单独实例化。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import (
    QIcon,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QMessageBox,
    QPushButton,
)

from autoslice_cover.document_layout import BACKGROUND_SCALE_MAX, BACKGROUND_SCALE_MIN

from .cover_draft import CoverDraft
from .cover_frames import CoverFrame, best_overview_frame, recommended_frame
from .cover_model import (
    AssetRef,
    BackgroundObject,
    object_for_profile,
)


class CoverFramesMixin:
    def _update_nearby_frame_selection(self, timestamp: float | None = None):
        if timestamp is not None:
            self._selected_frame_timestamp = max(0.0, float(timestamp))
        selected = self._selected_frame_timestamp
        distances = [
            abs(float(button.property("timestamp") or 0.0) - selected)
            if selected is not None else float("inf")
            for button in self.nearby_frame_buttons
        ]
        nearest = min(distances, default=float("inf"))
        nearest_index = distances.index(nearest) if nearest <= 0.06 else -1
        for index, button in enumerate(self.nearby_frame_buttons):
            button.setChecked(index == nearest_index)

    def _load_video_duration(self):
        video = self.video
        if video is None or not Path(video.path).is_file():
            self.frame_slider.setEnabled(False)
            return
        generation = self._context_generation
        self._run(
            lambda: self.service.video_duration(video),
            lambda result, error: self._video_duration_ready(generation, result, error),
        )

    def _video_duration_ready(self, generation: int, result, error):
        if generation != self._context_generation or error or not result:
            return
        self._video_duration = float(result)
        self.frame_slider.blockSignals(True)
        self.frame_slider.setRange(0, int(self._video_duration * 10))
        self.frame_slider.setPageStep(50)
        self.frame_slider.setValue(int((self._selected_frame_timestamp or 0.0) * 10))
        self.frame_slider.blockSignals(False)
        self.frame_slider.setEnabled(True)
        minutes, seconds = divmod(int(self._video_duration), 60)
        self.duration_label.setText(f"{minutes}:{seconds:02d}")

    def _sync_frame_slider(self, timestamp: float):
        self.frame_slider.blockSignals(True)
        self.frame_slider.setValue(int(max(0.0, timestamp) * 10))
        self.frame_slider.blockSignals(False)

    def _slider_frame(self):
        self._slider_timer.stop()
        if self.video is None or not self.frame_slider.isEnabled():
            return
        self._choose_nearby_frame(self.frame_slider.value() / 10)

    def _find_overview_frames(self):
        if self.video is None or not Path(self.video.path).is_file():
            return
        request_generation = self._nearby_request_generation + 1
        self._nearby_request_generation = request_generation
        video = self.video
        self.overview_button.setEnabled(False)
        self._set_notice("正在从全片按画质挑画面…", "info")
        self._run(
            lambda: self.service.overview_candidates(video, count=12),
            lambda result, error: self._wider_frames_ready(request_generation, result, error, scope="全片"),
        )

    @staticmethod
    def _nearby_offsets(center: float) -> tuple[float, ...]:
        """附近 7 帧的偏移；片头不足 1.2 秒时整体后移，避免重复的 0 秒帧。"""

        before = min(3, int(max(0.0, float(center or 0.0)) / 0.4 + 1e-6))
        return tuple(round((index - before) * 0.4, 2) for index in range(7))

    def _refresh_nearby_frame_strip(self, center: float):
        center = max(0.0, float(center or 0.0))
        self.frame_center_label.setText(f"当前 {center:.2f} 秒")
        self._sync_frame_slider(center)
        offsets = self._nearby_offsets(center)
        available = self.video is not None and Path(self.video.path).is_file()
        for button, offset in zip(self.nearby_frame_buttons, offsets):
            timestamp = max(0.0, center + offset)
            button.setProperty("timestamp", timestamp)
            button.setText(f"{timestamp:.2f}s")
            button.setToolTip(f"{timestamp:.2f} 秒")
            button.setProperty("recommended", False)
            button.setIcon(QIcon())
            button.setEnabled(available)
        self._update_nearby_frame_selection(
            self._selected_frame_timestamp if self._selected_frame_timestamp is not None else center
        )

    def _queue_nearby_thumbnails(self, center: float):
        # 页面未显示时不启动 FFmpeg；进入封面页后由 showEvent 补排队。
        if not self.isVisible() or self.video is None or not Path(self.video.path).is_file():
            return
        center = max(0.0, float(center or 0.0))
        offsets = self._nearby_offsets(center)
        self._nearby_request_generation += 1
        request_generation = self._nearby_request_generation
        video = self.video
        timestamps = sorted({round(max(0.0, center + offset), 3) for offset in offsets})
        self._nearby_pending = set(timestamps)
        self._nearby_results: list[CoverFrame] = []
        self._nearby_error = None
        # 每帧一个后台任务并行取帧，取到一张显示一张，不等整排完成。
        for timestamp in timestamps:
            self._run(
                lambda value=timestamp: self.service.extract_frame_candidate(video, value),
                lambda result, error, value=timestamp: self._nearby_frame_ready(
                    request_generation, value, result, error
                ),
            )

    def _nearby_frame_ready(self, request_generation: int, requested: float, result, error):
        if request_generation != self._nearby_request_generation:
            return
        self._nearby_pending.discard(requested)
        if error:
            self._nearby_error = error
        elif result is not None:
            self._nearby_results.append(result)
            button = min(
                self.nearby_frame_buttons,
                key=lambda item: abs(float(item.property("timestamp") or 0.0) - requested),
            )
            self._show_frame_on_button(button, result, recommended=False)
        if not self._nearby_pending:
            # 整排到齐后再比较画质，标出推荐帧。
            frames = tuple(sorted(self._nearby_results, key=lambda item: item.timestamp))
            self._nearby_frames_ready(request_generation, frames, None if frames else self._nearby_error)

    def _nearby_frames_ready(self, request_generation: int, result, error):
        if request_generation != self._nearby_request_generation:
            return
        if error:
            message = f"附近帧预览失败：{error}"
            self._set_notice(message, "warning")
            self.status_changed.emit(message)
            return
        if not result:
            self._set_notice("没有找到附近可用画面", "warning")
            return
        frames = tuple(result)
        best = recommended_frame(frames)
        for button in self.nearby_frame_buttons:
            target = float(button.property("timestamp") or 0.0)
            frame = min(frames, key=lambda item: abs(item.timestamp - target))
            self._show_frame_on_button(button, frame, recommended=frame is best)
        self._set_notice("")

    @staticmethod
    def _show_frame_on_button(button: QPushButton, frame: CoverFrame, *, recommended: bool) -> None:
        """缩略图、时间和画质说明；推荐只做标记，不替用户换帧。"""

        button.setProperty("timestamp", frame.timestamp)
        button.setText(f"{'★ ' if recommended else ''}{frame.timestamp:.2f}s")
        pixmap = QPixmap(str(frame.path))
        if not pixmap.isNull():
            button.setIcon(QIcon(pixmap))
        metrics = frame.metrics
        lines = [f"{frame.timestamp:.2f} 秒"]
        if metrics is not None:
            lines.append(f"清晰度 {metrics.sharpness:.0%} · 曝光 {metrics.exposure:.0%} · 对比度 {metrics.contrast:.0%}")
            if metrics.subtitle_risk >= 0.3:
                lines.append("画面中下部可能有字幕或文字条")
        if recommended:
            lines.append("推荐：附近画面中画质明显更好；点击后才会换帧")
        button.setToolTip("\n".join(lines))
        button.setProperty("recommended", recommended)
        button.style().unpolish(button)
        button.style().polish(button)

    def _choose_nearby_frame(self, timestamp: float):
        if self.video is None:
            return
        timestamp = max(0.0, float(timestamp))
        self._update_nearby_frame_selection(timestamp)
        self.timestamp_edit.setValue(timestamp)
        self._extract_frame()

    def _toggle_frame_lock(self, checked: bool):
        if self.document is None:
            self.frame_lock_button.setChecked(False)
            return
        before = self.document
        self._frame_locked = bool(checked)
        self.document = self.service.set_frame_locked(self.document, self._frame_locked)
        self.frame_lock_button.setText("已锁帧" if self._frame_locked else "锁帧")
        self._commit_document_change(before)
        message = "已锁定当前帧，后台候选和 AI 不会自动换帧" if self._frame_locked else "已解除帧锁定"
        self.status_changed.emit(message)

    def _find_more_frames(self):
        if self.video is None or not Path(self.video.path).is_file():
            message = "请先在字幕页选择一个包含视频的投稿项目"
            self._set_notice(message, "warning")
            self.status_changed.emit(message)
            return
        center = self.timestamp_edit.value() if self.draft.image_path else self._current_playhead
        request_generation = self._nearby_request_generation + 1
        self._nearby_request_generation = request_generation
        video = self.video
        self.more_frames_button.setEnabled(False)
        self.status_changed.emit("正在按需寻找更大范围画面…")
        self._run(
            lambda: self.service.wider_candidates(video, center),
            lambda result, error: self._wider_frames_ready(request_generation, result, error),
        )

    def _wider_frames_ready(self, request_generation: int, result, error, *, scope: str = "更大范围"):
        self.more_frames_button.setEnabled(self.video is not None)
        self.overview_button.setEnabled(self.video is not None)
        if request_generation != self._nearby_request_generation or error:
            if error:
                message = f"扩大取帧范围失败：{error}"
                self._set_notice(message, "error")
                self.status_changed.emit(message)
            return
        frames = tuple(result or ())
        self._wider_frames = tuple((frame.path, frame.timestamp) for frame in frames)
        if not frames:
            self._set_notice("没有找到更多可用画面", "warning")
            return
        # 旧版经验：后台先按画质筛选，用户最后确认；按时间顺序展示最好的几张。
        count = len(self.nearby_frame_buttons)
        picked = sorted(
            sorted(frames, key=lambda frame: frame.score - frame.subtitle_risk * 20, reverse=True)[:count],
            key=lambda frame: frame.timestamp,
        )
        best = recommended_frame(frames)
        for button, frame in zip(self.nearby_frame_buttons, picked):
            self._show_frame_on_button(button, frame, recommended=frame is best)
        self._update_nearby_frame_selection()
        message = f"已从 {len(frames)} 张{scope}画面中挑出画质较好的 {len(picked)} 张"
        self.status_changed.emit(message)

    def set_current_playhead(self, seconds: float):
        """接收字幕页只读播放位置，不触发 seek 或修改字幕状态。"""

        self._current_playhead = max(0.0, float(seconds or 0.0))
        if self.video is not None:
            self.current_frame_button.setToolTip(
                f"使用字幕页当前帧（{self._current_playhead:.2f} 秒），不会改变播放位置"
            )
            if not self.draft.image_path:
                self._refresh_nearby_frame_strip(self._current_playhead)
                if self.isVisible() and not self._frame_extract_pending:
                    QTimer.singleShot(0, self._use_current_frame)

    def _import_image(self):
        source, _ = QFileDialog.getOpenFileName(self, "选择封面底图", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not source or self.project is None or self.video is None:
            return
        try:
            path = self.service.import_image(source)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法导入底图", str(exc))
            return
        before_document = self.document
        if self.document is not None:
            background = next((item for item in self.document.objects if isinstance(item, BackgroundObject)), None)
            if background is not None:
                self.document = replace(self.document, objects=tuple(replace(item, asset=replace(item.asset, path=str(path)) if item.asset else AssetRef(path=str(path))) if isinstance(item, BackgroundObject) else item for item in self.document.objects))
                self.draft = CoverDraft.from_document(self.document)
            else:
                self.draft = replace(self._read_draft(), image_path=str(path))
        if self.document is not None and before_document != self.document:
            self.history.commit(self.document)
            self.undo_button.setEnabled(self.history.can_undo)
            self.redo_button.setEnabled(self.history.can_redo)
        else:
            self.draft = replace(self._read_draft(), image_path=str(path))
        self._save_draft()
        self._render_preview()

    def _use_current_frame(self):
        if not self.draft.image_path and self._reuse_layout_timestamp is not None:
            # 视频重新导出过：回到原来选中的时刻取帧。
            self.timestamp_edit.setValue(self._reuse_layout_timestamp)
            self._update_nearby_frame_selection(self._reuse_layout_timestamp)
            self._refresh_nearby_frame_strip(self._reuse_layout_timestamp)
            self._extract_frame()
            return
        if not self.draft.image_path and self._current_playhead < 0.5:
            # 字幕页没有播放过：不取片头黑帧，按旧网页端从全片挑画质好的一张。
            self._auto_pick_frame()
            return
        self.timestamp_edit.setValue(self._current_playhead)
        self._update_nearby_frame_selection(self._current_playhead)
        self._refresh_nearby_frame_strip(self._current_playhead)
        self._extract_frame()

    def _auto_pick_frame(self):
        if self.video is None or not Path(self.video.path).is_file():
            self._extract_frame()
            return
        self._frame_extract_pending = True
        self._frame_request_generation += 1
        request_generation = self._frame_request_generation
        video = self.video
        self.canvas.setText("正在从全片挑选画面…")
        self._set_notice("没有播放位置：正在从全片挑一张画质好的画面…", "info")
        self._run(
            lambda: self.service.overview_candidates(video),
            lambda result, error: self._overview_ready(request_generation, result, error),
        )

    def _overview_ready(self, request_generation: int, result, error):
        if request_generation != self._frame_request_generation:
            return
        self._frame_extract_pending = False
        best = None if error else best_overview_frame(tuple(result or ()))
        timestamp = best.timestamp if best is not None else self._current_playhead
        self.timestamp_edit.setValue(timestamp)
        self._update_nearby_frame_selection(timestamp)
        self._refresh_nearby_frame_strip(timestamp)
        self._extract_frame()

    def _extract_frame(self):
        if self.video is None or not Path(self.video.path).is_file():
            message = "请先在字幕页选择投稿项目和视频"
            self._set_notice(message, "warning")
            self.status_changed.emit(message)
            return
        first_background = not bool(self.draft.image_path)
        self.extract_button.setEnabled(False)
        self.current_frame_button.setEnabled(False)
        for button in self.nearby_frame_buttons:
            button.setEnabled(False)
        self._frame_extract_pending = True
        self._frame_request_generation += 1
        request_generation = self._frame_request_generation
        video = self.video
        timestamp = self.timestamp_edit.value()
        self._set_notice("正在从视频取帧…", "info")
        self.status_changed.emit("正在从视频取帧…")
        service = self.service

        def extract():
            path, actual = service.extract_frame(video, timestamp)
            if first_background:
                # 构图分析在后台预热缓存，回到界面线程的自动构图直接命中。
                service.warm_composition(path)
            return path, actual

        self._run(
            extract,
            lambda result, error: self._frame_ready(
                request_generation, first_background, result, error
            ),
        )

    def _frame_ready(self, request_generation: int, first_background: bool, result, error):
        if request_generation != self._frame_request_generation:
            return
        self._frame_extract_pending = False
        self.extract_button.setEnabled(self.video is not None and Path(self.video.path).is_file())
        self.current_frame_button.setEnabled(self.extract_button.isEnabled())
        self._refresh_nearby_frame_strip(self.timestamp_edit.value())
        if error:
            message = f"取帧失败：{error}"
            self._set_notice(message, "error")
            self.status_changed.emit(message)
            return
        path, timestamp = result
        self._update_nearby_frame_selection(timestamp)
        before_document = self.document
        draft = replace(self._read_draft(), image_path=str(path), selected_timestamp=timestamp)
        if self.document is not None:
            backgrounds = tuple(replace(item, asset=replace(item.asset, path=str(path)) if item.asset else AssetRef(path=str(path))) if isinstance(item, BackgroundObject) else item for item in self.document.objects)
            self.document = replace(self.document, source=replace(self.document.source, selected_timestamp=timestamp, image_asset_id=str(path)), objects=backgrounds)
            draft = CoverDraft.from_document(self.document)
        if first_background and self.document is not None and self._reuse_layout_timestamp is not None:
            # 视频更新过：原来的排版是用户确认过的，只换底图不重排。
            self._reuse_layout_timestamp = None
            draft = CoverDraft.from_document(self.document)
        elif first_background and self.document is not None:
            # 新底图的默认构图：两个比例分别保住主体，A/B 避开人脸和杂乱区域。
            self.document = self.service.apply_auto_layout(self.document, path)
            draft = CoverDraft.from_document(self.document)
        elif first_background:
            text_x, text_y = self.service.suggest_text_position(path, draft, canvas_key=self._canvas_key)
            draft = replace(draft, text_x=text_x, text_y=text_y)
        self.draft = draft
        if self.document is not None and before_document != self.document:
            self.history.commit(self.document)
            self.undo_button.setEnabled(self.history.can_undo)
            self.redo_button.setEnabled(self.history.can_redo)
        self.timestamp_edit.setValue(timestamp)
        self._refresh_nearby_frame_strip(timestamp)
        if first_background:
            # 自动排版改了字号和对齐，右侧控件跟着刷新。
            self.canvas.set_document(self.document, self._canvas_key)
            self._sync_selected_text_controls()
        self._save_draft()
        self._set_notice("")
        self.status_changed.emit("已加载当前视频画面")
        self._render_preview()
        self._queue_nearby_thumbnails(timestamp)
        self._refresh_schemes()

    def _store_background_controls(self):
        if self.document is None:
            return
        background = next((item for item in self.document.objects if isinstance(item, BackgroundObject)), None)
        if background is None:
            return
        current = object_for_profile(self.document, background.id, self._canvas_key)
        if not isinstance(current, BackgroundObject):
            current = background
        updated = replace(current, scale=self.zoom_spin.value())
        profile = self.document.profiles[self._canvas_key]
        payload = {"transform": updated.transform.to_payload(), "scale": updated.scale, "pan_x": updated.pan_x, "pan_y": updated.pan_y, "fit_mode": updated.fit_mode}
        self.document = replace(self.document, active_profile=self._canvas_key, profiles={**self.document.profiles, self._canvas_key: replace(profile, overrides={**profile.overrides, updated.id: payload})})

    def _background_position_changed(self, x: float, y: float):
        if self.project is None or self.video is None:
            return
        # Canvas 已经持有拖动中的本地对象；这里只刷新轻量取景显示。
        self.canvas.set_background_focus(x, y)

    def _zoom_changed(self, value: float):
        if self.project is None or self.video is None:
            return
        value = max(BACKGROUND_SCALE_MIN, min(BACKGROUND_SCALE_MAX, float(value)))
        self.zoom_spin.blockSignals(True)
        self.zoom_spin.setValue(value)
        self.zoom_spin.blockSignals(False)
        before = self.document
        if self.document is not None:
            background = next((item for item in self.document.objects if isinstance(item, BackgroundObject)), None)
            if background is not None:
                current = object_for_profile(self.document, background.id, self._canvas_key)
                current = current if isinstance(current, BackgroundObject) else background
                updated = replace(current, scale=value)
                profile = self.document.profiles[self._canvas_key]
                payload = {"transform": updated.transform.to_payload(), "scale": value, "pan_x": updated.pan_x, "pan_y": updated.pan_y, "fit_mode": updated.fit_mode}
                self.document = replace(self.document, profiles={**self.document.profiles, self._canvas_key: replace(profile, overrides={**profile.overrides, updated.id: payload})})
            self.draft = CoverDraft.from_document(self.document)
        else:
            self.draft = replace(self._read_draft(), background_scale=value)
        if self.document is not None and before != self.document:
            self.history.commit(self.document)
            self.undo_button.setEnabled(self.history.can_undo)
            self.redo_button.setEnabled(self.history.can_redo)
        self.canvas.set_zoom(value)
        self._draft_timer.start()
        self._preview_timer.start()

    def _fill_canvas(self):
        self._set_background_transform(0.5, 0.5, 1.0, fit_mode="cover")

    def _fit_canvas(self):
        self._set_background_transform(0.5, 0.5, 1.0, fit_mode="contain")

    def _set_background_transform(self, x: float, y: float, scale: float, *, fit_mode: str | None = None):
        before = self.document
        if self.document is not None:
            background = next((item for item in self.document.objects if isinstance(item, BackgroundObject)), None)
            if background is not None:
                current = object_for_profile(self.document, background.id, self._canvas_key)
                current = current if isinstance(current, BackgroundObject) else background
                updated = replace(current, pan_x=x, pan_y=y, scale=scale, fit_mode=fit_mode or current.fit_mode)
                profile = self.document.profiles[self._canvas_key]
                payload = {"transform": updated.transform.to_payload(), "scale": scale, "pan_x": x, "pan_y": y, "fit_mode": updated.fit_mode}
                self.document = replace(self.document, profiles={**self.document.profiles, self._canvas_key: replace(profile, overrides={**profile.overrides, updated.id: payload})})
            self.draft = CoverDraft.from_document(self.document)
        else:
            self.draft = replace(self._read_draft(), background_x=x, background_y=y, background_scale=scale)
        if self.document is not None and before != self.document:
            self.history.commit(self.document)
            self.undo_button.setEnabled(self.history.can_undo)
            self.redo_button.setEnabled(self.history.can_redo)
        self.zoom_spin.blockSignals(True)
        self.zoom_spin.setValue(scale)
        self.zoom_spin.blockSignals(False)
        self.canvas.set_zoom(scale)
        self.canvas.set_background_focus(x, y)
        self._draft_timer.start()
        self._preview_timer.start()
