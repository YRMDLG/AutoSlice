"""AutoCover vNext 的轻量素材索引与使用记忆。

素材库只负责索引和安全导入，画布对象仍由 ``CoverDocument`` 保存。使用次数、
最近使用和最终导出保留标记写在私有应用数据目录，不写入投稿项目。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from autoslice import runtime_config
from autoslice.desktop.foundation import DesktopStorage
from autoslice_cover.paths import DEFAULT_STICKER_ROOT

SUPPORTED_ASSET_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})


@dataclass(frozen=True, slots=True)
class CoverAsset:
    asset_id: str
    name: str
    path: str
    group: str = "未分组"
    usage_count: int = 0
    last_used_at: str | None = None
    final_export_count: int = 0

    def to_payload(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "name": self.name,
            "path": self.path,
            "group": self.group,
            "usage_count": self.usage_count,
            "last_used_at": self.last_used_at,
            "final_export_count": self.final_export_count,
        }


class CoverAssetLibrary:
    """按需扫描旧贴图库和本地导入目录。"""

    def __init__(
        self, storage_root: Path | str, *, legacy_root: Path | str | None = None,
        legacy_import_root: Path | str | None = None,
    ) -> None:
        self.root = Path(storage_root).expanduser().resolve() / "cover-assets"
        self.import_root = self.root / "imports"
        self.settings_path = self.root / "settings.json"
        try:
            settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
        selected = settings.get("source_directory") if isinstance(settings, dict) else None
        selected = selected.strip() if isinstance(selected, str) else ""
        configured = (
            os.environ.get("AUTOSLICE_STICKER_ROOT", "").strip()
            or os.environ.get("AUTOCOVER_STICKER_DIR", "").strip()
            or runtime_config.LOCAL_ENVIRONMENT.get("AUTOCOVER_STICKER_DIR", "")
        )
        self.legacy_root = Path(selected or legacy_root or configured or DEFAULT_STICKER_ROOT).expanduser().resolve()
        imported = (
            os.environ.get("AUTOCOVER_USER_ASSET_DIR", "").strip()
            or runtime_config.LOCAL_ENVIRONMENT.get("AUTOCOVER_USER_ASSET_DIR", "")
        )
        # 旧网页导入目录必须显式接入；用户选过素材文件夹后，以所选范围为准。
        imported = (legacy_import_root or imported) if not selected else None
        self.legacy_import_root = Path(imported).expanduser().resolve() if imported else None
        self.index_path = self.root / "index.json"
        self._assets: dict[str, CoverAsset] = {}
        self._loaded = False

    def set_source_directory(self, source: Path | str) -> Path:
        """设置可控的素材范围；不复制、删除或改写素材文件。"""

        directory = Path(source).expanduser().resolve()
        if not directory.is_dir():
            raise ValueError("请选择存在的素材文件夹")
        DesktopStorage._write_json(self.settings_path, {"source_directory": str(directory)})
        self.legacy_root = directory
        self.legacy_import_root = None
        self._loaded = False
        return directory

    def _read_index(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return {}
        assets = value.get("assets") if isinstance(value, dict) else None
        return assets if isinstance(assets, dict) else {}

    def _write_index(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "assets": {key: item.to_payload() for key, item in self._assets.items()}}
        temporary = self.index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.index_path)

    def scan(self) -> tuple[CoverAsset, ...]:
        existing = self._read_index()
        found: dict[str, CoverAsset] = {}
        roots = [(self.legacy_root, "文件夹素材"), (self.import_root, "我的导入")]
        if self.legacy_import_root is not None:
            roots.append((self.legacy_import_root, "旧版导入"))
        self.import_root.mkdir(parents=True, exist_ok=True)
        for root, group in roots:
            if not root.is_dir():
                continue
            root = root.resolve()
            candidates = root.rglob("*")
            for path in candidates:
                if not path.is_file() or path.suffix.casefold() not in SUPPORTED_ASSET_EXTENSIONS:
                    continue
                try:
                    resolved = path.resolve()
                    resolved.relative_to(root)
                    key = str(resolved).casefold()
                except (OSError, ValueError):
                    continue
                import hashlib
                asset_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
                if asset_id in found:
                    continue
                old = existing.get(asset_id) if isinstance(existing.get(asset_id), dict) else {}
                found[asset_id] = CoverAsset(
                    asset_id=asset_id,
                    name=path.stem,
                    path=str(resolved),
                    group=(
                        str(Path(root.name) / path.relative_to(root).parent).replace("\\", "/")
                        if group == "文件夹素材" else group
                    ),
                    usage_count=max(0, int(old.get("usage_count", 0))),
                    last_used_at=old.get("last_used_at") if isinstance(old.get("last_used_at"), str) else None,
                    final_export_count=max(0, int(old.get("final_export_count", 0))),
                )
        self._assets = found
        self._loaded = True
        self._write_index()
        return self.list_assets()

    def list_assets(self, *, group: str | None = None, preferred_group: str | None = None) -> tuple[CoverAsset, ...]:
        if not self._loaded:
            self.scan()
        values = [item for item in self._assets.values() if group is None or item.group == group]
        preferred = (preferred_group or "").casefold()
        return tuple(sorted(values, key=lambda item: (
            0 if preferred and preferred in item.group.casefold() else 1,
            # 导出时还保留着的素材比只点过一次更能代表习惯。
            -item.final_export_count,
            -item.usage_count,
            item.name.casefold(),
        )))

    def recent_assets(self, limit: int = 6) -> tuple[CoverAsset, ...]:
        """最近用过的素材，最新在前；素材菜单一键插入用。"""

        if not self._loaded:
            self.scan()
        used = [item for item in self._assets.values() if item.last_used_at and Path(item.path).is_file()]
        used.sort(key=lambda item: (item.last_used_at or "", item.usage_count), reverse=True)
        return tuple(used[: max(0, int(limit))])

    def get(self, asset_id: str) -> CoverAsset:
        if not self._loaded:
            self.scan()
        try:
            return self._assets[asset_id]
        except KeyError as exc:
            raise KeyError("素材不存在或已移除") from exc

    def import_file(self, source: Path | str) -> CoverAsset:
        path = Path(source).expanduser().resolve()
        if not path.is_file() or path.suffix.casefold() not in SUPPORTED_ASSET_EXTENSIONS:
            raise ValueError("素材只支持 PNG、JPG、WEBP 或 BMP")
        self.import_root.mkdir(parents=True, exist_ok=True)
        destination = self.import_root / path.name
        if destination.resolve() != path:
            shutil.copy2(path, destination)
        self.scan()
        asset = next((item for item in self._assets.values() if Path(item.path).resolve() == destination.resolve()), None)
        if asset is None:
            raise ValueError("导入素材后无法建立索引")
        return asset

    def mark_used(self, asset_id: str, *, final_export: bool = False) -> CoverAsset:
        asset = self.get(asset_id)
        used_at = datetime.now(timezone.utc)
        latest = max((item.last_used_at for item in self._assets.values() if item.last_used_at), default="")
        try:
            previous = datetime.fromisoformat(latest)
        except ValueError:
            previous = None
        if previous is not None and previous.tzinfo is not None and used_at <= previous:
            # Windows 的时钟可能让连续点击得到同一时间，给下一次使用保留确定的先后顺序。
            used_at = previous + timedelta(microseconds=1)
        updated = CoverAsset(
            **{
                **asset.to_payload(),
                "usage_count": asset.usage_count + 1,
                "last_used_at": used_at.isoformat(),
                "final_export_count": asset.final_export_count + (1 if final_export else 0),
            }
        )
        self._assets[asset_id] = updated
        self._write_index()
        return updated

    def resolve(self, asset_id: str) -> Path:
        asset = self.get(asset_id)
        path = Path(asset.path).resolve()
        if not path.is_file():
            raise FileNotFoundError("素材文件已不存在")
        return path

