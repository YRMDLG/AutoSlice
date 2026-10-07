"""输入分发：窗口事件过滤、焦点与快捷键作用范围、Esc 退出、时间轴边缘平移。

混入 DesktopWindow，排在 PreviewWindow 之前；状态都在窗口上，不单独实例化。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEvent,
    Qt,
)
from PySide6.QtWidgets import QPlainTextEdit


class InputRoutingMixin:
    def _is_timeline_input_widget(self, widget) -> bool:
        current = widget
        timeline_scroll = getattr(self, "timeline_scroll", None)
        while current is not None:
            if current is getattr(self, "timeline", None) or current is timeline_scroll:
                return True
            current = current.parentWidget() if hasattr(current, "parentWidget") else None
        return False

    def _timeline_side_button_direction(self, button):
        if button == Qt.MouseButton.BackButton:
            return -1
        if button == Qt.MouseButton.ForwardButton:
            return 1
        return 0

    def _stop_timeline_side_pan(self, event_type=None):
        controller = getattr(self, "_timeline_side_pan", None)
        if controller is not None:
            if event_type is None:
                controller.stop()
            else:
                controller.stop_for_event(event_type)

    def event(self, event):
        if event.type() in (QEvent.Type.WindowDeactivate, QEvent.Type.Hide,
                            QEvent.Type.Close):
            self._stop_timeline_side_pan(event.type())
        return super().event(event)

    def _focus_changed(self, _old, now):
        # 不用 focus loss 自动提交：同一编辑单元格内部的鼠标点击在 Qt 中可能
        # 先短暂触发 focus 迁移，若此时提交会让用户无法二次点击定位 caret。
        # 真正的“点到编辑器外部就结束编辑”由全局 MouseButtonPress 过滤器负责。
        self._sync_shortcuts(now)

    def _sync_shortcuts(self, focus=None):
        """字幕快捷键只在字幕页生效；封面页的 Delete、撤销等交给封面编辑器。"""

        if not getattr(self, "_shortcuts", None):
            return
        subtitle_page = self.pages.currentIndex() == 0
        editing = self._is_subtitle_editor_widget(focus) or self._subtitle_editor_open()
        for key, shortcut in self._shortcuts.items():
            text_sensitive = key in ("Space", "Delete", "Q", "W", "Ctrl+B")
            shortcut.setEnabled(subtitle_page and not (text_sensitive and editing))

    def _escape_context(self):
        if self._subtitle_editor_open():
            self._commit_editor()
            self.table.setFocus()
            return
        self.selection.clear()
        self.selected_index = None
        self.timeline.set_selection(None, set(), center=False)
        self.table.clearSelection()

    def eventFilter(self, watched, event):
        if getattr(self, "_event_filter_busy", False):
            return False
        self._event_filter_busy = True
        try:
            return self._event_filter_impl(watched, event)
        finally:
            self._event_filter_busy = False

    def _event_filter_impl(self, watched, event):
        if self._subtitle_editor_open() and self._handle_inline_editor_mouse(watched, event):
            return True
        if (event.type() == QEvent.Type.MouseButtonPress
                and self._subtitle_editor_open()):
            watched_inside = self._is_subtitle_editor_widget(watched)
            geometry_inside = self._event_hits_inline_editor(event)
            if watched_inside or geometry_inside:
                # 事件源本身属于 editor 时优先相信控件层级，避免高 DPI / 坐标换算
                # 在边缘出现 1px 误判，导致“点文字却退出编辑”的反复横跳。
                if not watched_inside:
                    self._place_caret_from_global(event)
                    event.accept()
                    return True
            else:
                # 只有事件源和屏幕坐标都确认在 editor 外部才退出。
                self._commit_editor()
        if event.type() == QEvent.Type.MouseButtonPress:
            direction = self._timeline_side_button_direction(event.button())
            if direction and self._is_timeline_input_widget(watched):
                self._timeline_side_pan.start(direction)
                event.accept()
                return True
        if event.type() == QEvent.Type.MouseButtonRelease:
            # 释放可能发生在时间轴外部；只要当前确实在连续平移，就应立即停下。
            direction = self._timeline_side_button_direction(event.button())
            if direction and getattr(self._timeline_side_pan, "direction", 0):
                self._timeline_side_pan.stop()
                event.accept()
                return True
        if watched is self.table.viewport() and event.type() == QEvent.Type.MouseButtonPress \
                and event.button() == Qt.MouseButton.LeftButton:
            index = self.table.indexAt(event.position().toPoint())
            if index.isValid() and index.column() == 2 and self.document is not None:
                self._pending_caret_click = (
                    index.row(),
                    index.column(),
                    event.position().toPoint(),
                )
            else:
                self._pending_caret_click = None
        if isinstance(watched, QPlainTextEdit) and event.type() == QEvent.Type.KeyPress:
            if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                    and not event.modifiers()):
                self._commit_editor()
                self.table.setFocus()
                return True
            if (event.key() == Qt.Key.Key_Z
                    and event.modifiers() == Qt.KeyboardModifier.ControlModifier):
                self._undo()
                return True
            if (event.key() == Qt.Key.Key_Y
                    and event.modifiers() == Qt.KeyboardModifier.ControlModifier):
                self._redo()
                return True
        return super().eventFilter(watched, event)
