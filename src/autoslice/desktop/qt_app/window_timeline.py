"""时间轴：选择与框选、吸附/波形/适配、裁切、拆分合并、右键菜单、滚动同步。

混入 DesktopWindow，排在 PreviewWindow 之前；状态都在窗口上，不单独实例化。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollBar,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.qt_preview import popups
from autoslice.desktop.qt_preview.window import label, tool_icon
from autoslice.transcription.contracts import srt_timestamp_seconds

from .timeline import SubtitleTimeline


class _ScrollDock(QObject):
    """时间轴变尺寸时，把横向细滚动条摆到轨道底边以内。"""

    def __init__(self, timeline, bar) -> None:
        super().__init__(timeline)
        self._bar = bar

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._bar.setGeometry(12, obj.height() - 7, max(10, obj.width() - 24), 6)
            self._bar.raise_()
        return False


class TimelineMixin:
    def _timeline(self):
        panel = QWidget()
        panel.setObjectName("timeline")
        # 最小高度由标题行与时间轴画布推算，避免波形被裁
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 6, 14, 12)
        layout.setSpacing(6)
        heading = QHBoxLayout()
        heading.setSpacing(6)
        heading.setContentsMargins(4, 3, 0, 3)
        heading.addWidget(label("时间轴", "sectionTitle"))
        heading.addSpacing(10)
        # 设计稿：磁吸、波形是纯文字开关，适合全部是无框文字按钮；操作提示收进悬停说明
        self.snap_button = QPushButton("磁吸")
        self.snap_button.setObjectName("tool")
        self.snap_button.setCheckable(True)
        self.snap_button.setChecked(True)
        self.snap_button.setToolTip("自动吸附字幕边界；拖动时按住 Alt 可临时关闭")
        self.snap_button.clicked.connect(self._toggle_snapping)
        heading.addWidget(self.snap_button)
        self.waveform_button = QPushButton("波形")
        self.waveform_button.setObjectName("tool")
        self.waveform_button.setCheckable(True)
        self.waveform_button.setChecked(True)
        self.waveform_button.setToolTip("显示轻量音频波形")
        self.waveform_button.clicked.connect(self._toggle_waveform)
        heading.addWidget(self.waveform_button)
        self.fit_button = QPushButton("适合全部")
        self.fit_button.setObjectName("tool")
        self.fit_button.setFixedHeight(26)
        self.fit_button.setToolTip("把整段字幕放进视野")
        self.fit_button.clicked.connect(self._fit_timeline)
        heading.addWidget(self.fit_button)
        self.more_button = QPushButton()
        self.more_button.setObjectName("tool")
        self.more_button.setProperty("square", True)
        self.more_button.setFixedSize(26, 26)
        tool_icon(self.more_button, "more")
        self.more_button.setToolTip("字幕编辑操作（也可在字幕块上右键）")
        self.more_button.clicked.connect(lambda: self._show_edit_menu(
            self.more_button.mapToGlobal(self.more_button.rect().bottomLeft()), global_position=True))
        heading.addWidget(self.more_button)
        heading.addStretch()
        self.timeline_overlap = label("", "danger")
        heading.addWidget(self.timeline_overlap)
        heading.addSpacing(8)
        self.timeline_span = label("", "hintLine")
        self.timeline_span.setToolTip("Ctrl+滚轮缩放 · 拖动空白框选 · 滚轮/鼠标侧键平移 · Ctrl+/ 快捷键一览")
        heading.addWidget(self.timeline_span)
        layout.addLayout(heading)
        self.timeline = SubtitleTimeline()
        self.timeline.set_waveform_visible(True)
        self.timeline.selection_requested.connect(self._timeline_selection_requested)
        self.timeline.marquee_selected.connect(self._timeline_marquee_selected)
        self.timeline.preview_changed.connect(self._timeline_preview_changed)
        self.timeline.changed.connect(self._timeline_changed)
        self.timeline.preview_requested.connect(self._timeline_preview_requested)
        self.timeline.seek_requested.connect(self._seek_to)
        self.timeline.view_changed.connect(self._sync_timeline_scroll)
        self.timeline.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.timeline.customContextMenuRequested.connect(self._show_edit_menu)
        layout.addWidget(self.timeline, 1)
        # 横向滚动条贴在轨道底边里面（设计稿没有独立滚动条，只留一道细条供拖动）
        self.timeline_scroll = QScrollBar(Qt.Orientation.Horizontal, self.timeline)
        self.timeline_scroll.setObjectName("timelineScroll")
        self.timeline_scroll.setFixedHeight(6)
        self.timeline.installEventFilter(_ScrollDock(self.timeline, self.timeline_scroll))
        self.timeline_scroll.setTracking(True)
        self.timeline_scroll.setSingleStep(250)
        self.timeline_scroll.valueChanged.connect(self._timeline_scroll_changed)
        self._syncing_timeline_scroll = False
        self._sync_timeline_scroll(0.0, self.timeline.span, self.timeline.duration)
        return panel

    def _timeline_selection_requested(self, cue_index: int, ctrl: bool, shift: bool):
        if not self.document:
            return
        self.selection.choose(cue_index, self._cue_order(), ctrl=ctrl, shift=shift)
        self.selected_index = self.selection.active
        # 时间轴里点到的字幕本来就已经在当前可视窗口内。
        # 这里只同步选中态和底部列表，不允许 selection 重新居中时间轴，
        # 否则会重置 _manual_pan，随后 playhead/player 回调又会二次改变视野，造成闪烁跳动。
        self._select_cue(self.selection.active, center_timeline=False)
        # 单击字幕块不定位（2026-11-26 用户要求），只有右侧列表单击才跳到句首。
        self._save_session()

    def _timeline_marquee_selected(self, cue_ids: set[int]):
        if not self.document:
            return
        self.selection.replace(cue_ids, self._cue_order())
        self.selected_index = self.selection.active
        self._select_cue(self.selected_index, center_timeline=False)
        if self.selected_index is None:
            self._show_transient_status("已取消多选")
        else:
            self._show_transient_status(f"已选择 {len(self.selection.selected)} 条字幕")

    def _toggle_snapping(self):
        self.snapping = self.snap_button.isChecked() if hasattr(self, "snap_button") else not self.snapping
        self.snap_button.setChecked(self.snapping)
        if hasattr(self, "timeline"):
            self.timeline.set_snapping(self.snapping)
        self._show_transient_status("已开启磁吸" if self.snapping else "已关闭磁吸")

    def _toggle_waveform(self):
        self.waveform_visible = self.waveform_button.isChecked() if hasattr(self, "waveform_button") else not self.waveform_visible
        self.waveform_button.setChecked(self.waveform_visible)
        self.timeline.set_waveform_visible(self.waveform_visible)
        self._show_transient_status("波形已显示" if self.waveform_visible else "波形已隐藏")

    def _fit_timeline(self):
        if self.document:
            duration = self.timeline.duration or srt_timestamp_seconds(self.document.entries[-1].end)
            self.timeline._manual_pan = False
            self.timeline.glide_to(duration / 2, max(4.0, self.timeline.duration or self.timeline.span))

    def _trim_start(self):
        self._trim_active("start")

    def _trim_end(self):
        self._trim_active("end")

    def _trim_active(self, side):
        if self._saving or not self.document or self.selection.active is None:
            return
        self._commit_editor()
        entry = next((item for item in self.document.entries if item.index == self.selection.active), None)
        if not entry:
            return
        start, end = srt_timestamp_seconds(entry.start), srt_timestamp_seconds(entry.end)
        if not start < self._player_position < end:
            self._show_transient_status("定位线不在当前字幕内")
            return
        try:
            self.document.change_time(entry.index, self._player_position if side == "start" else start,
                                      end if side == "start" else self._player_position)
        except ValueError as exc:
            self._show_transient_status(str(exc))
            return
        self.model.set_document(self.document)
        self.timeline.update()
        self._edited()

    def _split_at_playhead(self):
        if self._saving or not self.document or self.selection.active is None:
            return
        entry = next((item for item in self.document.entries if item.index == self.selection.active), None)
        if not entry:
            return
        editor = next((item for item in self.table.findChildren(QPlainTextEdit) if item.isVisible()), None)
        caret_text = None
        if editor:
            cursor = editor.textCursor()
            caret_text = editor.toPlainText()[:cursor.position()]
        self._commit_editor()
        try:
            new_index = self.document.split_at(entry.index, self._player_position,
                                                left_text=caret_text)
        except ValueError as exc:
            self._show_transient_status(str(exc))
            return
        self.selection.choose(new_index, self._cue_order())
        self.selected_index = new_index
        self.model.set_document(self.document)
        self.subtitle_delegate.effects.play(new_index, "born")
        self.subtitle_delegate.effects.play(entry.index, "flash")
        self._select_cue(new_index)
        self._edited()
        self._show_transient_status("已拆分字幕；请补全右侧正文后保存")
        self._toast("已在播放头处拆成两条", undo=True)

    def _show_edit_menu(self, position, *, global_position=False):
        menu = QMenu(self)
        effects = popups.MenuFx(menu)
        # 面板宽 232（设计稿），加两侧投影留白
        menu.setMinimumWidth(232 + popups.SHADOW[0] + popups.SHADOW[2])
        active = self.selection.active if self.selection.active is not None else self.selected_index
        entry = next((item for item in self.document.entries if item.index == active), None) if self.document else None
        if entry is not None:
            # 菜单头：第几条、开始时间；后面按设计稿：编辑、定位、拆分、与下一条合并、删除
            title = menu.addAction(f"第 {entry.index} 条 · {entry.start[:8]}")
            title.setEnabled(False)
            title.setProperty("heading", True)
            menu.addAction("编辑文字", lambda: self._edit_cue_text(entry.index))
            menu.addAction("定位到这一句", lambda: self._seek_to(srt_timestamp_seconds(entry.start)))
        menu.addAction("在定位线拆分 Ctrl+B", self._split_at_playhead)
        if entry is not None:
            following = self._following_cue(entry.index)
            merge_next = menu.addAction("与下一条合并", lambda: self._merge_with_next(entry.index))
            merge_next.setEnabled(following is not None and not self._saving)
        menu.addSeparator()
        delete = menu.addAction("删除 Delete", self._delete)
        delete.setProperty("danger", True)
        menu.addSeparator()
        # 不常用的收进子菜单，菜单不再长过窗口
        edits = popups.submenu(menu, "裁剪与贴合")
        edits.addAction("左裁到定位线 Q", self._trim_start)
        edits.addAction("右裁到定位线 W", self._trim_end)
        merge = edits.addAction("合并所选字幕", self._merge_selected)
        merge.setEnabled(self._can_merge_selected())
        if not merge.isEnabled():
            merge.setToolTip("请先选择时间轴中连续相邻的字幕")
        edits.addSeparator()
        # 字幕块和声音对不上（尾音被切、拖进静音、两句间闪一下）：按真实停顿重定起止
        snap = edits.addAction("选中字幕贴合声音", lambda: self._snap_cues_to_speech(selected_only=True))
        snap.setEnabled(bool(self.document) and bool(self.selection.selected))
        edits.addAction("全部字幕贴合声音", lambda: self._snap_cues_to_speech(selected_only=False)).setEnabled(
            bool(self.document))
        listen = popups.submenu(menu, "试听")
        listen.addAction("试听这一句 F1", self._audition_current)
        listen.addAction("只播这一句 F4", self._audition_once)
        listen.addAction("试听开头", self._audition_start)
        listen.addAction("试听结尾", self._audition_end)
        loop = listen.addAction("循环当前字幕")
        loop.setCheckable(True)
        loop.setChecked(self._loop_enabled)
        loop.triggered.connect(self._set_loop)
        menu.addAction("快捷键一览 Ctrl+/", self._show_shortcuts)
        anchor = position if global_position else (
            self.timeline.mapToGlobal(position) if position is not None else self.cursor().pos()
        )
        effects.popup(anchor)
        # 每次右键都新建一份，关掉就删，免得菜单越积越多
        menu.deleteLater()

    def _show_shortcuts(self):
        dialog = getattr(self, "_shortcuts_dialog", None)
        if dialog is None:
            from .shortcuts_dialog import ShortcutsDialog

            dialog = self._shortcuts_dialog = ShortcutsDialog(self)
        if not dialog.isVisible():
            dialog.place_over(self.window())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _snap_cues_to_speech(self, *, selected_only: bool, foreground: bool = False, chosen=None):
        """起止按真实停顿重定（识别后的同一套规则）；后台抽一次音频，结果一步撤销。

        foreground=True：用压低背景音乐后的音频再试（背景声音太满、分不清人声时）。
        """

        if self._saving or not self.document or getattr(self, "_speech_snapping", False):
            return
        self._commit_editor()
        entries = tuple(self.document.entries)
        if chosen is None:
            chosen = set(self.selection.selected) if selected_only else {entry.index for entry in entries}
        if not chosen:
            self._show_transient_status("先在列表或时间轴里选中要调整的字幕")
            return
        from autoslice.desktop.asr.refine import snap_to_speech, speech_audio

        segments = [(srt_timestamp_seconds(e.start), srt_timestamp_seconds(e.end), e.text) for e in entries]
        video, cache = self.document.video.path, self.storage.root / "cache" / "speech"
        self._speech_snapping = True
        self._show_transient_status("正在压低背景音乐后再对一遍…" if foreground else "正在对着声音调整字幕起止…", 60000)

        def action():
            reasons: dict[int, str] = {}
            snapped = snap_to_speech(segments, speech_audio(cache, video, foreground=foreground), reasons=reasons)
            return snapped, reasons

        self._run(action, lambda result, error: self._speech_snapped(entries, chosen, result, error, foreground))

    def _speech_snapped(self, entries, chosen, result, error, foreground=False):
        self._speech_snapping = False
        if error:
            self._show_transient_status(f"贴合声音没成功：{error}", 9000)
            return
        result, reasons = result
        if not self.document or tuple(self.document.entries) != entries or len(result) != len(entries):
            self._show_transient_status("调整期间字幕改过了，没有应用；需要的话再点一次")
            return
        before = entries
        changed = 0
        for position, (entry, (start, end, _text)) in enumerate(zip(entries, result)):
            if entry.index not in chosen:
                continue
            # 没选中的邻句保持原样：不压到它们
            if position and entries[position - 1].index not in chosen:
                start = max(start, srt_timestamp_seconds(entries[position - 1].end))
            if position + 1 < len(entries) and entries[position + 1].index not in chosen:
                end = min(end, srt_timestamp_seconds(entries[position + 1].start))
            old_start, old_end = srt_timestamp_seconds(entry.start), srt_timestamp_seconds(entry.end)
            if end - start < 0.1 or (abs(start - old_start) < 0.005 and abs(end - old_end) < 0.005):
                continue
            try:
                self.document.change_time(entry.index, start, end, remember=False)
            except ValueError:
                continue
            changed += 1
        self.document.commit_time_preview(before)
        # 没动的句子说清楚原因，别让人以为功能没生效
        from autoslice.desktop.asr.refine import GUARDED, NO_AUDIO, NO_PAUSE, TOO_LOUD

        counts = {code: 0 for code in (NO_AUDIO, TOO_LOUD, NO_PAUSE, GUARDED)}
        loud = set()
        for position, entry in enumerate(entries):
            code = reasons.get(position)
            if entry.index in chosen and code in counts:
                counts[code] += 1
                if code == TOO_LOUD:
                    loud.add(entry.index)
        labels = {NO_AUDIO: "没有拿到音频", TOO_LOUD: "背景声音太满、分不清人声",
                  NO_PAUSE: "前后没有停顿（话连着说）", GUARDED: "挪了会压到别的句子的字"}
        untouched = "；".join(f"{count} 条{labels[code]}" for code, count in counts.items() if count)
        self._speech_report = counts
        if changed:
            self.model.set_document(self.document)
            self.timeline.update()
            self._edited()
        head = f"已按声音调整 {changed} 条字幕的起止（Ctrl+Z 撤销）" if changed else "起止已经贴着声音了，没有要调的"
        self._show_transient_status(head + (f"。没动的：{untouched}" if untouched else ""), 9000)
        if loud and not foreground:
            choice = QMessageBox.question(
                self, "背景声音太满",
                f"有 {len(loud)} 条字幕附近背景声音太满，分不清人声和停顿，没有自动调整。\n"
                "要先压低背景音乐和底噪（识别设置里的同一个滤镜），再对这些字幕试一次吗？",
            )
            if choice == QMessageBox.StandardButton.Yes:
                self._snap_cues_to_speech(selected_only=False, foreground=True, chosen=loud)

    def _can_merge_selected(self):
        if not self.document or len(self.selection.selected) < 2:
            return False
        positions = [i for i, item in enumerate(self.document.entries)
                     if item.index in self.selection.selected]
        return len(positions) == len(self.selection.selected) and positions == list(
            range(min(positions), max(positions) + 1)
        )

    def _following_cue(self, cue_id):
        order = self._cue_order()
        if cue_id not in order:
            return None
        position = order.index(cue_id)
        return order[position + 1] if position + 1 < len(order) else None

    def _merge_with_next(self, cue_id):
        """右键“与下一条合并”：选中这一句和下一句，再走同一条合并逻辑（一步撤销）。"""

        following = self._following_cue(cue_id)
        if following is None:
            return
        self.selection.choose(cue_id, self._cue_order())
        self.selection.choose(following, self._cue_order(), shift=True)
        self._merge_selected()

    def _edit_cue_text(self, cue_id):
        """右键“编辑文字”：和单击正文一样进入行内编辑。"""

        row = self._row_of_cue(cue_id)
        if row >= 0:
            self._go_to_subtitle_row(row, edit=True)

    def _merge_selected(self):
        if self._saving or not self.document:
            return
        self._commit_editor()
        try:
            new_index = self.document.merge_adjacent(set(self.selection.selected))
        except ValueError as exc:
            self._show_transient_status(str(exc))
            return
        self.selection.choose(new_index, self._cue_order())
        self.selected_index = new_index
        self.model.set_document(self.document)
        self.subtitle_delegate.effects.play(new_index, "flash")
        self._select_cue(new_index)
        self._edited()
        self._show_transient_status("已合并所选字幕")
        self._toast("已合并所选字幕", undo=True)

    def _entry_bounds(self, cue_id=None):
        cue_id = cue_id if cue_id is not None else self.selection.active
        if not self.document or cue_id is None:
            return None
        entry = next((item for item in self.document.entries if item.index == cue_id), None)
        if entry is None:
            return None
        return srt_timestamp_seconds(entry.start), srt_timestamp_seconds(entry.end)

    def _timeline_preview_changed(self, cue_index):
        if not self.document:
            return
        row = next((i for i, item in enumerate(self.model.visible_entries())
                    if item.index == cue_index), None)
        if row is not None:
            self.model.dataChanged.emit(self.model.index(row, 1), self.model.index(row, 1),
                                        [Qt.ItemDataRole.DisplayRole])
        self._update_status()

    def _timeline_changed(self, cue_index):
        self._timeline_preview_changed(cue_index)
        self._edited()

    def _timeline_preview_requested(self, seconds):
        self._seek_to(seconds)

    def _sync_timeline_scroll(self, start: float, span: float, duration: float):
        if not hasattr(self, "timeline_scroll"):
            return
        self._syncing_timeline_scroll = True
        try:
            duration_ms = max(0, int(round(duration * 1000)))
            span_ms = max(1, int(round(min(span, duration) * 1000))) if duration > 0 else 1
            maximum = max(0, duration_ms - span_ms)
            self.timeline_scroll.setRange(0, maximum)
            self.timeline_scroll.setPageStep(span_ms)
            self.timeline_scroll.setValue(max(0, min(maximum, int(round(start * 1000)))))
            self.timeline_scroll.setEnabled(maximum > 0)
            # 只有一屏放不下时才露出滚动细条
            self.timeline_scroll.setVisible(maximum > 0)
            if hasattr(self, "timeline_span"):
                shown = min(span, duration) if duration > 0 else span
                self.timeline_span.setText(f"视野 {shown:.0f} 秒" if shown >= 10 else f"视野 {shown:.1f} 秒")
        finally:
            self._syncing_timeline_scroll = False

    def _timeline_scroll_changed(self, value: int):
        if getattr(self, "_syncing_timeline_scroll", False):
            return
        if hasattr(self, "timeline"):
            self.timeline.set_view_start(value / 1000.0)

    def _timeline_splitter_moved(self, _position, _index):
        sizes = self.vertical_splitter.sizes()
        if not hasattr(self, "timeline") or len(sizes) != 2 or sizes[1] <= 0:
            return
        self._session["timeline_card_height"] = sizes[1]
        self._save_session()
