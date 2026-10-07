"""AutoCover 导出记录：轻量回看最近导出的封面，并能打开所在文件夹。"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

_THUMB = QSize(96, 72)


def _when(value: object) -> str:
    try:
        return datetime.fromisoformat(str(value)).astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


def reveal_in_folder(path: Path) -> None:
    """在资源管理器里定位文件；文件已不在时只打开所在文件夹。"""

    if os.name == "nt" and path.is_file():
        subprocess.Popen(["explorer.exe", "/select,", str(path)])
        return
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


class CoverExportDialog(QDialog):
    """最新在前；缩略图来自导出的成品本身。"""

    def __init__(self, entries: tuple[dict[str, object], ...], parent=None):
        super().__init__(parent)
        self.setWindowTitle("导出记录")
        self.resize(640, 520)
        layout = QVBoxLayout(self)
        hint = QLabel("最近导出的封面，最新在前；双击打开所在文件夹")
        hint.setObjectName("subtle")
        layout.addWidget(hint)
        self.list = QListWidget()
        self.list.setIconSize(_THUMB)
        self.list.itemDoubleClicked.connect(lambda _item: self._reveal())
        layout.addWidget(self.list, 1)
        for entry in reversed(entries):
            output = Path(str(entry.get("output") or ""))
            ratio = "16:9" if entry.get("canvas_key") == "16x9" else "4:3"
            title = str(entry.get("title") or Path(str(entry.get("video") or "")).stem or "未知项目")
            exists = output.is_file()
            pixmap = QPixmap(str(output)) if exists else QPixmap()
            icon = QIcon(pixmap.scaled(_THUMB, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)) if not pixmap.isNull() else QIcon()
            state = "" if exists else "  （文件已不存在）"
            item = QListWidgetItem(icon, f"{_when(entry.get('timestamp'))}  ·  {ratio}  ·  {output.name}{state}\n{title}")
            item.setData(Qt.ItemDataRole.UserRole, str(output))
            item.setToolTip(str(output))
            self.list.addItem(item)
        if not self.list.count():
            self.list.addItem(QListWidgetItem("还没有导出记录"))
        else:
            self.list.setCurrentRow(0)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.reveal_button = QPushButton("打开所在文件夹")
        self.reveal_button.setEnabled(bool(entries))
        self.reveal_button.clicked.connect(self._reveal)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        actions.addWidget(self.reveal_button)
        actions.addWidget(close)
        layout.addLayout(actions)

    def selected_path(self) -> Path | None:
        item = self.list.currentItem()
        value = item.data(Qt.ItemDataRole.UserRole) if item else None
        return Path(value) if value else None

    def _reveal(self):
        path = self.selected_path()
        if path is not None:
            reveal_in_folder(path)
