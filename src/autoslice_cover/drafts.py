"""AutoCover 多视频编辑草稿与预览文件持久化。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .renderer import RenderResult
from .video import FrameCandidate
from .workspace import CoverTask


DRAFT_SCHEMA_VERSION = 1
DRAFT_DIRECTORY_NAME = "_AutoCover草稿"
_SAFE_NAME_PATTERN = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")


def _normalized_path(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _safe_workspace_name(path: Path) -> str:
    cleaned = _SAFE_NAME_PATTERN.sub("_", path.name).strip(" ._")
    return (cleaned or "workspace")[:64]


def _source_identity(path: str | Path) -> tuple[int, float]:
    stat = Path(path).stat()
    return int(stat.st_size), float(stat.st_mtime)


def _epoch_milliseconds(value: Any = None) -> float:
    """兼容旧草稿的秒级时间戳，并统一返回毫秒时间戳。"""

    if value is None:
        return time.time() * 1000.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number < 0:
        return 0.0
    # 2001 年至今的 Unix 毫秒时间戳均大于 1e12；旧版写入的是秒。
    return number * 1000.0 if number < 1_000_000_000_000 else number


def _preview_fingerprint(
    settings: dict[str, Any],
    canvas_key: str,
    selected_timestamp: float | None = None,
) -> str:
    relevant = {
        "title": settings.get("title"),
        "template_key": settings.get("template_key"),
        "palette_key": settings.get("palette_key"),
        "copy_lines": settings.get("copy_lines"),
        "line_colors": settings.get("line_colors"),
        "line_stroke_colors": settings.get("line_stroke_colors"),
        "layout": (
            settings.get("layouts", {}).get(canvas_key)
            if isinstance(settings.get("layouts"), dict)
            else None
        ),
        "selected_timestamp": (
            round(float(selected_timestamp), 6)
            if selected_timestamp is not None
            and math.isfinite(float(selected_timestamp))
            else None
        ),
    }
    encoded = json.dumps(
        relevant,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CoverDraftStore:
    """在封面输出目录中原子保存每个视频的编辑状态和最后预览。"""

    def __init__(self, workspace_root: str | Path, output_dir: str | Path) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.output_dir = Path(output_dir).expanduser().resolve()
        digest = hashlib.sha256(
            _normalized_path(self.workspace_root).encode("utf-8")
        ).hexdigest()[:12]
        self.root = (
            self.output_dir
            / DRAFT_DIRECTORY_NAME
            / f"{_safe_workspace_name(self.workspace_root)}-{digest}"
        ).resolve()
        self.index_path = self.root / "工作区.json"
        self._lock = threading.RLock()
        self._latest_revisions: dict[str, float] = {}

    def _empty_payload(self) -> dict[str, Any]:
        return {
            "schema_version": DRAFT_SCHEMA_VERSION,
            "workspace_root": str(self.workspace_root),
            "updated_at": 0.0,
            "active_relative_path": None,
            "tasks": {},
        }

    def _read_locked(self) -> dict[str, Any]:
        if not self.index_path.is_file():
            return self._empty_payload()
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return self._empty_payload()
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != DRAFT_SCHEMA_VERSION
            or _normalized_path(payload.get("workspace_root", ""))
            != _normalized_path(self.workspace_root)
            or not isinstance(payload.get("tasks"), dict)
        ):
            return self._empty_payload()
        return payload

    def _write_locked(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.index_path.with_name(
            f".{self.index_path.name}.{secrets.token_hex(8)}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temporary, self.index_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _copy_atomic(source: str | Path, destination: Path) -> Path:
        source_path = Path(source).expanduser().resolve()
        destination = destination.expanduser().resolve()
        if source_path == destination:
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.{secrets.token_hex(8)}.tmp"
        )
        try:
            shutil.copy2(source_path, temporary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def _relative_asset(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def _asset_path(self, value: Any) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            return None
        candidate = (self.root / value).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def save_preview(
        self,
        task: CoverTask,
        *,
        settings: dict[str, Any],
        canvas_key: str,
        preview_payload: dict[str, Any],
        result: RenderResult,
        candidate: FrameCandidate,
        revision: float | None = None,
    ) -> bool:
        """保存一次成功预览，并保留足以在下次启动后继续编辑的选中帧。"""

        with self._lock:
            incoming_revision = _epoch_milliseconds(revision)
            payload = self._read_locked()
            tasks = payload.setdefault("tasks", {})
            entry = tasks.get(task.relative_path)
            if not isinstance(entry, dict):
                entry = {}
            latest_revision = max(
                _epoch_milliseconds(entry.get("client_updated_at")),
                self._latest_revisions.get(task.relative_path, 0.0),
            )
            if incoming_revision < latest_revision:
                return False
            self._latest_revisions[task.relative_path] = incoming_revision

            task_dir = self.root / task.id
            preview_path = self._copy_atomic(
                result.output_path,
                task_dir / f"{canvas_key}.jpg",
            )
            background_path = None
            if result.background_path:
                background_path = self._copy_atomic(
                    result.background_path,
                    task_dir / f"{canvas_key}-background.jpg",
                )
            frame_suffix = Path(candidate.path).suffix.casefold()
            if frame_suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                frame_suffix = ".jpg"
            frame_path = self._copy_atomic(
                candidate.path,
                task_dir / f"selected-frame{frame_suffix}",
            )

            previews = entry.get("previews")
            if not isinstance(previews, dict):
                previews = {}
            metadata = dict(preview_payload)
            for key in ("media_token", "background_media_token", "filename"):
                metadata.pop(key, None)
            previews[canvas_key] = {
                "image": self._relative_asset(preview_path),
                "background": (
                    self._relative_asset(background_path) if background_path else None
                ),
                "metadata": metadata,
                "selected_timestamp": float(candidate.timestamp),
                "settings_fingerprint": _preview_fingerprint(
                    settings,
                    canvas_key,
                    float(candidate.timestamp),
                ),
            }
            source_size, source_modified_at = _source_identity(task.video_path)
            entry.update(
                {
                    "relative_path": task.relative_path,
                    "source_size": source_size,
                    "source_modified_at": source_modified_at,
                    "updated_at": _epoch_milliseconds(),
                    "client_updated_at": incoming_revision,
                    "selected_timestamp": float(candidate.timestamp),
                    "selected_frame": self._relative_asset(frame_path),
                    "selected_score": float(candidate.score),
                    "selected_metrics": candidate.metrics.to_dict(),
                    "settings": settings,
                    "previews": previews,
                }
            )
            tasks[task.relative_path] = entry
            payload["active_relative_path"] = task.relative_path
            payload["updated_at"] = entry["updated_at"]
            self._write_locked(payload)
            return True

    def reserve_revision(self, relative_path: str, revision: float | None) -> bool:
        """预约一次预览版本，阻止较旧的并发请求覆盖工作区和磁盘草稿。"""

        incoming_revision = _epoch_milliseconds(revision)
        with self._lock:
            payload = self._read_locked()
            entry = payload.get("tasks", {}).get(relative_path)
            saved_revision = (
                _epoch_milliseconds(entry.get("client_updated_at"))
                if isinstance(entry, dict)
                else 0.0
            )
            latest_revision = max(
                saved_revision,
                self._latest_revisions.get(relative_path, 0.0),
            )
            if incoming_revision < latest_revision:
                return False
            self._latest_revisions[relative_path] = incoming_revision
            return True

    def load_records(self, tasks: Iterable[CoverTask]) -> list[dict[str, Any]]:
        """读取仍与源视频身份一致的草稿记录，损坏的单条记录会被忽略。"""

        with self._lock:
            payload = self._read_locked()
            saved_tasks = payload.get("tasks", {})
            records: list[dict[str, Any]] = []
            for task in tasks:
                entry = saved_tasks.get(task.relative_path)
                if not isinstance(entry, dict) or not isinstance(entry.get("settings"), dict):
                    continue
                try:
                    source_size, source_modified_at = _source_identity(task.video_path)
                except OSError:
                    continue
                if (
                    int(entry.get("source_size", -1)) != source_size
                    or abs(float(entry.get("source_modified_at", -1)) - source_modified_at) > 0.01
                ):
                    continue
                selected_frame = self._asset_path(entry.get("selected_frame"))
                if selected_frame is None:
                    continue
                previews: dict[str, dict[str, Any]] = {}
                raw_previews = entry.get("previews")
                if isinstance(raw_previews, dict):
                    for canvas_key, raw_preview in raw_previews.items():
                        if canvas_key not in {"4x3", "16x9"} or not isinstance(raw_preview, dict):
                            continue
                        try:
                            preview_timestamp = float(raw_preview["selected_timestamp"])
                            current_timestamp = float(entry["selected_timestamp"])
                        except (KeyError, TypeError, ValueError):
                            # 旧版本没有按比例记录选帧，不能安全恢复其预览，
                            # 但仍保留同一 entry 的文字和布局设置。
                            continue
                        if (
                            not math.isfinite(preview_timestamp)
                            or not math.isfinite(current_timestamp)
                            or abs(preview_timestamp - current_timestamp) > 0.05
                        ):
                            continue
                        image = self._asset_path(raw_preview.get("image"))
                        background = self._asset_path(raw_preview.get("background"))
                        metadata = raw_preview.get("metadata")
                        if (
                            image is None
                            or not isinstance(metadata, dict)
                            or raw_preview.get("settings_fingerprint")
                            != _preview_fingerprint(
                                entry["settings"],
                                canvas_key,
                                current_timestamp,
                            )
                        ):
                            continue
                        previews[canvas_key] = {
                            "image_path": image,
                            "background_path": background,
                            "metadata": dict(metadata),
                            "selected_timestamp": preview_timestamp,
                        }
                records.append(
                    {
                        "relative_path": task.relative_path,
                        "updated_at": _epoch_milliseconds(entry.get("updated_at")),
                        "selected_timestamp": float(entry.get("selected_timestamp", 0.0)),
                        "selected_score": float(entry.get("selected_score", 0.0)),
                        "selected_metrics": entry.get("selected_metrics"),
                        "selected_frame_path": selected_frame,
                        "settings": dict(entry["settings"]),
                        "previews": previews,
                        "active": payload.get("active_relative_path") == task.relative_path,
                    }
                )
            return records

    def set_active(self, relative_path: str) -> bool:
        """记录用户最后打开的已有草稿，不为尚未生成预览的视频创建空记录。"""

        with self._lock:
            payload = self._read_locked()
            tasks = payload.get("tasks", {})
            if relative_path not in tasks:
                return False
            payload["active_relative_path"] = relative_path
            payload["updated_at"] = _epoch_milliseconds()
            self._write_locked(payload)
            return True
