"""本地表情包素材库。"""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError


SUPPORTED_STICKER_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
DEFAULT_STICKER_ROOT = Path(
    os.environ.get("AUTOCOVER_STICKER_DIR", Path.cwd() / "stickers")
).expanduser().resolve()


@dataclass(frozen=True, slots=True)
class StickerAsset:
    """一张经过验证的表情包素材。"""

    id: str
    name: str
    group: str
    relative_path: str
    width: int
    height: int

    def to_dict(self) -> dict[str, str | int]:
        """返回不包含本地绝对路径的前端数据。"""

        return asdict(self)


class StickerLibrary:
    """只读扫描并安全解析表情包素材。"""

    def __init__(self, root: str | Path = DEFAULT_STICKER_ROOT) -> None:
        self.root = Path(root).expanduser().resolve()
        self._assets: dict[str, StickerAsset] = {}
        self._paths: dict[str, Path] = {}
        self._root_available = False
        self._invalid_count = 0

    def _is_expression_pack(self, relative_path: Path) -> bool:
        """兼容“表情包/主播”和旧版“主播表情包”两种目录结构。"""

        if "表情包" in self.root.name:
            return True
        return any("表情包" in part for part in relative_path.parts[:-1])

    def _group_name(self, relative_path: Path) -> str:
        """返回前端使用的主播分组，不暴露本机绝对路径。"""

        directories = relative_path.parts[:-1]
        if "表情包" in self.root.name:
            return directories[0].strip() if directories else "未分组"

        for index, part in enumerate(directories):
            if "表情包" not in part:
                continue
            if part == "表情包" and index + 1 < len(directories):
                return directories[index + 1].strip() or "未分组"
            return part.strip() or "未分组"
        return "未分组"

    @staticmethod
    def _asset_id(relative_path: Path) -> str:
        normalized = relative_path.as_posix().casefold().encode("utf-8")
        return hashlib.sha256(normalized).hexdigest()[:20]

    def scan(self) -> list[StickerAsset]:
        """重建素材索引；目录不存在时返回空列表。"""

        assets: dict[str, StickerAsset] = {}
        paths: dict[str, Path] = {}
        self._root_available = self.root.is_dir()
        self._invalid_count = 0
        if not self._root_available:
            self._assets = assets
            self._paths = paths
            return []

        for candidate in sorted(self.root.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if not candidate.is_file() or candidate.suffix.casefold() not in SUPPORTED_STICKER_EXTENSIONS:
                continue
            resolved = candidate.resolve()
            try:
                relative_path = resolved.relative_to(self.root)
            except ValueError:
                continue
            if not self._is_expression_pack(relative_path):
                continue
            try:
                with Image.open(resolved) as image:
                    image.verify()
                with Image.open(resolved) as image:
                    width, height = image.size
            except (OSError, UnidentifiedImageError):
                self._invalid_count += 1
                continue
            if width <= 0 or height <= 0:
                self._invalid_count += 1
                continue

            asset_id = self._asset_id(relative_path)
            asset = StickerAsset(
                id=asset_id,
                name=resolved.stem,
                group=self._group_name(relative_path),
                relative_path=relative_path.as_posix(),
                width=width,
                height=height,
            )
            assets[asset_id] = asset
            paths[asset_id] = resolved

        self._assets = assets
        self._paths = paths
        return self.list_assets()

    def list_assets(self) -> list[StickerAsset]:
        """按分组和名称返回素材快照。"""

        return sorted(
            self._assets.values(),
            key=lambda asset: (asset.group.casefold(), asset.name.casefold(), asset.id),
        )

    def summary(self) -> dict[str, Any]:
        """返回不包含根目录路径的扫描摘要。"""

        group_counts = Counter(asset.group for asset in self._assets.values())
        groups = [
            {"name": name, "count": count}
            for name, count in sorted(group_counts.items(), key=lambda item: item[0].casefold())
        ]
        return {
            "available": self._root_available,
            "asset_count": len(self._assets),
            "group_count": len(groups),
            "invalid_count": self._invalid_count,
            "groups": groups,
        }

    def get(self, asset_id: str) -> StickerAsset:
        """按不透明 ID 获取素材元数据。"""

        try:
            return self._assets[asset_id]
        except KeyError as exc:
            raise KeyError("贴图素材不存在或已失效") from exc

    def resolve(self, asset_id: str) -> Path:
        """解析已登记素材；拒绝任意路径和已删除文件。"""

        self.get(asset_id)
        path = self._paths[asset_id]
        if not path.is_file():
            raise FileNotFoundError("贴图素材文件已不存在")
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise KeyError("贴图素材不在允许目录中") from exc
        return path
