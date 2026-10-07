"""播放：视频区、试听与循环、跳转、播放控制、播放器状态与字幕叠加预览。

混入 DesktopWindow，排在 PreviewWindow 之前；状态都在窗口上，不单独实例化。
"""

from __future__ import annotations

import time

from PySide6.QtCore import (
    QSize,
    Qt,
)
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QPushButton,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.qt_preview.icons import icon as desktop_icon
from autoslice.desktop.qt_preview.theme import COLORS, SIZES
from autoslice.desktop.qt_preview.window import label
from autoslice.transcription.contracts import srt_timestamp_seconds


class PlayerMixin:
    def _video_surface(self):
        surface = QWidget()
        surface.setObjectName("videoSurface")
        surface.setMinimumHeight(200)
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(12, 12, 12, 0)
        self.video_stack = QStackedWidget()
        self.video_placeholder = label("选择项目后可校对真实 SRT\n视频播放将在 Desktop-05 接入", "hint")
        self.video_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_stack.addWidget(self.video_placeholder)
        self.video_widget = QWidget()
        self.video_widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.video_widget.setStyleSheet("background: #000000;")
        self.video_stack.addWidget(self.video_widget)
        layout.addWidget(self.video_stack, 1)
        controls_bar = QWidget()
        controls_bar.setObjectName("playerControls")
        controls = QHBoxLayout(controls_bar)
        controls.setContentsMargins(8, 6, 8, 6)
        controls.setSpacing(SIZES.space_3)
        self.play_button = QPushButton("播放")
        self.play_button.setIcon(desktop_icon("play", COLORS.text))
        self.play_button.setIconSize(QSize(SIZES.icon_size, SIZES.icon_size))
        self.play_button.setEnabled(False)
        self.play_button.clicked.connect(self._toggle_play)
        controls.addWidget(self.play_button)
        self.preview_toggle = QPushButton("字幕预览")
        self.preview_toggle.setCheckable(True)
        self.preview_toggle.setChecked(True)
        self.preview_toggle.setToolTip("显示接近最终压制样式的实时字幕预览")
        self.preview_toggle.clicked.connect(self._toggle_subtitle_preview)
        controls.addWidget(self.preview_toggle)
        self.time_label = label("00:00:00 / 00:00:00", "timecode")
        controls.addWidget(self.time_label)
        controls.addStretch()
        volume_icon = label("")
        volume_icon.setPixmap(desktop_icon("volume").pixmap(SIZES.icon_size, SIZES.icon_size))
        volume_icon.setToolTip("音量")
        controls.addWidget(volume_icon)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(50)
        self.volume.setFixedWidth(90)
        self.volume.valueChanged.connect(self._volume_changed)
        controls.addWidget(self.volume)
        self.speed = QComboBox()
        for value in (0.75, 1.0, 1.25, 1.5, 2.0):
            self.speed.addItem(f"{value:g}×", value)
        self.speed.setCurrentIndex(1)
        self.speed.currentIndexChanged.connect(self._speed_changed)
        controls.addWidget(self.speed)
        layout.addWidget(controls_bar)
        return surface

    def _audition(self, side):
        bounds = self._entry_bounds()
        if bounds is None:
            return
        start, end = bounds
        if side == "start":
            self._play_window(max(0.0, start - .45), min(self._player_duration, start + .5))
        else:
            self._play_window(max(0.0, end - .5), min(self._player_duration, end + .45))

    def _audition_start(self):
        self._audition("start")

    def _audition_end(self):
        self._audition("end")

    def _audition_current(self):
        bounds = self._entry_bounds()
        if bounds:
            self._play_window(max(0.0, bounds[0] - .2),
                              min(self._player_duration, bounds[1] + .2))

    def _audition_before(self):
        bounds = self._entry_bounds()
        if bounds:
            self._play_window(max(0.0, bounds[0] - .5), bounds[0])

    def _audition_after(self):
        bounds = self._entry_bounds()
        if bounds:
            self._play_window(bounds[1], min(self._player_duration, bounds[1] + .5))

    def _audition_once(self):
        bounds = self._entry_bounds()
        if bounds:
            self._play_window(bounds[0], min(self._player_duration, bounds[1]))

    def _play_window(self, start, end):
        if not self.player or not self._media_ready or end <= start:
            return
        self._audition_until = end
        self._loop_enabled = False
        self._seek_to(start)
        self._player_paused = False
        self.player.seek(start, pause=False)
        self.player.play()
        self._set_play_button(False)

    def _set_loop(self, enabled):
        self._loop_enabled = bool(enabled)
        self._audition_until = None
        self._loop_cue_id = self.selection.active if enabled else None
        self._loop_seek_pending = False
        self._show_transient_status("已开启当前字幕循环" if enabled else "已关闭当前字幕循环")

    def _seek_to(self, seconds):
        if not self.player:
            return
        if not self._media_ready:
            self._pending_seek = max(0.0, float(seconds))
            return
        target = max(0.0, min(float(seconds), self._player_duration))
        self._pending_seek = None
        self._player_paused = True
        self._set_play_button(True)
        self._player_position = target
        self._seek_guard_target = target
        self._seek_guard_until = time.monotonic() + 1.0
        # 主动 seek 后先清掉旧的“正在播放字幕”高亮，避免 mpv 的旧 position
        # 回调把下一条字幕短暂/稳定染成选中态。
        if self.document:
            self.model.set_playing_index(None)
        self.timeline.set_playing_cue(None)
        self.timeline.set_playhead(target, playing=False)
        self.player.seek(target, pause=True)

    def _refresh_subtitle_preview(self):
        if not self.document or not self.player or not self.preview_toggle.isChecked():
            return
        try:
            path = self.preview_service.render(self.document.video.path, self.document.entries)
            self.player.set_subtitle(path)
            self._preview_attached = True
        except (OSError, ValueError) as exc:
            self._show_transient_status(f"字幕预览暂不可用：{exc}")

    def _toggle_subtitle_preview(self):
        if not self.player:
            return
        if self.preview_toggle.isChecked():
            self._refresh_subtitle_preview()
        else:
            self.player.clear_subtitle()
            self._preview_attached = False

    def _toggle_play(self):
        if not self.player or not self._media_ready:
            return
        if self._player_paused:
            self._player_paused = False
            # 开始播放不应抢走用户手动调整过的时间轴视野。
            # timeline.set_playhead() 会在允许跟随时自行处理播放跟随；
            # 若用户已通过滚动条/滚轮/侧键平移，_manual_pan 会保持当前视窗。
            self.player.play()
        else:
            self._player_paused = True
            self.player.pause()
        self._set_play_button(self._player_paused)

    def _set_play_button(self, paused):
        self.play_button.setText("播放" if paused else "暂停")
        self.play_button.setIcon(desktop_icon("play" if paused else "pause", COLORS.text))

    def _volume_changed(self, value):
        if getattr(self, "player", None):
            self.player.set_volume(value)

    def _speed_changed(self):
        if getattr(self, "player", None):
            self.player.set_speed(float(self.speed.currentData()))

    @staticmethod
    def _clock(seconds):
        value = max(0, int(seconds or 0))
        return f"{value // 3600:02d}:{value // 60 % 60:02d}:{value % 60:02d}"

    def _player_status(self, state):
        if not self.project or not self.project.videos:
            return
        duration = state.get("duration")
        position = state.get("position")
        if position is not None and self._seek_guard_target is not None:
            if abs(float(position) - self._seek_guard_target) <= .35:
                self._seek_guard_target = None
                self._seek_guard_until = 0.0
            elif time.monotonic() < self._seek_guard_until:
                # mpv seek 后可能先回报 seek 前的旧 position。这个旧位置不能再
                # 驱动 playhead / playing cue，否则用户会看到“跳到下一字幕”的假选中。
                position = None
            else:
                self._seek_guard_target = None
                self._seek_guard_until = 0.0
        requested_seek = False
        if duration is not None and duration > 0:
            self._player_duration = duration
            if state.get("idle") is False:
                self._media_ready = True
                self.play_button.setEnabled(True)
                if self.document and self.preview_toggle.isChecked() and not self._preview_attached:
                    self._refresh_subtitle_preview()
            self.timeline.set_duration(duration)
            if self._pending_seek is not None and self._media_ready:
                self._resume_pending = False
                self._seek_to(self._pending_seek)
                requested_seek = True
            elif self._resume_pending:
                self._resume_pending = False
                resume = self._session.get("playback_seconds", 0)
                if isinstance(resume, (int, float)) and 0 < resume < duration:
                    self._seek_to(float(resume))
                    requested_seek = True
        if position is not None and not requested_seek:
            self._player_position = position
            self.timeline.set_playhead(position, playing=state.get("paused") is False)
            if hasattr(self, "cover_editor"):
                self.cover_editor.set_current_playhead(position)
            if self._audition_until is not None and position >= self._audition_until:
                until = self._audition_until
                self._audition_until = None
                self._player_paused = True
                self.player.pause()
                self._seek_to(until)
            elif self._loop_enabled and self._loop_cue_id is not None:
                bounds = self._entry_bounds(self._loop_cue_id)
                if bounds is not None:
                    loop_start = max(0.0, bounds[0] - .2)
                    loop_end = min(self._player_duration, bounds[1] + .2)
                    if position < loop_end - .05:
                        self._loop_seek_pending = False
                    if position >= loop_end and not self._loop_seek_pending:
                        self._loop_seek_pending = True
                        self._player_position = loop_start
                        self.player.seek(loop_start, pause=False)
        if state.get("paused") is not None and not requested_seek:
            self._player_paused = state["paused"]
        if "hwdec" in state:
            self._hwdec = state["hwdec"]
        self._set_play_button(self._player_paused)
        self.time_label.setText(
            f"{self._clock(self._player_position)} / {self._clock(self._player_duration)}"
        )
        if self.document and position is not None and not requested_seek:
            active = next((entry.index for entry in self.document.entries
                           if srt_timestamp_seconds(entry.start) <= position
                           < srt_timestamp_seconds(entry.end)), None)
            self.model.set_playing_index(active)
            self.timeline.set_playing_cue(active)

    def _player_error(self, message):
        self.app_status.setText(f"播放器错误：{message}")
        if not self._media_ready:
            self.play_button.setEnabled(False)
            self.video_placeholder.setText(f"视频无法播放：{message}")
            self.video_stack.setCurrentIndex(0)

    def _check_player_load(self, token):
        if token == self._player_load_token and self._player_duration <= 0:
            self._player_error("视频加载超时或格式不受支持")
