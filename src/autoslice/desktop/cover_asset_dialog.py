"""完整素材索引的缩略图选择器：搜索全部素材，只解码需要显示的缩略图。"""

from __future__ import annotations

import importlib.util
from collections import OrderedDict

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSize, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QIcon, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QVBoxLayout,
)

from .cover_assets import CoverAsset
from .qt_preview import motion

_THUMB = 96
_SEARCH_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_PINYIN_ROLE = int(Qt.ItemDataRole.UserRole) + 2
_ICON_CACHE_SIZE = 192
_HAS_PINYIN = importlib.util.find_spec("pypinyin") is not None


def pinyin_text(text: str) -> str:
    """名称和分组的拼音全拼与首字母（小写，名称和分组之间留空格）；纯英文或没装 pypinyin 返回空串。"""

    if text.isascii():
        return ""
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return ""
    # errors=list：非汉字（英文、数字、斜杠、空格）逐字符原样保留
    full = "".join(lazy_pinyin(text, errors=list))
    initials = "".join(lazy_pinyin(text, style=Style.FIRST_LETTER, errors=list))
    return f"{full} {initials}".casefold()


class CoverAssetModel(QAbstractListModel):
    """保留全部素材；视图索取图标时才读取图片，缓存有上限。"""

    def __init__(self, assets: tuple[CoverAsset, ...], parent=None):
        super().__init__(parent)
        self.assets = assets
        self._icons: OrderedDict[str, QIcon] = OrderedDict()
        self._pinyin: dict[str, str] = {}  # 只有输入了英文关键词才会用到；对话框打开后在后台分批预热
        self._warm_cursor = 0

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.assets)

    def warm_pinyin(self, count: int = 100) -> bool:
        """分批算好拼音缓存，免得第一次输入英文关键词时卡住界面；返回是否还有没算的。"""

        if not _HAS_PINYIN:
            return False
        end = min(len(self.assets), self._warm_cursor + count)
        for asset in self.assets[self._warm_cursor:end]:
            if asset.asset_id not in self._pinyin:
                self._pinyin[asset.asset_id] = pinyin_text(f"{asset.name} {asset.group}")
        self._warm_cursor = end
        return end < len(self.assets)

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
        if role == _PINYIN_ROLE:
            if asset.asset_id not in self._pinyin:
                self._pinyin[asset.asset_id] = pinyin_text(f"{asset.name} {asset.group}")
            return self._pinyin[asset.asset_id]
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


class AssetFilterModel(QSortFilterProxyModel):
    """空格隔开的多个关键词要同时命中；匹配名称和分组，英文关键词还能匹配拼音全拼和首字母。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._terms: tuple[str, ...] = ()

    def set_query(self, text: str) -> None:
        terms = tuple(text.casefold().split())  # split() 同时处理全角空格和多余空白
        if hasattr(self, "beginFilterChange"):  # Qt 6.10+：invalidateFilter 已弃用，改用成对的 begin/end
            self.beginFilterChange()
            self._terms = terms
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            self._terms = terms
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        if not self._terms:
            return True
        index = self.sourceModel().index(source_row, 0, source_parent)
        text = str(index.data(_SEARCH_ROLE) or "").casefold()
        for term in self._terms:
            if term in text:
                continue
            # 中文关键词不碰拼音；英文关键词没在名称里命中时才去查（结果会缓存）
            if term.isascii() and term in str(index.data(_PINYIN_ROLE) or ""):
                continue
            return False
        return True


class CoverAssetDialog(QDialog):
    """选中一张素材后作为可编辑对象加入画布；双击直接插入。"""

    # exec() 的返回值：用户点了“更换文件夹…”，由调用方选目录后重新打开本对话框（0 取消、1 插入）。
    CHANGE_FOLDER = 2

    def __init__(self, assets: tuple[CoverAsset, ...], parent=None, *, source_directory: str = ""):
        super().__init__(parent)
        self.setWindowTitle("插入素材")
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        hint = QLabel("优先显示当前主播与常用素材；搜索覆盖全部素材，双击插入")
        hint.setObjectName("subtle")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        # 素材范围由所选文件夹决定：把它摆在这里，缺素材时一眼看得出是不是选错了文件夹。
        self.folder_label = QLabel(f"素材文件夹：{source_directory}")
        self.folder_label.setObjectName("subtle")
        self.folder_label.setWordWrap(True)
        self.folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.change_folder = QPushButton("更换文件夹…")
        self.change_folder.clicked.connect(lambda: self.done(self.CHANGE_FOLDER))
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_label, 1)
        folder_row.addWidget(self.change_folder)
        layout.addLayout(folder_row)
        self.folder_label.setVisible(bool(source_directory))
        self.change_folder.setVisible(bool(source_directory))
        self.search = QLineEdit()
        self.search.setPlaceholderText(
            "搜索素材名或分组，多个词用空格隔开" + ("；也可输入拼音（如 zeyin、zy）" if _HAS_PINYIN else "") + "…")
        layout.addWidget(self.search)
        self.asset_model = CoverAssetModel(assets, self)
        self.filtered_model = AssetFilterModel(self)
        self.filtered_model.setSourceModel(self.asset_model)
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
        # 事件循环空闲时一小批一小批地算拼音（5000 个素材一次算完要 1 秒多），界面不卡。
        self.warm_timer = QTimer(self)
        self.warm_timer.timeout.connect(self._warm_pinyin)
        if assets and _HAS_PINYIN:
            self.warm_timer.start(0)
        motion.install_tree(self)

    def _selection_changed(self, *_args) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.selected_asset() is not None)

    def _filter(self, text: str) -> None:
        self.filtered_model.set_query(text)
        count = self.filtered_model.rowCount()
        self.grid.setCurrentIndex(self.filtered_model.index(0, 0) if count else QModelIndex())
        if not self.asset_model.assets:
            self.count_label.setText("这个文件夹（含子文件夹）里没有找到 png / jpg / webp / bmp 素材；点“更换文件夹…”选择素材所在位置")
        else:
            self.count_label.setText(f"共 {len(self.asset_model.assets)} 张素材 · 当前显示 {count} 张")
        self._selection_changed()

    def _warm_pinyin(self) -> None:
        if not self.asset_model.warm_pinyin():
            self.warm_timer.stop()

    def selected_asset(self) -> CoverAsset | None:
        source = self.filtered_model.mapToSource(self.grid.currentIndex())
        return self.asset_model.assets[source.row()] if source.isValid() else None
