"""独立读取 AutoSlice 整理包中供 AutoCover 使用的稳定 JSON 契约。"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_FILENAME = "精调任务.json"
ARTIFACT_BUNDLE_SUFFIX = "_自动切片"
SLICE_DIRECTORY_SUFFIX = "_话题切片"
SUPPORTED_CLIP_TIMEBASE = "source_video_seconds"


def _exact_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        return None
    return candidate.resolve()


def _path_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


@dataclass(frozen=True, slots=True)
class CoverContractMatch:
    """一个通过绝对路径精确命中的封面任务契约。"""

    match_source: str
    publish_title: str | None
    cover_anchor_seconds: float | None
    slice_anchor_source: str | None
    editorial_interest_score: float | None
    editorial_interest_reason: str | None
    subtitle_exists: bool
    subtitle_filename: str | None

    def to_public_dict(self) -> dict[str, Any]:
        """返回不包含本机绝对路径的 API 元数据。"""

        return {
            "matched": True,
            "match_source": self.match_source,
            "cover_anchor_seconds": self.cover_anchor_seconds,
            "slice_anchor_source": self.slice_anchor_source,
            "editorial_interest_score": self.editorial_interest_score,
            "editorial_interest_reason": self.editorial_interest_reason,
            "subtitle_exists": self.subtitle_exists,
            "subtitle_filename": self.subtitle_filename,
        }


def empty_public_contract() -> dict[str, Any]:
    """返回未关联时的稳定 API 形状。"""

    return {
        "matched": False,
        "match_source": None,
        "cover_anchor_seconds": None,
        "slice_anchor_source": None,
        "editorial_interest_score": None,
        "editorial_interest_reason": None,
        "subtitle_exists": False,
        "subtitle_filename": None,
    }


def discover_refinement_manifest(workspace_root: str | Path) -> Path | None:
    """按规范目录名确定性查找当前切片目录对应的 sibling 整理包。"""

    root = Path(workspace_root).expanduser().resolve()
    if not root.name.endswith(SLICE_DIRECTORY_SUFFIX):
        return None
    recording_stem = root.name[: -len(SLICE_DIRECTORY_SUFFIX)]
    if not recording_stem:
        return None
    candidate = (
        root.parent
        / f"{recording_stem}{ARTIFACT_BUNDLE_SUFFIX}"
        / "数据"
        / MANIFEST_FILENAME
    )
    return candidate.resolve() if candidate.is_file() else None


class RefinementManifestContract:
    """把可靠任务声明索引为可精确匹配的媒体绝对路径。"""

    def __init__(self, matches: dict[str, CoverContractMatch]) -> None:
        self._matches = dict(matches)

    @classmethod
    def load(cls, manifest_path: str | Path | None) -> RefinementManifestContract:
        """读取整理包；缺失、损坏或旧 schema 均安全回退为空契约。"""

        if manifest_path is None:
            return cls({})
        path = Path(manifest_path).expanduser().resolve()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError):
            return cls({})
        if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
            return cls({})

        indexed: dict[str, CoverContractMatch] = {}
        ambiguous: set[str] = set()
        for task in payload["tasks"]:
            declarations = cls._task_declarations(task)
            for media_path, match in declarations:
                key = _path_key(media_path)
                if key in indexed:
                    ambiguous.add(key)
                    indexed.pop(key, None)
                elif key not in ambiguous:
                    indexed[key] = match
        for key in ambiguous:
            indexed.pop(key, None)
        return cls(indexed)

    @staticmethod
    def _task_declarations(task: Any) -> list[tuple[Path, CoverContractMatch]]:
        if not isinstance(task, dict):
            return []
        if task.get("clip_timebase") != SUPPORTED_CLIP_TIMEBASE:
            return []
        segment_count = task.get("source_segment_count")
        if isinstance(segment_count, bool) or segment_count != 1:
            return []
        clip_start = _finite_number(task.get("clip_start_seconds"))
        clip_end = _finite_number(task.get("clip_end_seconds"))
        if clip_start is None or clip_end is None or clip_end <= clip_start:
            return []
        publish_title = _optional_text(task.get("publish_title"))

        original_path = _exact_path(task.get("original_slice_path"))
        legacy_path = _exact_path(task.get("slice_path"))
        final_path = _exact_path(task.get("final_clip_path"))
        original_path_reliable = (
            original_path is not None
            and (
                legacy_path is None
                or _path_key(legacy_path) == _path_key(original_path)
            )
        )
        subtitle_path = _exact_path(task.get("corrected_srt_path"))
        anchor_media_path = _exact_path(task.get("cover_anchor_media_path"))
        anchor_seconds = _finite_number(task.get("cover_anchor_seconds"))
        slice_anchor = _finite_number(task.get("slice_anchor"))
        source_anchor_valid = (
            anchor_seconds is not None
            and anchor_seconds >= 0
            and slice_anchor is not None
            and clip_start <= slice_anchor <= clip_end
            and math.isclose(
                anchor_seconds,
                slice_anchor - clip_start,
                rel_tol=0.0,
                abs_tol=1e-6,
            )
        )
        final_anchor_valid = anchor_seconds is not None and anchor_seconds >= 0

        subtitle_filename = subtitle_path.name if subtitle_path is not None else None
        shared = {
            "publish_title": publish_title,
            "slice_anchor_source": _optional_text(task.get("slice_anchor_source")),
            "editorial_interest_score": _finite_number(
                task.get("editorial_interest_score")
            ),
            "editorial_interest_reason": _optional_text(
                task.get("editorial_interest_reason")
            ),
            "subtitle_exists": bool(subtitle_path and subtitle_path.is_file()),
            "subtitle_filename": subtitle_filename,
        }

        declarations: list[tuple[Path, CoverContractMatch]] = []
        if final_path is not None:
            declarations.append(
                (
                    final_path,
                    CoverContractMatch(
                        match_source="manifest_final_clip",
                        cover_anchor_seconds=(
                            anchor_seconds
                            if final_anchor_valid
                            and anchor_media_path is not None
                            and _path_key(anchor_media_path) == _path_key(final_path)
                            else None
                        ),
                        **shared,
                    ),
                )
            )
        if original_path_reliable and (
            final_path is None or _path_key(final_path) != _path_key(original_path)
        ):
            declarations.append(
                (
                    original_path,
                    CoverContractMatch(
                        match_source="manifest_original_slice",
                        cover_anchor_seconds=(
                            anchor_seconds
                            if source_anchor_valid
                            and anchor_media_path is not None
                            and _path_key(anchor_media_path) == _path_key(original_path)
                            and anchor_seconds <= clip_end - clip_start
                            else None
                        ),
                        **shared,
                    ),
                )
            )
        return declarations

    def match(self, media_path: str | Path) -> CoverContractMatch | None:
        """只按已解析绝对路径精确命中，不做文件名或相似度推断。"""

        return self._matches.get(_path_key(media_path))


__all__ = [
    "CoverContractMatch",
    "RefinementManifestContract",
    "discover_refinement_manifest",
    "empty_public_contract",
]
