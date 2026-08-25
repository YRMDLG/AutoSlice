"""只读切片时间轴的独立契约与序列化 owner。

``quality_overview`` 是有意允许裁剪的摘要，不能作为时间轴数据源。本模块只
接受现有流水线的 ``clip_marks`` 和候选复核审计，生成一个可独立读取的、带完整性
标记的 JSON-compatible mapping；不写文件、不读取媒体，也不修改输入对象。

输入数据的兼容策略是保守的：缺少时间或时间异常的记录不会被猜测或修正，而是
被排除并令 ``complete`` 为 ``False``。重复 ID 只保留第一次出现的记录；输入超过
条数或字节预算时只保留确定的前缀/较早时间记录，并显式令
``complete=False``、``truncated=True``。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1
MAX_CLIPS = 500
MAX_EDGE_CANDIDATES = 500
MAX_TIMELINE_BYTES = 256 * 1024
MAX_TITLE_CHARS = 120
MAX_SOURCE_CHARS = 80
MAX_REASON_CHARS = 240
MAX_ID_CHARS = 160
MAX_ISSUES = 64

_MISSING = object()
_LOCAL_PATH_PATTERN = re.compile(
    r"(?i)(?:file:"
    r"//|[a-z]:[\\/]|\\\\[^\\/\s]+[\\/]"
    r"|(?:^|[\s(（])/(?:home|users|var|tmp|etc|opt|mnt|srv|root)(?:/|$)"
    r"|(?:^|[\s(（])\.{1,2}[\\/])"
)


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return round(number, 3)


def _safe_text(value: Any, fallback: str, maximum: int) -> tuple[str, bool, bool]:
    """返回文本、是否使用回退值以及是否发生了文本截断。"""

    if value is None or isinstance(value, bool):
        return fallback, True, False
    text = " ".join(str(value).replace("\x00", " ").split())
    if not text or _LOCAL_PATH_PATTERN.search(text):
        return fallback, True, False
    if len(text) > maximum:
        return text[: maximum - 1].rstrip() + "…", False, True
    return text, False, False


def _stable_id(
        item: Mapping[str, Any],
        *,
        kind: str,
        task_id: str,
        start: float,
        end: float,
        title: str,
) -> tuple[str, bool, bool]:
    """读取显式 ID，或由稳定业务字段派生一个 ID。"""

    invalid_explicit_id = False
    for key in ("id", "stable_id", "clip_id" if kind == "clip" else "candidate_id"):
        value = item.get(key)
        if value is None or isinstance(value, bool):
            continue
        text = " ".join(str(value).split())
        if text and len(text) <= MAX_ID_CHARS and not _LOCAL_PATH_PATTERN.search(text):
            return text, True, False
        if text:
            invalid_explicit_id = True

    identity = {
        "end": end,
        "kind": kind,
        "start": start,
        "task_id": task_id,
        "title": title,
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()[:24]
    return f"{kind}-{digest}", False, invalid_explicit_id


def _records(value: Any, *, field: str, candidate: bool) -> tuple[list[Any], bool, str | None]:
    """解包旧 owner 的数据，同时区分缺失字段和显式空数据。"""

    if value is None:
        return [], False, f"{field}_missing"
    if isinstance(value, Mapping):
        if candidate and "candidates" in value:
            value = value["candidates"]
        elif not candidate and "clips" in value:
            value = value["clips"]
        else:
            return [], False, f"{field}_missing"
    if not _is_sequence(value):
        return [], False, f"{field}_invalid_container"
    return list(value), True, None


def _serialize_records(
        records: list[Any],
        *,
        kind: str,
        task_id: str,
        video_duration: float | None,
        issues: list[str],
        seen_ids: set[str],
) -> tuple[list[dict[str, Any]], bool]:
    serialized: list[dict[str, Any]] = []
    text_truncated = False
    for item in records:
        if not isinstance(item, Mapping):
            if "invalid_record" not in issues:
                issues.append("invalid_record")
            continue

        start = _number(item.get("start"))
        end = _number(item.get("end"))
        if start is None or end is None:
            issue = f"{kind}_time_missing" if "start" not in item or "end" not in item else f"{kind}_time_invalid"
            if issue not in issues:
                issues.append(issue)
            continue
        if end <= start or (video_duration is not None and end > video_duration):
            if f"{kind}_time_invalid" not in issues:
                issues.append(f"{kind}_time_invalid")
            continue

        title_value = item.get("title") or item.get("publish_title")
        title, title_missing, title_truncated = _safe_text(
            title_value, "未命名", MAX_TITLE_CHARS
        )
        source_value = item.get("source")
        if source_value is None:
            source_value = (
                item.get("slice_anchor_source")
                if kind == "clip"
                else item.get("final_slice_anchor_source") or item.get("candidate_sources")
            )
        if isinstance(source_value, (list, tuple)):
            source_value = ", ".join(str(value).strip() for value in source_value if str(value).strip())
        source, source_missing, source_truncated = _safe_text(
            source_value, "未知来源", MAX_SOURCE_CHARS
        )

        reason_value = item.get("reason")
        if reason_value is None:
            reason_value = (
                item.get("editorial_interest_reason")
                if kind == "clip"
                else item.get("interest_reason") or item.get("rejection_reason")
            )
        reason, reason_missing, reason_truncated = _safe_text(
            reason_value, "未记录原因", MAX_REASON_CHARS
        )
        text_truncated = text_truncated or title_truncated or source_truncated or reason_truncated
        if source_missing and f"{kind}_source_missing" not in issues:
            issues.append(f"{kind}_source_missing")
        if reason_missing and f"{kind}_reason_missing" not in issues:
            issues.append(f"{kind}_reason_missing")
        if (
            title_missing
            and title_value is not None
            and str(title_value).strip()
            and f"{kind}_title_missing" not in issues
        ):
            issues.append(f"{kind}_title_missing")

        item_id, _explicit_id, invalid_explicit_id = _stable_id(
            item,
            kind=kind,
            task_id=task_id,
            start=start,
            end=end,
            title=title,
        )
        if invalid_explicit_id:
            _add_issue(issues, f"{kind}_id_invalid")
        if item_id in seen_ids:
            if "duplicate_id" not in issues:
                issues.append("duplicate_id")
            continue
        seen_ids.add(item_id)
        row = {
            "id": item_id,
            "start": start,
            "end": end,
            "source": source,
            "reason": reason,
            "title": title,
        }
        # 媒体预览只接受 manifest 明确登记的 clip_id；时间轴展示 ID
        # 不能被前端自行猜成文件或媒体 ID。旧数据没有该字段时保持兼容，
        # 由前端明确展示“暂无可预览短片”。
        if kind == "clip" and item.get("clip_id") is not None:
            media_id = item.get("clip_id")
            if (
                    isinstance(media_id, str)
                    and media_id
                    and media_id == media_id.strip()
                    and len(media_id) <= MAX_ID_CHARS
                    and not _LOCAL_PATH_PATTERN.search(media_id)
                    and not any(char in media_id for char in "\\/\x00\r\n")
            ):
                row["clip_id"] = media_id
            else:
                _add_issue(issues, "clip_id_invalid")
        serialized.append(row)
    serialized.sort(key=lambda row: (row["start"], row["end"], row["id"]))
    return serialized, text_truncated


def _add_issue(issues: list[str], issue: str) -> None:
    if issue not in issues and len(issues) < MAX_ISSUES:
        issues.append(issue)


def _encoded_size(value: Mapping[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def serialize_timeline(
        task_id: Any,
        video_duration: Any,
        clip_marks: Any = None,
        candidate_review_audit: Any = None,
        *,
        clips: Any = _MISSING,
        edge_candidates: Any = _MISSING,
        generated_at: str | None = None,
        max_clips: int = MAX_CLIPS,
        max_edge_candidates: int = MAX_EDGE_CANDIDATES,
        max_bytes: int = MAX_TIMELINE_BYTES,
) -> dict[str, Any]:
    """将现有切片和候选 owner 序列化为只读时间轴契约。

    ``[]``/``{"candidates": []}`` 表示已确认的空数据；``None`` 或缺少内部字段
    表示旧数据缺失，因此会得到 ``complete=False``。所有记录均先校验再排序，输入
    mapping 不会被修改。``generated_at`` 建议由调用方注入以保持测试和重放确定性。
    """

    if max_clips < 0 or max_edge_candidates < 0 or max_bytes <= 0:
        raise ValueError("时间轴限制必须为非负条数且字节限制必须大于 0")

    issues: list[str] = []
    raw_task_id, task_id_missing, task_id_truncated = _safe_text(task_id, "unknown-task", 160)
    if task_id_missing:
        _add_issue(issues, "task_id_missing")
    duration = _number(video_duration)
    if duration is None:
        _add_issue(issues, "video_duration_invalid")

    timestamp, timestamp_missing, timestamp_truncated = _safe_text(
        generated_at,
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        64,
    )
    if generated_at is not None and timestamp_missing:
        _add_issue(issues, "generated_at_missing")

    raw_clips = clip_marks if clips is _MISSING else clips
    raw_edges = candidate_review_audit if edge_candidates is _MISSING else edge_candidates
    clip_rows, clips_present, clips_issue = _records(raw_clips, field="clips", candidate=False)
    edge_rows, edges_present, edges_issue = _records(raw_edges, field="edge_candidates", candidate=True)
    if clips_issue:
        _add_issue(issues, clips_issue)
    if edges_issue:
        _add_issue(issues, edges_issue)

    seen_ids: set[str] = set()
    output_clips, clips_text_truncated = _serialize_records(
        clip_rows,
        kind="clip",
        task_id=raw_task_id,
        video_duration=duration,
        issues=issues,
        seen_ids=seen_ids,
    ) if clips_present else ([], False)
    output_edges, edges_text_truncated = _serialize_records(
        edge_rows,
        kind="candidate",
        task_id=raw_task_id,
        video_duration=duration,
        issues=issues,
        seen_ids=seen_ids,
    ) if edges_present else ([], False)

    truncated = task_id_truncated or timestamp_truncated or clips_text_truncated or edges_text_truncated
    if truncated:
        _add_issue(issues, "text_limit")
    if len(output_clips) > max_clips:
        del output_clips[max_clips:]
        truncated = True
        _add_issue(issues, "clips_limit")
    if len(output_edges) > max_edge_candidates:
        del output_edges[max_edge_candidates:]
        truncated = True
        _add_issue(issues, "edge_candidates_limit")

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "task_id": raw_task_id,
        "video_duration": duration,
        "clips": output_clips,
        "edge_candidates": output_edges,
        "complete": False,
        "truncated": truncated,
        "generated_at": timestamp,
        "incomplete_reasons": issues,
    }

    while _encoded_size(payload) > max_bytes:
        if payload["clips"]:
            payload["clips"].pop()
        elif payload["edge_candidates"]:
            payload["edge_candidates"].pop()
        else:
            raise ValueError("时间轴元数据超过 max_bytes，无法生成契约")
        truncated = True
        _add_issue(issues, "payload_limit")
        payload["truncated"] = True
        payload["incomplete_reasons"] = issues

    payload["complete"] = not issues and not truncated
    return payload


# 让调用方可以使用更贴近 owner 语义的名称，但实现只有一个。
build_timeline = serialize_timeline
serialize_timeline_payload = serialize_timeline


__all__ = [
    "MAX_CLIPS",
    "MAX_EDGE_CANDIDATES",
    "MAX_TIMELINE_BYTES",
    "SCHEMA_VERSION",
    "build_timeline",
    "serialize_timeline",
    "serialize_timeline_payload",
]
