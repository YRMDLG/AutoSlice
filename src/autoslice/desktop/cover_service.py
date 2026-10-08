"""AutoCover 的无 UI 服务：按职责拆在 cover_store / cover_frames / cover_autolayout / cover_exporting，这里只组装状态。"""

from __future__ import annotations

import threading

from autoslice.desktop.foundation import DesktopStorage
from autoslice_cover.video import (
    VideoMetadata,
)

from .cover_assets import CoverAssetLibrary
from .cover_autolayout import CoverLayoutService
from .cover_exporting import CoverExportService
from .cover_frames import CoverFrameService
from .cover_store import CoverStoreService
from .cover_style import (
    CoverStyleMemoryStore,
)
from .cover_works import CoverWorks


class CoverService(
    CoverStoreService,
    CoverFrameService,
    CoverLayoutService,
    CoverExportService,
):
    """封面草稿、素材缓存和导出的无 UI 服务。"""

    def __init__(self, storage: DesktopStorage) -> None:
        self.storage = storage
        self.assets = storage.thumbnails / "cover-assets"
        self.previews = storage.thumbnails / "cover-previews"
        self.asset_library = CoverAssetLibrary(storage.root)
        self.style_memory = CoverStyleMemoryStore(storage.root)
        self.works = CoverWorks(storage)
        self.export_history_path = storage.root / "cover-export-history.json"
        # 同一视频只探测一次时长与尺寸；取帧任务会并发调用。
        self._metadata: dict[tuple[str, int, int], VideoMetadata] = {}
        self._metadata_lock = threading.Lock()


