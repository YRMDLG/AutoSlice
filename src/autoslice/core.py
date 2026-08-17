"""AutoSlice 已弃用的旧核心兼容入口。

新产品代码不得依赖本模块。这里只保留仍有明确契约价值的 SRT 转发、
JSON 标记解析，以及会立即报告迁移方式的 ``process_video`` 退役占位符。
旧弹幕密度、DOCX 时间轴、边界扩展和 FFmpeg 实现不再受支持。
"""

import json
import os


__all__ = [
    "LegacyCoreDeprecatedError",
    "generate_srt",
    "parse_timeline_json",
    "process_video",
]


class LegacyCoreDeprecatedError(RuntimeError):
    """调用已退役的 ``core`` 工作流时抛出的明确错误。"""


def generate_srt(video_path, progress_callback=None):
    """兼容旧 ASR 入口，转发到原子且可续跑的唯一字幕实现。"""
    try:
        from autoslice.topic_engine import ensure_srt

        return ensure_srt(video_path, progress_callback=progress_callback)
    except Exception as exc:
        if progress_callback:
            progress_callback(f"识别失败: {exc}", 0, 1)
        return None


def parse_timeline_json(json_path):
    """解析旧 JSON 标记并完整保留起止范围与时间基准。"""
    if not json_path or not os.path.exists(json_path):
        return []
    with open(json_path, encoding="utf-8") as file:
        data = json.load(file)

    marks = []
    items = data.get("clip_marks") or data.get("topics") or []
    default_time_basis = data.get("time_basis", "video_elapsed_seconds")
    for item in items:
        try:
            start = float(item.get("start", item.get("topic_start")))
            end = float(item.get("end", item.get("topic_end")))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue

        topic_start = item.get("topic_start", start)
        topic_end = item.get("topic_end", end)
        try:
            topic_start = float(topic_start)
            topic_end = float(topic_end)
        except (TypeError, ValueError):
            topic_start, topic_end = start, end
        marks.append({
            "start": start,
            "end": end,
            "topic_start": topic_start,
            "topic_end": topic_end,
            "title": str(item.get("title") or "未命名片段").strip(),
            "time_basis": item.get("time_basis", default_time_basis),
        })
    return marks


def process_video(
        flv_path, ass_path, output_dir, mode="danmaku",
        timeline_path=None, timeline_json=None, progress_callback=None):
    """拒绝运行已退役的第二套切片工作流。"""
    raise LegacyCoreDeprecatedError(
        "core.process_video() 已退役，不再支持弹幕、DOCX 时间轴或混合直切；"
        "请先运行智能分析生成 clip_marks.json，再调用 "
        "topic_engine.slice_from_marks() 重新切片。"
    )
