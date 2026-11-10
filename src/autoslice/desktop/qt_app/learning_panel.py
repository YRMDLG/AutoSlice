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
from autoslice.desktop.qt_preview.icons import icon as desktop_icon
from autoslice.desktop.qt_preview.theme import COLORS, SIZES
from autoslice.desktop.qt_preview.window import card, label
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
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)

        memory_card, layout = card(
            "字幕纠错记忆",
            "保存校对字幕时学习常见改字，同时统计其它字幕中原样保留的用法。"
            "可靠的记忆作为待确认校对建议，不会自动改写识别结果。只存在本机。",
        )
        self.summary = label("", "muted")
        layout.addWidget(self.summary)
        self.list = QListWidget()
        self.list.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.list)
        self.forget_row = QWidget()
        row = QHBoxLayout(self.forget_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch()
        self.forget_button = QPushButton("忽略所选")
        self.forget_button.setIcon(desktop_icon("trash", COLORS.muted, SIZES.tool_icon_size))
        self.forget_button.setToolTip("忽略这条校对记忆，以后同样的改动不再提供建议")
        self.forget_button.setEnabled(False)
        self.forget_button.clicked.connect(self._forget)
        row.addWidget(self.forget_button)
        layout.addWidget(self.forget_row)
        outer.addWidget(memory_card)

        works_card, works_layout = card(
            "封面作品库",
            "每次导出记下构图、配色、文案和用了哪个方案；快速方案会优先给最近没用过的构图和配色。",
        )
        self.works_summary = label("", "muted")
        self.works_summary.setWordWrap(True)
        works_layout.addWidget(self.works_summary)
        outer.addWidget(works_card)
        self.refresh()

    def refresh(self) -> None:
        rows = self.memory.entries()
        self.list.clear()
        for profile_id, entry in rows:
            state = "已记住 · 仅供校对确认" if entry.promoted else (
                f"观察中：{entry.note}" if entry.note else
                f"其它字幕保留原写法 {entry.kept} 次，继续观察" if entry.kept else "继续积累校对样本"
            )
            text = f"{entry.wrong} → {entry.right}　·　{_profile_label(profile_id)}　·　{entry.videos} 个视频　·　{state}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, (profile_id, entry.wrong, entry.right))
            self.list.addItem(item)
        remembered = sum(1 for _profile, entry in rows if entry.promoted)
        self.summary.setText(
            f"已记住 {remembered} 条，观察中 {len(rows) - remembered} 条" if rows else "还没有学到错词：保存几次校对字幕后会出现在这里。"
        )
        self.list.setVisible(bool(rows))
        self.forget_row.setVisible(bool(rows))
        # 按行数定高，最多约 8 行，超出滚动。
        row_height = self.list.sizeHintForRow(0) if rows else 0
        self.list.setFixedHeight(min(220, row_height * len(rows) + 14))
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
