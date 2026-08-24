"""后台任务完成结果的规范化与紧凑摘要。

完整报告、话题、字幕和检查点均由各自产物 owner 持久化。任务数据库只保存
队列恢复和前端结果入口需要的小型摘要，避免把完整流水线返回对象复制进 SQLite。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

_PIPELINE_PATH_FIELDS = (
    "artifact_dir",
    "overview_path",
    "slice_dir",
    "json_path",
    "md_path",
    "srt_path",
)
_PIPELINE_WARNING_FIELDS = (
    "api_precheck_warning",
    "clip_review_warning",
    "unified_queue_warning",
)
_MAX_WARNING_LENGTH = 2000
_MAX_RESULT_SUMMARY_BYTES = 64 * 1024
_MAX_QUALITY_OVERVIEW_BYTES = 48 * 1024


def normalize_task_result(value: Any) -> Any:
    """把旧 Web 层传入的 JSON 字符串恢复为实际 JSON 值。

    非 JSON 字符串仍按普通文本保留，确保旧的直接切片结果和错误兼容入口不变。
    """

    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _non_negative_int(value: Any, *, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _failed_chunk_count(result: Mapping[str, Any]) -> int:
    explicit = result.get("failed_chunk_count")
    if explicit is not None:
        return _non_negative_int(explicit)
    failed_chunks = result.get("failed_chunks")
    if isinstance(failed_chunks, Sequence) and not isinstance(
            failed_chunks, (str, bytes, bytearray)):
        return len(failed_chunks)
    return 0


def _compact_quality_overview(value: Any) -> dict[str, Any] | None:
    """只规范化已由分析 owner 构建的概览，不重新计算业务指标。"""

    if not isinstance(value, Mapping):
        return None
    try:
        overview = json.loads(json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ))
    except (TypeError, ValueError):
        return None
    if not isinstance(overview, dict):
        return None
    clips = overview.get("clips")
    edges = overview.get("edge_candidates")
    if not isinstance(clips, list):
        overview["clips"] = []
    if not isinstance(edges, list):
        overview["edge_candidates"] = []
    trim_clips_next = len(overview["clips"]) >= len(overview["edge_candidates"])
    while len(json.dumps(
            overview,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
    ).encode("utf-8")) > _MAX_QUALITY_OVERVIEW_BYTES:
        clips = overview["clips"]
        edges = overview["edge_candidates"]
        can_trim_clips = bool(clips) and (len(clips) > 1 or not edges)
        can_trim_edges = bool(edges) and (len(edges) > 1 or not clips)
        if not can_trim_clips and not can_trim_edges:
            return None
        if can_trim_clips and can_trim_edges:
            trim_clips = trim_clips_next
            trim_clips_next = not trim_clips_next
        else:
            trim_clips = can_trim_clips
        field = "clips" if trim_clips else "edge_candidates"
        truncated_field = (
            "clips_truncated_count"
            if trim_clips
            else "edge_candidates_truncated_count"
        )
        rows = overview[field]
        remove_count = max(1, len(rows) // 2)
        del rows[-remove_count:]
        overview[truncated_field] = (
            _non_negative_int(overview.get(truncated_field)) + remove_count
        )
    return overview


def build_pipeline_result_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    """从完整流水线返回对象构建可持久化的小型任务摘要。"""

    if not isinstance(result, Mapping):
        raise TypeError("流水线结果必须是 Mapping")

    clip_marks = result.get("clip_marks")
    clip_mark_count = (
        len(clip_marks)
        if isinstance(clip_marks, Sequence)
        and not isinstance(clip_marks, (str, bytes, bytearray))
        else 0
    )
    summary: dict[str, Any] = {
        "summary_version": 1,
        "topic_count": _non_negative_int(result.get("topic_count")),
        "slice_count": _non_negative_int(
            result.get("slice_count"),
            default=clip_mark_count,
        ),
        "failed_chunk_count": _failed_chunk_count(result),
        "report_available": bool(result.get("report") or result.get("md_path")),
    }

    def add_if_fits(field: str, value: Any) -> None:
        """只追加可完整落入 TaskStore 大小上限的可选摘要字段。"""

        candidate = {**summary, field: value}
        encoded = json.dumps(
            candidate,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) <= _MAX_RESULT_SUMMARY_BYTES:
            summary[field] = value

    quality_overview = _compact_quality_overview(result.get("quality_overview"))
    if quality_overview is not None:
        add_if_fits("quality_overview", quality_overview)
    for field in _PIPELINE_PATH_FIELDS:
        value = result.get(field)
        if value:
            add_if_fits(field, str(value))
    for field in _PIPELINE_WARNING_FIELDS:
        value = str(result.get(field) or "").strip()
        if value:
            add_if_fits(field, value[:_MAX_WARNING_LENGTH])
    return summary


__all__ = [
    "build_pipeline_result_summary",
    "normalize_task_result",
]
