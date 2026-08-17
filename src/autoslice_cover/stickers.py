"""本地表情包素材库。"""

from __future__ import annotations

import hashlib
import io
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .paths import DEFAULT_IMPORTED_STICKER_ROOT, DEFAULT_STICKER_ROOT

SUPPORTED_STICKER_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})


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

    def __init__(
        self,
        root: str | Path = DEFAULT_STICKER_ROOT,
        *,
        import_root: str | Path | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.import_root = Path(
            import_root or DEFAULT_IMPORTED_STICKER_ROOT
        ).expanduser().resolve()
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
        self._root_available = self.root.is_dir() or self.import_root.is_dir()
        self._invalid_count = 0

        root_candidates = self.root.rglob("*") if self.root.is_dir() else ()
        for candidate in sorted(root_candidates, key=lambda item: item.as_posix().casefold()):
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

        if self.import_root.is_dir():
            for candidate in sorted(
                self.import_root.glob("*"),
                key=lambda item: item.name.casefold(),
            ):
                if (
                    not candidate.is_file()
                    or candidate.suffix.casefold() not in SUPPORTED_STICKER_EXTENSIONS
                ):
                    continue
                try:
                    with Image.open(candidate) as image:
                        image.verify()
                    with Image.open(candidate) as image:
                        width, height = image.size
                except (OSError, UnidentifiedImageError):
                    self._invalid_count += 1
                    continue
                relative_path = Path("我的导入") / candidate.name
                asset_id = self._asset_id(relative_path)
                assets[asset_id] = StickerAsset(
                    id=asset_id,
                    name=candidate.stem,
                    group="我的导入",
                    relative_path=relative_path.as_posix(),
                    width=width,
                    height=height,
                )
                paths[asset_id] = candidate.resolve()

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

    def import_image(self, filename: str, raw: bytes) -> StickerAsset:
        """验证并保存用户手动导入的本地图片。"""

        suffix = Path(str(filename or "")).suffix.casefold()
        if suffix not in SUPPORTED_STICKER_EXTENSIONS:
            raise ValueError("只支持 PNG、JPEG 或 WebP 图片")
        if not raw:
            raise ValueError("导入图片为空")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
        except (OSError, UnidentifiedImageError) as exc:
            raise ValueError("导入文件不是有效图片或图片已损坏") from exc
        if width <= 0 or height <= 0:
            raise ValueError("导入图片尺寸无效")

        digest = hashlib.sha256(raw).hexdigest()[:24]
        safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", Path(filename).stem).strip(" ._")
        safe_stem = (safe_stem or "导入图片")[:48]
        self.import_root.mkdir(parents=True, exist_ok=True)
        destination = self.import_root / f"{safe_stem}-{digest[:10]}{suffix}"
        if not destination.is_file():
            temporary = self.import_root / f".{destination.name}.tmp"
            try:
                temporary.write_bytes(raw)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        self.scan()
        return self.get(self._asset_id(Path("我的导入") / destination.name))

    def resolve(self, asset_id: str) -> Path:
        """解析已登记素材；拒绝任意路径和已删除文件。"""

        self.get(asset_id)
        path = self._paths[asset_id]
        if not path.is_file():
            raise FileNotFoundError("贴图素材文件已不存在")
        allowed = False
        for root in (self.root, self.import_root):
            try:
                path.relative_to(root)
                allowed = True
                break
            except ValueError:
                continue
        if not allowed:
            raise KeyError("贴图素材不在允许目录中")
        return path
