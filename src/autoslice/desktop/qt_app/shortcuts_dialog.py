"""字幕页快捷键一览（Ctrl+/ 或“⋯”菜单打开）。不模态，可以边看边操作。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QGridLayout, QLabel, QPushButton, QVBoxLayout

from autoslice.desktop.qt_preview.theme import COLORS, SIZES
from autoslice.desktop.qt_preview.window import label

# (分组, [(按键, 作用)])；按键里的“ / ”表示几个键都行
SHORTCUT_GROUPS = (
    ("播放与试听", (
        ("空格 / F8", "播放 / 暂停"),
        ("F1", "试听这一句（前后各多 0.2 秒）"),
        ("F2", "试听句前 0.5 秒"),
        ("F3", "试听句后 0.5 秒"),
        ("F4", "只播这一句"),
    )),
    ("改时间轴", (
        ("Q", "左边裁到定位线"),
        ("W", "右边裁到定位线"),
        ("Ctrl+B", "在定位线拆成两句"),
        ("Delete", "删除选中的字幕"),
        ("Ctrl+Z / Ctrl+Y", "撤销 / 重做"),
        ("Ctrl+S", "保存校对字幕"),
    )),
    ("AI 建议", (
        ("Alt+A", "采纳当前建议"),
        ("Alt+S", "跳过当前建议"),
    )),
    ("鼠标", (
        ("拖动空白", "框选多句"),
        ("滚轮 / 侧键", "左右平移时间轴"),
        ("Ctrl+滚轮", "缩放时间轴"),
        ("拖动时按住 Alt", "临时关闭磁吸"),
    )),
    ("其它", (
        ("Esc", "结束改字 / 取消选中"),
        ("Ctrl+/", "打开这张表"),
    )),
)


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("快捷键")
        self.setModal(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(12)
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        row = 0
        for group, items in SHORTCUT_GROUPS:
            title = label(group, "cardTitle")
            if row:
                title.setContentsMargins(0, 8, 0, 0)
            grid.addWidget(title, row, 0, 1, 2)
            row += 1
            for keys, action in items:
                key = QLabel(keys)
                key.setStyleSheet(
                    f"color: {COLORS.text}; background: {COLORS.overlay}; border: 1px solid {COLORS.border};"
                    f" border-radius: {SIZES.control_radius}px; padding: 2px 8px;"
                )
                grid.addWidget(key, row, 0, Qt.AlignmentFlag.AlignLeft)
                grid.addWidget(label(action, "muted"), row, 1)
                row += 1
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        note = label("正在改字时，空格、Delete、Q、W、Ctrl+B 照常打字，不触发快捷键。", "subtle")
        note.setWordWrap(True)
        layout.addWidget(note)
        close = QPushButton("知道了")
        close.clicked.connect(self.close)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
