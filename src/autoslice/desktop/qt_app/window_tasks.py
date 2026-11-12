"""窗口内统一查看识别、字幕检查、压制和话题分析任务。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)


class TaskPanelMixin:
    def _task_update(self, key, *, kind=None, project=None, video_path=None,
                     state="running", message="", cancel=None, result=None, result_label=None):
        if not hasattr(self, "_task_records"):
            self._task_records = {}
        record = self._task_records.setdefault(key, {})
        if kind is not None:
            record["kind"] = kind
        if project is not None:
            record.update(project_id=project.id, title=project.title)
        if video_path is not None:
            record["video_path"] = video_path
        record.update(state=state, message=message)
        if cancel is not None:
            # 新一轮开始：上次的结果不再对应
            record["cancel"] = cancel
            record.pop("result", None)
            record.pop("result_label", None)
        if result is not None:
            record.update(result=result, result_label=result_label or "打开结果")
        if state != "running":
            record.pop("cancel", None)
        while len(self._task_records) > 30:
            removable = next((name for name, item in self._task_records.items()
                              if item["state"] != "running" and name != key), None)
            if removable is None:
                break
            del self._task_records[removable]
        self._refresh_task_panel()

    def _show_tasks(self):
        if not hasattr(self, "task_dialog"):
            self.task_dialog = QDialog(self)
            self.task_dialog.setWindowTitle("后台任务")
            self.task_dialog.resize(660, 370)
            layout = QVBoxLayout(self.task_dialog)
            title = QLabel("可查看所属视频、打开结果，或停止正在运行的任务。")
            title.setWordWrap(True)
            layout.addWidget(title)
            self.task_list = QListWidget()
            self.task_list.currentRowChanged.connect(self._task_selection_changed)
            self.task_list.itemActivated.connect(lambda _item: self._locate_task())
            layout.addWidget(self.task_list, 1)
            self.task_detail = QLabel()
            self.task_detail.setWordWrap(True)
            layout.addWidget(self.task_detail)
            row = QHBoxLayout()
            row.addStretch()  # 按钮靠右、按内容宽，不再撑满整行
            self.task_locate = QPushButton("查看所属视频")
            self.task_locate.clicked.connect(self._locate_task)
            row.addWidget(self.task_locate)
            self.task_open = QPushButton("打开结果")
            self.task_open.clicked.connect(self._open_task_result)
            row.addWidget(self.task_open)
            self.task_cancel = QPushButton("停止任务")
            self.task_cancel.clicked.connect(self._cancel_task)
            row.addWidget(self.task_cancel)
            layout.addLayout(row)
        self._refresh_task_panel()
        self.task_dialog.show()
        self.task_dialog.raise_()

    def _refresh_task_panel(self):
        records = getattr(self, "_task_records", {})
        running = sum(item["state"] == "running" for item in records.values())
        if hasattr(self, "tasks_button"):
            self.tasks_button.setText(f"任务 {running}" if running else "任务")
        if not hasattr(self, "task_list"):
            return
        current = self.task_list.currentItem()
        key = current.data(Qt.ItemDataRole.UserRole) if current else None
        self.task_list.blockSignals(True)
        self.task_list.clear()
        states = {"running": "进行中", "done": "已完成", "error": "失败", "cancelled": "已停止"}
        for name, record in reversed(tuple(records.items())):
            video = Path(record["video_path"]).name if record.get("video_path") else ""
            text = f"{record.get('kind', '任务')} · {record.get('title', '')} · {states[record['state']]}\n{video} · {record['message']}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setToolTip(text)
            self.task_list.addItem(item)
            if name == key:
                self.task_list.setCurrentItem(item)
        self.task_list.blockSignals(False)
        if self.task_list.currentRow() < 0 and self.task_list.count():
            self.task_list.setCurrentRow(0)
        self._task_selection_changed()

    def _selected_task(self):
        item = self.task_list.currentItem()
        return self._task_records.get(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _task_selection_changed(self, _row=None):
        record = self._selected_task()
        self.task_locate.setEnabled(bool(record and record.get("video_path")))
        self.task_open.setEnabled(bool(record and record.get("result")))
        self.task_open.setText(record.get("result_label", "打开结果") if record else "打开结果")
        self.task_cancel.setEnabled(bool(record and record.get("cancel")))
        self.task_detail.setText(record["message"] if record else "当前没有后台任务。")

    def _open_task_video(self, project_id, video_path):
        if self._loading or self._saving:
            self._show_transient_status("当前字幕正在加载或保存，请稍后再查看任务视频。")
            return False
        project = next((p for p in self.service.snapshot.projects if p.id == project_id), None)
        if project is None:
            self._show_transient_status("所属项目已不在当前投稿目录，请重新选择目录后查看。")
            return False
        self._select_real_project(project, preferred_video=video_path)
        if self.project is None or self.project.id != project_id:
            return False
        index = next((i for i, video in enumerate(project.videos) if video.path == video_path), None)
        if index is not None and self.video_choice.currentIndex() != index:
            self.video_choice.setCurrentIndex(index)
        current = self.video_choice.currentData()
        if current is None or current.path != video_path:
            return False
        self._select_page(0)
        return True

    def _locate_task(self):
        record = self._selected_task()
        if record:
            self._open_task_video(record.get("project_id"), record.get("video_path"))

    def _open_task_result(self):
        record = self._selected_task()
        if record and record.get("result"):
            self._open_local_path(record["result"])

    def _open_local_path(self, path):
        """用系统默认程序打开；打不开（比如 .md 没关联程序）就打开所在文件夹。"""

        target = Path(path)
        if not target.exists():
            self._show_transient_status(f"找不到：{target}", 6000)
            return False
        if QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
            return True
        if target.is_file() and QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.parent))):
            return True
        self._show_transient_status(f"没能打开，请手动查看：{target}", 9000)
        return False

    def _cancel_task(self):
        record = self._selected_task()
        if record and record.get("cancel"):
            record["cancel"]()
            record["message"] = "正在停止任务…"
            self._refresh_task_panel()
