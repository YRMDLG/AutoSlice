"""人工时间轴优化产物的路径、写入与读取。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from autoslice import timecode
from autoslice.analysis.manual import candidates as manual_candidates
from autoslice.analysis.manual import timebase as timeline_analysis
from autoslice.streamer_profiles import current_streamer_profile

FACADE_EXPORTS = {
    "_optimized_timeline_paths": "optimized_timeline_paths",
    "_write_optimized_timeline_files": "write_optimized_timeline_files",
    "_load_optimized_timeline_artifact": "load_optimized_timeline_artifact",
    "_manual_timeline_for_rebuilt_report": "manual_timeline_for_rebuilt_report",
}

MANUAL_TIMELINE_OPTIMIZATION_VERSION = (
    timeline_analysis.MANUAL_TIMELINE_OPTIMIZATION_VERSION
)
_sanitize_optimized_manual_entry = (
    manual_candidates.sanitize_optimized_manual_entry
)


def optimized_timeline_paths(video_base, artifact_layout=None):
    if artifact_layout:
        return (
            artifact_layout["optimized_timeline_json_path"],
            artifact_layout["optimized_timeline_md_path"],
        )
    return video_base + "_优化时间轴.json", video_base + "_优化时间轴.md"


def write_optimized_timeline_files(
        video_base, source_path, raw_entries, optimized_entries, warning=None,
        artifact_layout=None, video_path=None):
    """保存可审阅的优化时间轴，便于判断人工参考如何被字幕校准。"""
    json_path, md_path = optimized_timeline_paths(
        video_base, artifact_layout=artifact_layout
    )
    os.makedirs(os.path.dirname(os.path.abspath(json_path)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(md_path)), exist_ok=True)
    source_video_path = video_path or video_base + ".flv"
    payload = {
        "video_path": str(Path(source_video_path).expanduser().resolve()),
        "source_path": source_path,
        "streamer_profile_id": current_streamer_profile().id,
        "optimization_version": MANUAL_TIMELINE_OPTIMIZATION_VERSION,
        "raw_entry_count": len(raw_entries or []),
        "optimized_entry_count": len(optimized_entries or []),
        "warning": warning,
        "entries": optimized_entries or [],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    lines = [
        "# 字幕校准后的人工时间轴",
        "",
        f"> 原始文件: {source_path or '无'}",
        f"> 原始 {len(raw_entries or [])} 条 → 优化 {len(optimized_entries or [])} 个话题候选",
    ]
    if warning:
        lines.append(f"> 警告: {warning}")
    lines.extend(["", "---", ""])
    for index, entry in enumerate(optimized_entries or [], 1):
        stars = " ⭐" * min(int(entry.get("stars", 0)), 5)
        confidence = (
            "字幕/AI初审（完整分析时再次独立复核）"
            if entry.get("ai_enriched")
            else "低权重参考"
        )
        lines.append(
            f"## {index:02d} [{timecode.format_elapsed(entry['start'])}－{timecode.format_elapsed(entry['end'])}] "
            f"{entry.get('text', '未命名话题')}{stars}"
        )
        lines.append(f"- 状态: {confidence}")
        adjustments = [
            f"{timecode.format_elapsed(item.get('original_start', item.get('start', 0)))}→"
            f"{timecode.format_elapsed(item.get('start', 0))} ({int(item.get('alignment_shift_sec', 0)):+d}秒)"
            for item in entry.get("original_entries") or []
            if int(item.get("alignment_shift_sec", 0)) != 0
        ]
        if adjustments:
            lines.append(f"- 字幕校时: {'；'.join(adjustments[:4])}")
        for point in entry.get("summary") or []:
            lines.append(f"- {point}")
        lines.append("")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    return json_path, md_path


def load_optimized_timeline_artifact(
        artifact_path, flv_path, manual_timeline_path=None):
    """加载独立优化产物，并核对录播及原始 DOCX，避免串用时间轴。"""
    if not artifact_path or not os.path.isfile(artifact_path):
        raise FileNotFoundError(f"优化时间轴文件不存在: {artifact_path or '未选择'}")
    try:
        with open(artifact_path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"优化时间轴 JSON 无法读取: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise ValueError("优化时间轴 JSON 缺少 entries 数组")

    def normalized(path):
        return os.path.normcase(os.path.abspath(str(path or "")))

    artifact_video_path = payload.get("video_path")
    if not artifact_video_path or normalized(artifact_video_path) != normalized(flv_path):
        raise ValueError("优化时间轴不属于当前选择的录播文件")
    source_path = payload.get("source_path")
    if manual_timeline_path and normalized(source_path) != normalized(manual_timeline_path):
        raise ValueError("优化时间轴与当前选择的人工 DOCX 不一致")

    sanitized_entries = []
    for entry in payload["entries"]:
        if not isinstance(entry, dict):
            continue
        sanitized = _sanitize_optimized_manual_entry(entry)
        if sanitized:
            sanitized_entries.append(sanitized)
    dropped_count = len(payload["entries"]) - len(sanitized_entries)
    warning = str(payload.get("warning") or "").strip()
    if dropped_count:
        grounding_warning = (
            f"已忽略 {dropped_count} 个与原人工记录语义不符的优化候选"
        )
        warning = "；".join(item for item in (warning, grounding_warning) if item)

    return {
        "path": source_path,
        "entries": sanitized_entries,
        "source_entry_count": int(payload.get("raw_entry_count", 0)),
        "raw_entry_count": int(payload.get("raw_entry_count", 0)),
        "optimized_entry_count": len(sanitized_entries),
        "optimized_json_path": artifact_path,
        "optimized_md_path": os.path.splitext(artifact_path)[0] + ".md",
        "optimization_warning": warning or None,
        "optimization_version": int(payload.get("optimization_version", 0)),
        "streamer_profile_id": payload.get("streamer_profile_id"),
        "mode": "optimized_artifact",
        "video_start": timeline_analysis.extract_video_start_datetime(flv_path),
    }


def manual_timeline_for_rebuilt_report(summary, flv_path):
    """从现有 JSON 恢复报告头所需的人工时间轴元数据。"""
    summary = dict(summary or {})
    optimized_path = summary.get("optimized_json_path")
    source_path = summary.get("path")
    if optimized_path and os.path.isfile(optimized_path):
        try:
            return load_optimized_timeline_artifact(
                optimized_path,
                flv_path,
                manual_timeline_path=source_path,
            )
        except (OSError, ValueError, TypeError):
            pass
    entry_count = int(summary.get("entry_count", 0) or 0)
    star_count = min(entry_count, int(summary.get("star_count", 0) or 0))
    summary["entries"] = [
        {"stars": 1 if index < star_count else 0}
        for index in range(entry_count)
    ]
    return summary
