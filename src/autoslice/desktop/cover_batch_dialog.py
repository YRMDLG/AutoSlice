"""AutoCover 批量出图：勾选视频，逐个自动选帧、排版并导出 4:3 + 16:9。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from autoslice.desktop.projects import ProjectVideo, SubmissionProject


@dataclass(frozen=True)
class BatchTarget:
    """一条待出图的视频及其当前状态。"""

    project: SubmissionProject
    video: ProjectVideo
    exported: bool
    has_draft: bool

    @property
    def label(self) -> str:
        name = self.project.title
        if len(self.project.videos) > 1:
            name += f" · {self.video.name}"
        return name

    @property
    def status(self) -> str:
        if self.exported:
            return "已导出过"
        return "有草稿，按草稿导出" if self.has_draft else "新，自动选帧排版"


# run(action, callback)：后台执行 action，完成后在界面线程回调 callback(result, error)。
Runner = Callable[[Callable[[], object], Callable[[object, object], None]], None]


class CoverBatchDialog(QDialog):
    """逐个处理，处理中可停止；每个视频导出两张，不覆盖已有文件。"""

    finished_batch = Signal(int, int)

    def __init__(self, targets: tuple[BatchTarget, ...], action: Callable[[SubmissionProject, ProjectVideo], object],
                 run: Runner, parent=None):
        super().__init__(parent)
        self.setWindowTitle("批量出封面")
        self.resize(620, 520)
        self._targets = targets
        self._action = action
        self._run = run
        self._queue: list[int] = []
        self._running = False
        self._stopping = False
        self._done = 0
        self._failed = 0
        layout = QVBoxLayout(self)
        hint = QLabel(
            "有草稿的按草稿导出；没有草稿的从全片挑画面、自动排版后导出，并存成草稿方便回头再改。"
            "每个视频导出 4:3 和 16:9 两张，不覆盖已有文件。"
        )
        hint.setObjectName("subtle")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.list = QListWidget()
        self.list.setWordWrap(True)
        for target in targets:
            item = QListWidgetItem(f"{target.label}    —  {target.status}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if target.exported else Qt.CheckState.Checked)
            item.setToolTip(target.video.path)
            self.list.addItem(item)
        self.list.itemChanged.connect(lambda _item: self._update_summary())
        layout.addWidget(self.list, 1)
        picks = QHBoxLayout()
        self.all_button = QPushButton("全选")
        self.all_button.setObjectName("quiet")
        self.all_button.clicked.connect(lambda: self._check(lambda _target: True))
        self.new_button = QPushButton("只选未导出")
        self.new_button.setObjectName("quiet")
        self.new_button.clicked.connect(lambda: self._check(lambda target: not target.exported))
        self.none_button = QPushButton("全不选")
        self.none_button.setObjectName("quiet")
        self.none_button.clicked.connect(lambda: self._check(lambda _target: False))
        for button in (self.all_button, self.new_button, self.none_button):
            picks.addWidget(button)
        picks.addStretch(1)
        layout.addLayout(picks)
        self.progress = QLabel("")
        self.progress.setObjectName("subtle")
        layout.addWidget(self.progress)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.start_button = QPushButton("开始出图")
        self.start_button.setObjectName("primary")
        self.start_button.setMinimumWidth(96)
        self.start_button.clicked.connect(self._start_or_stop)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.reject)
        actions.addWidget(self.start_button)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        self._update_summary()

    def _check(self, predicate: Callable[[BatchTarget], bool]):
        for index, target in enumerate(self._targets):
            state = Qt.CheckState.Checked if predicate(target) else Qt.CheckState.Unchecked
            self.list.item(index).setCheckState(state)

    def checked_indices(self) -> list[int]:
        return [index for index in range(self.list.count()) if self.list.item(index).checkState() == Qt.CheckState.Checked]

    def _update_summary(self):
        if self._running:
            return
        count = len(self.checked_indices())
        self.start_button.setEnabled(count > 0)
        self.progress.setText(f"已选 {count} 个视频，将导出 {count * 2} 张封面")

    def _start_or_stop(self):
        if self._running:
            # 当前这个做完再停，不中断正在写的文件。
            self._stopping = True
            self.start_button.setEnabled(False)
            self.progress.setText("正在停止：当前这个完成后结束…")
            return
        self._queue = self.checked_indices()
        if not self._queue:
            return
        self._running = True
        self._stopping = False
        self._done = self._failed = 0
        self.start_button.setText("停止")
        for widget in (self.list, self.all_button, self.new_button, self.none_button, self.close_button):
            widget.setEnabled(False)
        self._next()

    def _next(self):
        if not self._queue or self._stopping:
            self._finish()
            return
        index = self._queue.pop(0)
        target = self._targets[index]
        total = self._done + self._failed + len(self._queue) + 1
        self.progress.setText(f"{self._done + self._failed + 1}/{total} 正在处理：{target.label}")
        self.list.item(index).setText(f"{target.label}    —  处理中…")
        self.list.scrollToItem(self.list.item(index))
        self._run(
            lambda: self._action(target.project, target.video),
            lambda result, error, value=index: self._item_done(value, result, error),
        )

    def _item_done(self, index: int, result, error):
        target = self._targets[index]
        item = self.list.item(index)
        if error:
            self._failed += 1
            item.setText(f"{target.label}    —  ✗ 失败：{error}")
        else:
            self._done += 1
            paths = tuple(result or ())
            item.setText(f"{target.label}    —  ✓ 已导出 {len(paths)} 张")
            item.setToolTip("\n".join(str(path) for path in paths))
            item.setCheckState(Qt.CheckState.Unchecked)
        self._next()

    def _finish(self):
        self._running = False
        self.start_button.setText("开始出图")
        for widget in (self.list, self.all_button, self.new_button, self.none_button, self.close_button):
            widget.setEnabled(True)
        stopped = "（已停止）" if self._stopping and self._queue else ""
        self.progress.setText(f"完成 {self._done} 个，失败 {self._failed} 个{stopped}；封面在各项目目录里")
        self.start_button.setEnabled(bool(self.checked_indices()))
        self.finished_batch.emit(self._done, self._failed)

    def reject(self):
        if self._running:
            return
        super().reject()
