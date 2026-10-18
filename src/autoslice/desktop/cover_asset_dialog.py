"""完整素材索引的缩略图选择器：搜索全部素材，只解码需要显示的缩略图。"""

from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSize, QSortFilterProxyModel, Qt
from PySide6.QtGui import QIcon, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListView,
    QVBoxLayout,
)

from .cover_assets import CoverAsset
from .qt_preview import motion

_THUMB = 96
_SEARCH_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_ICON_CACHE_SIZE = 192


class CoverAssetModel(QAbstractListModel):
    """保留全部素材；视图索取图标时才读取图片，缓存有上限。"""

    def __init__(self, assets: tuple[CoverAsset, ...], parent=None):
        super().__init__(parent)
        self.assets = assets
        self._icons: OrderedDict[str, QIcon] = OrderedDict()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.assets)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.assets):
            return None
        asset = self.assets[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return asset.name
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{asset.name}\n{asset.group} · 使用 {asset.usage_count} 次"
        if role == Qt.ItemDataRole.UserRole:
            return asset.asset_id
        if role == _SEARCH_ROLE:
            return f"{asset.name} {asset.group}"
        if role != Qt.ItemDataRole.DecorationRole:
            return None
        icon = self._icons.get(asset.asset_id)
        if icon is None:
            reader = QImageReader(asset.path)
            size = reader.size()
            if size.isValid():
                reader.setScaledSize(size.scaled(QSize(_THUMB, _THUMB), Qt.AspectRatioMode.KeepAspectRatio))
            icon = QIcon(QPixmap.fromImage(reader.read()))
            self._icons[asset.asset_id] = icon
            if len(self._icons) > _ICON_CACHE_SIZE:
                self._icons.popitem(last=False)
        else:
            self._icons.move_to_end(asset.asset_id)
        return icon


class CoverAssetDialog(QDialog):
    """选中一张素材后作为可编辑对象加入画布；双击直接插入。"""

    def __init__(self, assets: tuple[CoverAsset, ...], parent=None):
        super().__init__(parent)
        self.setWindowTitle("插入素材")
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        hint = QLabel("优先显示当前主播与常用素材；搜索覆盖全部素材，双击插入")
        hint.setObjectName("subtle")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索全部素材名或分组…")
        layout.addWidget(self.search)
        self.asset_model = CoverAssetModel(assets, self)
        self.filtered_model = QSortFilterProxyModel(self)
        self.filtered_model.setSourceModel(self.asset_model)
        self.filtered_model.setFilterRole(_SEARCH_ROLE)
        self.filtered_model.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.grid = QListView()
        self.grid.setModel(self.filtered_model)
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setIconSize(QSize(_THUMB, _THUMB))
        self.grid.setGridSize(QSize(_THUMB + 24, _THUMB + 36))
        self.grid.setUniformItemSizes(True)
        self.grid.setLayoutMode(QListView.LayoutMode.Batched)
        self.grid.setBatchSize(48)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setWordWrap(True)
        self.grid.doubleClicked.connect(lambda _index: self.accept())
        layout.addWidget(self.grid, 1)
        self.count_label = QLabel()
        self.count_label.setObjectName("subtle")
        layout.addWidget(self.count_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("插入")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.search.textChanged.connect(self._filter)
        self.grid.selectionModel().currentChanged.connect(self._selection_changed)
        self._filter("")
        motion.install_tree(self)

    def _selection_changed(self, *_args) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.selected_asset() is not None)

    def _filter(self, text: str) -> None:
        self.filtered_model.setFilterFixedString(text.strip())
        count = self.filtered_model.rowCount()
        self.grid.setCurrentIndex(self.filtered_model.index(0, 0) if count else QModelIndex())
        self.count_label.setText(f"共 {len(self.asset_model.assets)} 张素材 · 当前显示 {count} 张")
        self._selection_changed()

    def selected_asset(self) -> CoverAsset | None:
        source = self.filtered_model.mapToSource(self.grid.currentIndex())
        return self.asset_model.assets[source.row()] if source.isValid() else None
