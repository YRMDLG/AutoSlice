"""字幕样式（D-048）：右列换成样式面板，边改边在播放器上看预览；压制用当前选中的那套。"""

from __future__ import annotations

from autoslice.desktop.qt_preview import motion
from autoslice.desktop.subtitle_render import subtitle_font_problem
from autoslice.desktop.subtitle_styles import DEFAULT_ID
from autoslice.transcription.contracts import srt_timestamp_seconds

from .subtitle_style_panel import SubtitleStylePanel


class SubtitleStyleMixin:
    def _build_style_panel(self):
        self.style_panel = SubtitleStylePanel(self.style_store, self.storage)
        self.style_card = self._card(self.style_panel)
        self.side_stack.addWidget(self.style_card)
        self.style_panel.style_changed.connect(self._style_changed)
        self.style_panel.close_requested.connect(lambda: self._toggle_style_panel(False))
        self.style_panel.step_requested.connect(self._style_preview_step)
        self.style_panel.font_check_requested.connect(self._check_style_font)
        self._sync_style_labels()

    # ── 查询 ──
    def _subtitle_style(self) -> dict:
        store = getattr(self, "style_store", None)
        return dict(store.active().style) if store is not None else {}

    def _style_name(self) -> str:
        store = getattr(self, "style_store", None)
        return store.active().name if store is not None else "默认"

    def _style_panel_open(self) -> bool:
        return hasattr(self, "style_card") and self.side_stack.currentWidget() is self.style_card

    # ── 面板开合 ──
    def _toggle_style_panel(self, show=None):
        if not hasattr(self, "style_card"):
            return
        show = (not self._style_panel_open()) if show is None else bool(show)
        if show == self._style_panel_open():
            self.style_toggle.setChecked(show)
            return
        if show:
            self._commit_editor()
            self.side_stack.setCurrentWidget(self.style_card)
            motion.fade_in(self.style_card, 160)
            # 预览要开着，定位线还得停在有字幕的地方，不然看不到效果
            if self.player and not self.preview_toggle.isChecked():
                self.preview_toggle.setChecked(True)
                self._toggle_subtitle_preview()
            self._show_style_on_cue()
            self._update_style_hint()
        else:
            self.style_panel.flush()
            self.side_stack.setCurrentWidget(self.side_splitter)
        self.style_toggle.setChecked(show)

    def _update_style_hint(self):
        if not self.player:
            hint = "这台电脑没有播放组件，视频上看不到预览；压制照样按这套样式。"
        elif not self.document:
            hint = "先选一个项目，视频上才有字幕可看。"
        else:
            hint = ""
        self.style_panel.set_preview_hint(hint)

    def _cue_at(self, seconds: float):
        if not self.document:
            return None
        return next((entry for entry in self.document.entries
                     if srt_timestamp_seconds(entry.start) <= seconds < srt_timestamp_seconds(entry.end)), None)

    def _show_style_on_cue(self):
        """定位线不在任何一句上时，跳到选中句（没有就第一句）的中间。"""

        if not self.document or self._cue_at(self._player_position) is not None:
            return
        active = self.selection.active if self.selection.active is not None else self.selected_index
        entry = next((item for item in self.document.entries if item.index == active), self.document.entries[0])
        self._jump_to_cue_middle(entry)

    def _jump_to_cue_middle(self, entry):
        start, end = srt_timestamp_seconds(entry.start), srt_timestamp_seconds(entry.end)
        self.selection.choose(entry.index, self._cue_order())
        self.selected_index = entry.index
        self._select_cue(entry.index)
        self._seek_to((start + end) / 2)

    def _style_preview_step(self, delta: int):
        """面板里的“上一句 / 下一句”：换一句看样式（长句、短句各看一眼）。"""

        if not self.document or not self.document.entries:
            return
        entries = self.document.entries
        current = self._cue_at(self._player_position)
        if current is not None:
            position = entries.index(current) + delta
        else:
            # 停在空白处：往后找下一句、往前找上一句
            following = next((i for i, e in enumerate(entries)
                              if srt_timestamp_seconds(e.start) > self._player_position), len(entries))
            position = following if delta > 0 else following - 1
        position = max(0, min(len(entries) - 1, position))
        self._jump_to_cue_middle(entries[position])

    # ── 样式变化 ──
    def _style_changed(self, _style: dict):
        self._preview_timer.start()
        self._sync_style_labels()
        # 设置页的字体提醒按新样式重查
        self._render_font_checked = False

    def _sync_style_labels(self):
        name = self._style_name()
        style = self._subtitle_style()
        if hasattr(self, "render_button"):
            self.render_button.setToolTip(f"用“{name}”字幕样式，把已保存的校对字幕压制到新视频")
        if hasattr(self, "style_toggle"):
            self.style_toggle.setToolTip(f"调整压制用的字幕样式（现在用“{name}”）")
        if hasattr(self, "render_style_note") and style:
            kind = "你原来的压制配置" if self.style_store.active().id == DEFAULT_ID else "自建"
            self.render_style_note.setText(
                f"当前样式：{name}（{kind}）· {style['font_name']} · 字号 {style['font_size']:g} · "
                f"#{style['outline_color']} 描边")

    def _check_style_font(self, font_name: str, bold: bool):
        """后台查一次这个字体本机能不能用上（要跑一次 FFmpeg，结果有缓存）。"""

        def done(problem, error):
            if hasattr(self, "style_panel"):
                self.style_panel.set_font_status(font_name, "" if error is not None else problem)

        self._run(lambda: subtitle_font_problem(font_name, bold), done)

    def _open_style_from_settings(self):
        """设置页“调整字幕样式”：回到字幕页并打开样式面板。"""

        self._select_page(0)
        self._toggle_style_panel(True)
