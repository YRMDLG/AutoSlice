"""本地表情包素材库。"""

from __future__ import annotations

import hashlib
import io
import re
import warnings
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .paths import DEFAULT_IMPORTED_STICKER_ROOT, DEFAULT_STICKER_ROOT

SUPPORTED_STICKER_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
MAX_STICKER_BYTES = 16_000_000
MAX_STICKER_PIXELS = 20_000_000
MAX_STICKER_WIDTH = 8_192
MAX_STICKER_HEIGHT = 8_192
MAX_STICKER_FRAMES = 8


class StickerValidationError(ValueError):
    """贴图未通过安全解码预算或格式校验。"""


_IMAGE_VALIDATION_ERRORS = (
    Image.DecompressionBombError,
    Image.DecompressionBombWarning,
    UnidentifiedImageError,
    OSError,
    EOFError,
    SyntaxError,
    StickerValidationError,
)


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
        path_authorizer: Callable[[Path], bool] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.import_root = Path(
            import_root or DEFAULT_IMPORTED_STICKER_ROOT
        ).expanduser().resolve()
        self._path_authorizer = path_authorizer or (lambda _path: True)
        if not callable(self._path_authorizer):
            raise TypeError("path_authorizer 必须可调用")
        self._assets: dict[str, StickerAsset] = {}
        self._paths: dict[str, Path] = {}
        self._root_available = False
        self._invalid_count = 0

    @staticmethod
    def _image_source(source: Path | bytes) -> Path | io.BytesIO:
        return io.BytesIO(source) if isinstance(source, bytes) else source

    @staticmethod
    def _validated_image_metadata(image: Image.Image) -> tuple[int, int, int]:
        width, height = image.size
        if (
            width <= 0
            or height <= 0
            or width > MAX_STICKER_WIDTH
            or height > MAX_STICKER_HEIGHT
            or width * height > MAX_STICKER_PIXELS
        ):
            raise StickerValidationError("贴图尺寸超过允许的解码预算")
        try:
            frame_count = int(getattr(image, "n_frames", 1))
        except (TypeError, ValueError):
            raise StickerValidationError("贴图动画帧数无效") from None
        if frame_count < 1 or frame_count > MAX_STICKER_FRAMES:
            raise StickerValidationError("贴图动画帧数超过允许的解码预算")
        return width, height, frame_count

    @classmethod
    def _inspect_image(cls, source: Path | bytes) -> tuple[int, int]:
        """先检查元数据，再解码每个允许的帧，避免只信任图片头。"""

        if isinstance(source, bytes):
            size = len(source)
        else:
            try:
                size = source.stat().st_size
            except OSError as exc:
                raise StickerValidationError("贴图文件不可读取") from exc
        if size <= 0:
            raise StickerValidationError("导入图片为空")
        if size > MAX_STICKER_BYTES:
            raise StickerValidationError("贴图文件超过允许的大小预算")

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(cls._image_source(source)) as image:
                width, height, frame_count = cls._validated_image_metadata(image)
                image.verify()

            with Image.open(cls._image_source(source)) as image:
                checked_width, checked_height, checked_frames = (
                    cls._validated_image_metadata(image)
                )
                if (checked_width, checked_height, checked_frames) != (
                    width,
                    height,
                    frame_count,
                ):
                    raise StickerValidationError("贴图元数据在解码前后不一致")
                for frame_index in range(frame_count):
                    image.seek(frame_index)
                    image.load()
        return width, height

    def _ensure_paths_allowed(self, *paths: Path) -> None:
        for path in paths:
            try:
                allowed = bool(self._path_authorizer(path))
            except Exception:
                allowed = False
            if not allowed:
                raise PermissionError("贴图路径不在当前允许范围内")

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

        self._ensure_paths_allowed(self.root, self.import_root)

        assets: dict[str, StickerAsset] = {}
        paths: dict[str, Path] = {}
        self._root_available = self.root.is_dir() or self.import_root.is_dir()
        self._invalid_count = 0

        root_candidates = self.root.rglob("*") if self.root.is_dir() else ()
        for candidate in sorted(root_candidates, key=lambda item: item.as_posix().casefold()):
            if not candidate.is_file() or candidate.suffix.casefold() not in SUPPORTED_STICKER_EXTENSIONS:
                continue
            resolved = candidate.resolve()
            self._ensure_paths_allowed(resolved)
            try:
                relative_path = resolved.relative_to(self.root)
            except ValueError:
                continue
            if not self._is_expression_pack(relative_path):
                continue
            try:
                width, height = self._inspect_image(resolved)
            except _IMAGE_VALIDATION_ERRORS:
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
                resolved = candidate.resolve()
                self._ensure_paths_allowed(resolved)
                try:
                    width, height = self._inspect_image(resolved)
                except _IMAGE_VALIDATION_ERRORS:
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

    def import_image(self, filename: str, raw: bytes) -> StickerAsset:
        """验证并保存用户手动导入的本地图片。"""

        self._ensure_paths_allowed(self.import_root)

        suffix = Path(str(filename or "")).suffix.casefold()
        if suffix not in SUPPORTED_STICKER_EXTENSIONS:
            raise ValueError("只支持 PNG、JPEG 或 WebP 图片")
        if not raw:
            raise ValueError("导入图片为空")
        try:
            width, height = self._inspect_image(raw)
        except _IMAGE_VALIDATION_ERRORS as exc:
            raise ValueError("导入文件不是有效图片或图片已损坏") from exc

        digest = hashlib.sha256(raw).hexdigest()[:24]
        safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", Path(filename).stem).strip(" ._")
        safe_stem = (safe_stem or "导入图片")[:48]
        self.import_root.mkdir(parents=True, exist_ok=True)
        destination = self.import_root / f"{safe_stem}-{digest[:10]}{suffix}"
        self._ensure_paths_allowed(destination)
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
        self._ensure_paths_allowed(path)
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
        try:
            self._inspect_image(path)
        except _IMAGE_VALIDATION_ERRORS as exc:
            raise ValueError("贴图文件已损坏或超过允许的解码预算") from exc
        return path
