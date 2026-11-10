"""封面常用色板：一键选色、直接输入色号，并记住最近用过的颜色。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from .foundation import DesktopStorage

# 首行放封面常用色，其余保留紧凑色板里的主要色相。
COVER_COLORS = (
    "#FFFFFF", "#000000", "#111111", "#FFE438", "#16D8ED", "#FF80AB", "#F44336", "#BDBDBD",
    "#EC407A", "#AB47BC", "#673AB7", "#3F51B5", "#1976D2", "#039BE5", "#00BCD4", "#26A69A",
    "#4CAF50", "#8BC34A", "#CDDC39", "#FFEB3B", "#FFC107", "#FF9800", "#FF5722", "#B71C1C",
)


class CoverColorMenu(QMenu):
    """普通选色只展示色块；完整调色器放在“更多颜色”中。"""

    color_selected = Signal(str)
    more_requested = Signal()

    def __init__(self, *, allow_none=False, allow_alpha=False, none_label="无", recent_path: Path | None = None, parent=None):
        super().__init__(parent)
        self._allow_alpha = allow_alpha
        self._recent_path = recent_path
        self._recent: list[str] = []
        self._current = ""
        self.swatches: dict[str, QPushButton] = {}
        panel = QWidget(self)
        column = QVBoxLayout(panel)
        column.setContentsMargins(10, 8, 10, 8)
        column.setSpacing(6)
        column.addWidget(QLabel("常用颜色"))
        grid = QGridLayout()
        grid.setSpacing(3)
        for index, color in enumerate(COVER_COLORS):
            button = self._swatch(color)
            grid.addWidget(button, index // 8, index % 8)
            self.swatches[color] = button
        column.addLayout(grid)
        self.recent_panel = QWidget()
        recent_column = QVBoxLayout(self.recent_panel)
        recent_column.setContentsMargins(0, 0, 0, 0)
        recent_column.setSpacing(4)
        recent_column.addWidget(QLabel("最近使用"))
        self.recent_row = QHBoxLayout()
        self.recent_row.setSpacing(3)
        recent_column.addLayout(self.recent_row)
        column.addWidget(self.recent_panel)
        row = QHBoxLayout()
        row.addWidget(QLabel("色号"))
        self.hex_edit = QLineEdit()
        self.hex_edit.setFixedWidth(106 if allow_alpha else 90)
        self.hex_edit.setPlaceholderText("#RRGGBBAA" if allow_alpha else "#RRGGBB")
        self.hex_edit.setMaxLength(9 if allow_alpha else 7)
        self.hex_edit.returnPressed.connect(self._apply_hex)
        row.addWidget(self.hex_edit, 1)
        self.apply_button = QPushButton("使用")
        self.apply_button.setFixedWidth(48)
        self.apply_button.setStyleSheet("padding: 0 4px; min-height: 26px;")
        self.apply_button.clicked.connect(self._apply_hex)
        row.addWidget(self.apply_button)
        column.addLayout(row)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.hide()
        column.addWidget(self.notice)
        action = QWidgetAction(self)
        action.setDefaultWidget(panel)
        self.addAction(action)
        if allow_none:
            self.addAction(none_label, lambda: self._select(""))
        self.addAction("更多颜色…", self.more_requested.emit)
        self.aboutToShow.connect(self._refresh_recent)
        self._refresh_recent()

    def _swatch(self, color: str) -> QPushButton:
        button = QPushButton()
        button.setFixedSize(24, 24)
        button.setToolTip(color)
        button.setAccessibleName(f"选择颜色 {color}")
        button.setStyleSheet(f"background-color: {color[:7]}; border: 1px solid #808080; border-radius: 2px; padding: 0; min-height: 0; min-width: 0;")
        button.clicked.connect(lambda _checked=False: self._select(color))
        return button

    def set_color(self, color: str) -> None:
        self._current = color
        self.hex_edit.setText(color)
        self.notice.hide()
        for value, button in self.swatches.items():
            border = "2px solid #3598FF" if value == color else "1px solid #808080"
            button.setStyleSheet(f"background-color: {value}; border: {border}; border-radius: 2px; padding: 0; min-height: 0; min-width: 0;")

    def _select(self, color: str) -> None:
        self.close()
        self.color_selected.emit(color)

    def _apply_hex(self) -> None:
        value = self.hex_edit.text().strip().upper()
        if not value.startswith("#"):
            value = "#" + value
        if not re.fullmatch(r"#[0-9A-F]{6}(?:[0-9A-F]{2})?" if self._allow_alpha else r"#[0-9A-F]{6}", value):
            self.notice.setText("请输入六位颜色色号" + ("，可附加两位透明度" if self._allow_alpha else ""))
            self.notice.show()
            return
        self._select(value)

    def _refresh_recent(self) -> None:
        if self._recent_path is not None:
            try:
                payload = json.loads(self._recent_path.read_text(encoding="utf-8"))
                raw = payload.get("recent", []) if isinstance(payload, dict) else []
                self._recent = list(dict.fromkeys(
                    value.upper() for value in raw if isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?", value)
                ))[:8] if isinstance(raw, list) else []
            except (OSError, ValueError, UnicodeError):
                self._recent = []
        while self.recent_row.count():
            item = self.recent_row.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        colors = [color for color in self._recent if color not in COVER_COLORS and (self._allow_alpha or len(color) == 7)]
        for color in colors:
            self.recent_row.addWidget(self._swatch(color))
        self.recent_row.addStretch(1)
        self.recent_panel.setVisible(bool(colors))

    def remember(self, color: str) -> None:
        if not color:
            return
        self._refresh_recent()
        self._recent = [color, *(value for value in self._recent if value != color)][:8]
        if self._recent_path is not None:
            try:
                DesktopStorage._write_json(self._recent_path, {"recent": self._recent})
            except OSError:
                pass  # 选色本身仍生效，数据目录暂不可写时不打断编辑。
