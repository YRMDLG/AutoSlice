"""设置页“成长记录”：工具从日常操作里学到了什么，看得见、也能撤回。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.correction_memory import CorrectionMemory
from autoslice.desktop.cover_works import CoverWorks, describe_composition, describe_scheme
from autoslice.desktop.qt_preview.window import label
from autoslice.streamer_profiles import resolve_streamer_profile


def _profile_label(profile_id: str) -> str:
    try:
        return resolve_streamer_profile(profile_id).label
    except ValueError:
        return profile_id


class LearningPanel(QWidget):
    def __init__(self, memory: CorrectionMemory, works: CoverWorks | None = None, parent=None):
        super().__init__(parent)
        self.memory = memory
        self.works = works
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(label("字幕纠错记忆", "sectionTitle"))
        help_text = label(
            "保存校对字幕时自动学习：同一个错词在两个视频里都被你改过，就记住它。"
            "之后 AI 检查会把它列成建议，仍由你决定是否采纳。只存在本机。", "muted",
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.summary = label("", "muted")
        layout.addWidget(self.summary)
        self.list = QListWidget()
        self.list.setMaximumHeight(220)
        self.list.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.list)
        row = QHBoxLayout()
        row.addStretch()
        self.forget_button = QPushButton("忽略所选")
        self.forget_button.setObjectName("quiet")
        self.forget_button.setFixedHeight(28)
        self.forget_button.setToolTip("从词库删除，以后同样的改动也不再自动记住")
        self.forget_button.setEnabled(False)
        self.forget_button.clicked.connect(self._forget)
        row.addWidget(self.forget_button)
        layout.addLayout(row)
        layout.addSpacing(6)
        layout.addWidget(label("封面作品库", "sectionTitle"))
        works_help = label(
            "每次导出记下构图、配色、文案和用了哪个方案；快速方案会优先给最近没用过的构图和配色。", "muted",
        )
        works_help.setWordWrap(True)
        layout.addWidget(works_help)
        self.works_summary = label("", "muted")
        self.works_summary.setWordWrap(True)
        layout.addWidget(self.works_summary)
        self.refresh()

    def refresh(self) -> None:
        rows = self.memory.entries()
        self.list.clear()
        for profile_id, entry in rows:
            state = "已记住" if entry.promoted else (f"未记住：{entry.note}" if entry.note else "再改一次就记住")
            text = f"{entry.wrong} → {entry.right}　·　{_profile_label(profile_id)}　·　{entry.videos} 个视频　·　{state}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, (profile_id, entry.wrong, entry.right))
            self.list.addItem(item)
        remembered = sum(1 for _profile, entry in rows if entry.promoted)
        self.summary.setText(
            f"已记住 {remembered} 条，观察中 {len(rows) - remembered} 条" if rows else "还没有学到错词：保存几次校对字幕后会出现在这里。"
        )
        self.list.setVisible(bool(rows))
        self._selection_changed()
        self._refresh_works()

    def _refresh_works(self) -> None:
        summary = self.works.summary() if self.works is not None else {"count": 0}
        if not summary["count"]:
            self.works_summary.setText("还没有作品：导出封面后会记在这里。")
            return
        lines = [f"已记录 {summary['count']} 张封面。"]
        if summary["compositions"]:
            lines.append("常用构图：" + "；".join(
                f"{describe_composition(key)}（{count} 张）" for key, count in summary["compositions"]
            ))
        if summary["schemes"]:
            lines.append("常用方案：" + "、".join(
                f"{describe_scheme(key)} {count} 次" for key, count in summary["schemes"]
            ))
        self.works_summary.setText("\n".join(lines))

    def _selection_changed(self) -> None:
        self.forget_button.setEnabled(bool(self.list.selectedItems()))

    def _forget(self) -> None:
        for item in self.list.selectedItems():
            profile_id, wrong, right = item.data(Qt.ItemDataRole.UserRole)
            self.memory.forget(profile_id, wrong, right)
        self.refresh()
