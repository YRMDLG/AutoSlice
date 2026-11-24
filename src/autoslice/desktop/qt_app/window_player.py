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
    QHBoxLayout,
    QPushButton,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.qt_preview import popups
from autoslice.desktop.qt_preview.icons import icon as desktop_icon
from autoslice.desktop.qt_preview.shell import Popover
from autoslice.desktop.qt_preview.theme import COLORS, SIZES
from autoslice.desktop.qt_preview.window import label, tool_icon
from autoslice.transcription.contracts import srt_timestamp_seconds


class PlayerMixin:
    def _video_surface(self):
        surface = QWidget()
        surface.setObjectName("videoSurface")
        surface.setMinimumHeight(200)
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(10, 10, 10, 0)
        self.video_stack = QStackedWidget()
        self.video_stack.setObjectName("videoFrame")
        self.video_stack.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
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
        # 无框按钮按图标对齐左边线
        controls.setContentsMargins(4, 10, 4, 10)
        controls.setSpacing(4)
        self.seek_back_button = QPushButton()
        self.seek_forward_button = QPushButton()
        for button, name, tip, step in ((self.seek_back_button, "skip_back", "后退 2 秒", -2.0),
                                        (self.seek_forward_button, "skip_forward", "前进 2 秒", 2.0)):
            button.setObjectName("tool")
            button.setProperty("square", True)
            button.setFixedSize(32, 32)
            button.setToolTip(tip)
            button.setAccessibleName(tip)
            tool_icon(button, name)
            button.clicked.connect(lambda _checked=False, delta=step: self._nudge_playhead(delta))
        self.play_button = QPushButton("播放")
        # 播放是这一行的主动作：夜航强调色描边，晨雾实心强调色
        self.play_button.setObjectName("primary")
        self.play_button.setFixedHeight(34)
        self.play_button.setIcon(desktop_icon("play", COLORS.primary_text, SIZES.tool_icon_size))
        self.play_button.setIconSize(QSize(SIZES.tool_icon_size, SIZES.tool_icon_size))
        self.play_button.setToolTip("播放 / 暂停（空格）")
        self.play_button.setEnabled(False)
        self.play_button.clicked.connect(self._toggle_play)
        controls.addWidget(self.seek_back_button)
        controls.addWidget(self.play_button)
        controls.addWidget(self.seek_forward_button)
        controls.addSpacing(8)
        self.preview_toggle = QPushButton("字幕预览")
        self.preview_toggle.setObjectName("tool")
        self.preview_toggle.setCheckable(True)
        tool_icon(self.preview_toggle, "eye", checkable=True)
        self.preview_toggle.setChecked(True)
        self.preview_toggle.setToolTip("显示接近最终压制样式的实时字幕预览")
        self.preview_toggle.clicked.connect(self._toggle_subtitle_preview)
        self.time_label = label("00:00:00 / 00:00:00", "timecode")
        controls.addWidget(self.time_label)
        controls.addStretch()
        controls.addWidget(self.preview_toggle)
        # 音量收进弹层：点喇叭弹出滑块，平时不占控制条
        self.volume_button = QPushButton()
        self.volume_button.setObjectName("tool")
        self.volume_button.setProperty("square", True)
        self.volume_button.setFixedSize(32, 32)
        self.volume_button.setToolTip("音量")
        self.volume_button.setAccessibleName("音量")
        tool_icon(self.volume_button, "volume")
        controls.addWidget(self.volume_button)
        volume_panel = QWidget()
        volume_row = QHBoxLayout(volume_panel)
        volume_row.setContentsMargins(4, 2, 4, 2)
        volume_row.setSpacing(12)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(50)
        self.volume.setMinimumWidth(140)
        self.volume.valueChanged.connect(self._volume_changed)
        volume_row.addWidget(self.volume, 1)
        self.volume_value = label("50", "mono")
        self.volume_value.setMinimumWidth(26)
        self.volume.valueChanged.connect(lambda value: self.volume_value.setText(str(value)))
        volume_row.addWidget(self.volume_value)
        self.volume_popover = Popover(self, volume_panel, 212)
        self.volume_button.clicked.connect(lambda: self.volume_popover.popup(
            self.volume_button, align_right=True, above=True))
        self.speed = popups.ComboBox()
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
        self._clear_playback_range()
        self._seek_to(start)
        self._audition_until = end
        self._player_paused = False
        self.player.seek(start, pause=False)
        self.player.play()
        self._set_play_button(False)

    def _clear_playback_range(self):
        self._audition_until = None
        self._loop_enabled = False
        self._loop_cue_id = None
        self._loop_seek_pending = False

    def _set_loop(self, enabled):
        self._loop_enabled = bool(enabled)
        self._audition_until = None
        self._loop_cue_id = self.selection.active if enabled else None
        self._loop_seek_pending = False
        self._show_transient_status("已开启当前字幕循环" if enabled else "已关闭当前字幕循环")

    def _seek_to(self, seconds):
        # 主动定位结束当前试听；不能把旧试听的停止点带到新的播放位置。
        self._audition_until = None
        if not self.player:
            # 没有播放器（缺 libmpv）时播放头照样跳，按播放头裁切、拆分仍可用。
            duration = self.timeline.duration
            target = max(0.0, float(seconds))
            if duration > 0:
                target = min(target, duration)
            self._player_position = target
            self.timeline.set_playhead(target, playing=False)
            self.time_label.setText(f"{self._clock(target)} / {self._clock(duration)}")
            return
        if not self._media_ready:
            self._pending_seek = max(0.0, float(seconds))
            return
        target = max(0.0, min(float(seconds), self._player_duration))
        if self._loop_enabled:
            bounds = self._entry_bounds(self._loop_cue_id)
            if bounds is None or not max(0.0, bounds[0] - .2) <= target <= bounds[1] + .2:
                self._clear_playback_range()
            self._loop_seek_pending = False
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
        # 用户接管播放后恢复普通播放，避免继续被上次试听的结束点截停。
        self._audition_until = None
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
        self.play_button.setIcon(desktop_icon("play" if paused else "pause", COLORS.primary_text, SIZES.tool_icon_size))

    def _space_play(self):
        """空格：暂停时有选中字幕就从那句开头播放；播放中按空格暂停；没有选中字幕时普通播放 / 暂停。"""

        if not self.player or not self._media_ready:
            return
        if self._player_paused:
            bounds = self._entry_bounds(self.selected_index) if self.selected_index is not None else None
            if bounds is not None:
                self._seek_to(bounds[0])
        self._toggle_play()

    def _nudge_playhead(self, delta):
        """控制条上的后退 / 前进 2 秒：和点时间轴一样定位并暂停。"""

        self._seek_to(max(0.0, getattr(self, "_player_position", 0.0) + delta))

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
                requested_seek = True
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
