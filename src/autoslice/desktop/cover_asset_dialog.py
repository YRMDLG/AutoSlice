"""AutoCover 素材选择：缩略图网格 + 搜索，按最近/常用/当前主播排序。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from .cover_assets import CoverAsset

_THUMB = 96
_MAX_ITEMS = 120


class CoverAssetDialog(QDialog):
    """选中一张素材后作为可编辑对象加入画布；双击直接插入。"""

    def __init__(self, assets: tuple[CoverAsset, ...], parent=None):
        super().__init__(parent)
        self.setWindowTitle("插入素材")
        self.resize(720, 520)
        self._assets = assets[:_MAX_ITEMS]
        layout = QVBoxLayout(self)
        hint = QLabel("按当前主播、最近使用和使用次数排序；双击插入")
        hint.setObjectName("subtle")
        layout.addWidget(hint)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索素材名或分组…")
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid.setIconSize(QSize(_THUMB, _THUMB))
        self.grid.setGridSize(QSize(_THUMB + 24, _THUMB + 36))
        self.grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid.setMovement(QListWidget.Movement.Static)
        self.grid.setWordWrap(True)
        self.grid.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.grid, 1)
        for asset in self._assets:
            pixmap = QPixmap(asset.path)
            if not pixmap.isNull():
                pixmap = pixmap.scaled(
                    _THUMB, _THUMB,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            item = QListWidgetItem(QIcon(pixmap), asset.name)
            item.setToolTip(f"{asset.name}\n{asset.group} · 使用 {asset.usage_count} 次")
            item.setData(Qt.ItemDataRole.UserRole, asset.asset_id)
            self.grid.addItem(item)
        if self.grid.count():
            self.grid.setCurrentRow(0)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("插入")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for row, asset in enumerate(self._assets):
            hidden = bool(needle) and needle not in f"{asset.name} {asset.group}".casefold()
            self.grid.item(row).setHidden(hidden)

    def selected_asset(self) -> CoverAsset | None:
        item = self.grid.currentItem()
        if item is None or item.isHidden():
            return None
        asset_id = item.data(Qt.ItemDataRole.UserRole)
        return next((asset for asset in self._assets if asset.asset_id == asset_id), None)
