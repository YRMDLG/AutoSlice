"""Desktop vNext 的用户数据目录与可恢复草稿协议。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DRAFT_SCHEMA_VERSION = 1
SESSION_SCHEMA_VERSION = 1
DATA_DIR_ENV_NAME = "AUTOSLICE_DESKTOP_DATA_DIR"
_DRAFT_KINDS = {"subtitle", "cover"}


@dataclass(frozen=True)
class DraftRead:
    """ready 才允许直接恢复；其他状态须交由界面提示用户。"""

    status: str
    payload: Any = None
    updated_at: str | None = None


class DesktopStorage:
    """只保存应用状态；投稿目录与正式 SRT 仍是项目事实来源。"""

    def __init__(self, root: Path | str | None = None) -> None:
        if root is None:
            configured = os.environ.get(DATA_DIR_ENV_NAME)
            local = os.environ.get("LOCALAPPDATA")
            if not configured and not local:
                raise RuntimeError("无法确定桌面数据目录：LOCALAPPDATA 未设置")
            root = configured or Path(local) / "AutoSlice"
        self.root = Path(root).expanduser().resolve()

    @property
    def sessions(self) -> Path:
        return self.root / "sessions"

    @property
    def drafts(self) -> Path:
        return self.root / "drafts"

    @property
    def thumbnails(self) -> Path:
        return self.root / "cache" / "thumbnails"

    @property
    def waveforms(self) -> Path:
        return self.root / "cache" / "waveforms"

    @property
    def ai_cache(self) -> Path:
        return self.root / "cache" / "ai"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @staticmethod
    def _identity(path: Path | str) -> str:
        return os.path.normcase(str(Path(path).expanduser().resolve()))

    @classmethod
    def _signature(cls, source: Path | str) -> dict[str, int | str]:
        path = Path(source)
        stat = path.stat()
        return {
            "path": cls._identity(path),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }

    @classmethod
    def _dependency_signature(cls, source: Path | str) -> dict[str, int | str | bool]:
        try:
            return cls._signature(source)
        except FileNotFoundError:
            return {"path": cls._identity(source), "exists": False}

    def draft_path(self, kind: str, project: Path | str, source: Path | str) -> Path:
        if kind not in _DRAFT_KINDS:
            raise ValueError(f"未知草稿类型：{kind}")
        identity = "\0".join((kind, self._identity(project), self._identity(source)))
        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self.drafts / kind / f"{key}.json"

    def capture_baseline(
        self, source: Path | str, *, dependencies: Iterable[Path | str] = (),
    ) -> dict[str, Any]:
        """在打开编辑器时记录基线，供首次自动保存沿用。"""

        return {
            "source": self._signature(source),
            "dependencies": [
                self._dependency_signature(item) for item in dependencies
            ],
        }

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                prefix=f".{path.stem}-", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def save_draft(
        self, kind: str, project: Path | str, source: Path | str, payload: Any,
        *, dependencies: Iterable[Path | str] = (), baseline: dict[str, Any] | None = None,
    ) -> Path:
        """原子保存编辑快照；从不写入 source 或项目目录。"""

        path = self.draft_path(kind, project, source)
        dependencies = tuple(dependencies)
        project_identity = self._identity(project)
        requested_dependencies = [self._identity(item) for item in dependencies]
        if baseline is not None and (
            not isinstance(baseline.get("source"), dict)
            or baseline["source"].get("path") != self._identity(source)
            or not isinstance(baseline.get("dependencies"), list)
            or not all(isinstance(item, dict) for item in baseline["dependencies"])
            or [item.get("path") for item in baseline["dependencies"]]
            != requested_dependencies
        ):
            raise ValueError("草稿基线与当前源文件不匹配")
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError) as exc:
                raise ValueError("已有草稿无法读取，请先处理冲突") from exc
            if not isinstance(existing, dict) or (
                existing.get("schema_version") != DRAFT_SCHEMA_VERSION
                or existing.get("kind") != kind
                or existing.get("project") != project_identity
                or not isinstance(existing.get("source"), dict)
                or not isinstance(existing.get("dependencies"), list)
                or not all(isinstance(item, dict) for item in existing["dependencies"])
                or [item.get("path") for item in existing["dependencies"]]
                != requested_dependencies
            ):
                raise ValueError("已有草稿格式或依赖不兼容，请先处理冲突")
            source_signature = existing["source"]
            dependency_signatures = existing["dependencies"]
            if baseline is not None and (
                baseline["source"] != source_signature
                or baseline["dependencies"] != dependency_signatures
            ):
                raise ValueError("草稿基线与已有草稿不一致，请先处理冲突")
        else:
            captured = baseline or self.capture_baseline(
                source, dependencies=dependencies
            )
            source_signature = captured["source"]
            dependency_signatures = captured["dependencies"]
        envelope = {
            "schema_version": DRAFT_SCHEMA_VERSION,
            "kind": kind,
            "project": project_identity,
            "source": source_signature,
            "dependencies": dependency_signatures,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        self._write_json(path, envelope)
        return path

    def read_draft(
        self, kind: str, project: Path | str, source: Path | str,
        *, dependencies: Iterable[Path | str] = (),
    ) -> DraftRead:
        """版本或源文件变化时保留草稿，但不自动套用。"""

        path = self.draft_path(kind, project, source)
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return DraftRead("missing")
        except (OSError, ValueError, UnicodeError):
            return DraftRead("invalid")
        if not isinstance(envelope, dict):
            return DraftRead("invalid")
        if envelope.get("schema_version") != DRAFT_SCHEMA_VERSION:
            return DraftRead("incompatible")
        if envelope.get("kind") != kind or envelope.get("project") != self._identity(project):
            return DraftRead("invalid")
        try:
            signature = self._signature(source)
        except OSError:
            return DraftRead("source_missing")
        if envelope.get("source") != signature:
            return DraftRead("source_changed")
        try:
            current_dependencies = [
                self._dependency_signature(path) for path in dependencies
            ]
        except OSError:
            return DraftRead("source_missing")
        if envelope.get("dependencies") != current_dependencies:
            return DraftRead("source_changed")
        if "payload" not in envelope or not isinstance(envelope.get("updated_at"), str):
            return DraftRead("invalid")
        return DraftRead("ready", envelope["payload"], envelope["updated_at"])

    def save_session(self, payload: dict[str, Any]) -> Path:
        """保存页面、项目和处理位置；调用方负责字段契约。"""

        path = self.sessions / "last.json"
        self._write_json(path, {"schema_version": SESSION_SCHEMA_VERSION, "payload": payload})
        return path

    def read_session(self) -> dict[str, Any] | None:
        try:
            value = json.loads((self.sessions / "last.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return None
        if not isinstance(value, dict) or value.get("schema_version") != SESSION_SCHEMA_VERSION:
            return None
        payload = value.get("payload")
        return payload if isinstance(payload, dict) else None
