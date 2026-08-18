"""报告渲染、产物整理和精调任务清单的唯一实现。"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime

from autoslice import timecode
from autoslice.analysis import candidates as candidate_analysis
from autoslice.analysis import boundaries as boundary_analysis
from autoslice.analysis.report import formatting as topic_formatting
from autoslice.artifact_store import (
    ARTIFACT_LAYOUT_VERSION,
    ARTIFACT_QUEUE_DIRNAME,
    UNIFIED_REFINEMENT_QUEUE_JSON,
    UNIFIED_REFINEMENT_QUEUE_MD,
    artifact_bundle_layout as _calculate_artifact_bundle_layout,
    copy_artifact_file as _copy_artifact_file,
    first_existing_artifact_path as _first_existing_artifact_path,
    load_artifact_json as _load_artifact_json,
    markdown_relative_artifact_link as _markdown_relative_artifact_link,
    rewrite_organized_report_links as _rewrite_organized_report_links,
    write_artifact_json as _write_artifact_json,
    write_artifact_text as _write_artifact_text,
)
from autoslice.media_formats import (
    compatible_output_extensions,
    preferred_output_extension,
)
from autoslice.runtime_config import OUTPUT_DIR

FACADE_EXPORTS = {
    'LLM_MODEL': 'LLM_REVIEW_MODEL',
    'DEFAULT_REFINEMENT_QUEUE_DIR': 'DEFAULT_REFINEMENT_QUEUE_DIR',
    '_UNIFIED_REFINEMENT_QUEUE_LOCK': '_UNIFIED_REFINEMENT_QUEUE_LOCK',
    'REFINEMENT_WORKFLOW_STEPS': 'REFINEMENT_WORKFLOW_STEPS',
    '_GENERATED_REPORT_TOPIC_RE': '_GENERATED_REPORT_TOPIC_RE',
    '_parse_generated_topic_report': 'parse_generated_topic_report',
    '_strip_emoji_for_title': '_strip_emoji_for_title',
    '_make_part_title': '_make_part_title',
    '_group_topics_for_parts': '_group_topics_for_parts',
    '_group_topics_by_hour': '_group_topics_by_hour',
    '_topic_clip_filename': 'topic_clip_filename',
    '_compatible_topic_clip_filenames': 'compatible_topic_clip_filenames',
    '_synchronise_selected_topic_ranges': 'synchronise_selected_topic_ranges',
    '_clip_subtitle_filename': 'clip_subtitle_filename',
    '_resolve_clip_subtitle_source': 'resolve_clip_subtitle_source',
    '_publish_title_report_lines': 'publish_title_report_lines',
    '_artifact_bundle_layout': 'artifact_bundle_layout',
    '_render_artifact_overview': 'render_artifact_overview',
    'organize_existing_artifacts': 'organize_existing_artifacts',
    '_build_refinement_manifest': 'build_refinement_manifest',
    '_render_refinement_manifest_markdown': 'render_refinement_manifest_markdown',
    '_unified_refinement_queue_paths': '_unified_refinement_queue_paths',
    '_refinement_task_is_completed': '_refinement_task_is_completed',
    '_unified_refinement_record': '_unified_refinement_record',
    '_render_unified_refinement_queue_markdown': 'render_unified_refinement_queue_markdown',
    '_upsert_unified_refinement_queue': 'upsert_unified_refinement_queue',
    '_write_refinement_manifest_files': 'write_refinement_manifest_files',
    '_update_refinement_manifest_after_slice': 'update_refinement_manifest_after_slice',
    '_build_timeline_report': 'build_timeline_report',
}


_clean_topic_title = candidate_analysis._clean_topic_title
_filter_unsupported_ai_points = candidate_analysis._filter_unsupported_ai_points
_parse_hms = timecode.parse_hms
fmt_time = timecode.format_elapsed
_replace_streamer_role = candidate_analysis._replace_streamer_role
_format_report_time = topic_formatting.format_report_time
_dedupe_clip_marks = boundary_analysis._dedupe_clip_marks
_normalise_publish_title = candidate_analysis._normalise_publish_title
_format_topic_block = topic_formatting.format_topic_block
LLM_ANALYSIS_MODEL = candidate_analysis.LLM_ANALYSIS_MODEL



LLM_REVIEW_MODEL = (
    os.environ.get("AUTOSLICE_LLM_REVIEW_MODEL", "").strip()
    or "gpt-5.6-terra"
)


DEFAULT_REFINEMENT_QUEUE_DIR = os.environ.get(
    "AUTOSLICE_REFINEMENT_QUEUE_DIR",
    str(OUTPUT_DIR),
)


_UNIFIED_REFINEMENT_QUEUE_LOCK = threading.Lock()


REFINEMENT_WORKFLOW_STEPS = (
    ("verify_context", "核查前后文"),
    ("trim_breath", "剪气口与停顿"),
    ("correct_subtitles", "精剪导出后自动识别、校对并压制字幕"),
    ("add_intro_outro", "添加片头片尾"),
    ("export_video", "导出精调成片"),
    ("make_cover", "用 AutoCover 制作封面"),
    ("publish_bilibili", "在 B 站网页投稿"),
)


_GENERATED_REPORT_TOPIC_RE = re.compile(
    r'^\s*(?:[①-⑳㉑-㊿]|\d+[.、)])\s*'
    r'\[\s*(?P<start>\d{1,3}:\d{2}(?::\d{2})?)\s*[－—–~-]\s*'
    r'(?P<end>\d{1,3}:\d{2}(?::\d{2})?)\s*\]\s*(?P<title>.+?)\s*$'
)


def parse_generated_topic_report(report_path):
    """从已有逐话题报告恢复首轮话题，供仅重做候选复核使用。"""
    if not report_path or not os.path.isfile(report_path):
        raise FileNotFoundError(f"话题报告不存在: {report_path or '未指定'}")
    with open(report_path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    if not any("时间基准" in line and "视频内时间" in line for line in lines):
        raise ValueError("话题报告未声明视频内时间基准，不能安全恢复候选")

    topics = []
    current = None
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("## 投稿标题建议") or line.startswith("## 分析警告"):
            current = None
            break
        match = _GENERATED_REPORT_TOPIC_RE.match(line)
        if match:
            try:
                start = timecode.parse_hms(match.group("start"))
                end = timecode.parse_hms(match.group("end"))
            except (TypeError, ValueError):
                current = None
                continue
            title = re.sub(r'[✂⭐★]\ufe0f?', '', match.group("title")).strip()
            title = _clean_topic_title(title)
            if end <= start or not title:
                current = None
                continue
            current = {
                "start": start,
                "end": end,
                "start_str": timecode.format_elapsed(start),
                "end_str": timecode.format_elapsed(end),
                "title": title,
                "body": [],
                "can_slice": False,
                "source": "recovered_report",
                "recovered_from_report": True,
            }
            topics.append(current)
            continue
        if current and line.startswith(("·", "●")):
            if line.startswith("·切片核心："):
                continue
            current["body"].append(line)

    for topic in topics:
        topic["body"] = _filter_unsupported_ai_points(topic.get("body") or [])
        body_text = " ".join(topic.get("body") or [])
        if "未形成稳定可切片主题" in body_text or "暂不标记为自动切片" in body_text:
            topic["fallback"] = True
    if not topics:
        raise ValueError("话题报告中没有可恢复的逐话题条目")
    return topics


def _strip_emoji_for_title(title):
    """给 Part 标题做轻量清理，避免标题太花。"""
    return re.sub(r'^[^\w\u4e00-\u9fff]+', '', title).strip() or title


def _make_part_title(topics, streamer_name=None):
    """根据 Part 内话题生成阶段标题。"""
    titles = [_strip_emoji_for_title(_replace_streamer_role(t["title"], streamer_name)) for t in topics if t.get("title")]
    if not titles:
        return "阶段话题整理"
    if len(titles) == 1:
        return titles[0]
    first, second = titles[0], titles[1]
    if len(first) + len(second) <= 18:
        return f"{first}与{second}"
    return f"{first}等话题"


def _group_topics_for_parts(topics, part_seconds=900):
    """按约 15 分钟一段聚合话题，生成 Part。"""
    sorted_topics = sorted(topics, key=lambda t: (t["start"], t["end"]))
    groups = []
    current = []
    group_start = None
    for topic in sorted_topics:
        if not current:
            current = [topic]
            group_start = topic["start"]
            continue
        if topic["start"] - group_start >= part_seconds:
            groups.append(current)
            current = [topic]
            group_start = topic["start"]
        else:
            current.append(topic)
    if current:
        groups.append(current)
    return groups


def _group_topics_by_hour(topics):
    """按视频内自然小时聚合话题，生成“每小时重点”。"""
    sorted_topics = sorted(topics, key=lambda t: (t["start"], t["end"]))
    buckets = []
    current_hour = None
    current = []
    for topic in sorted_topics:
        hour = int(topic["start"] // 3600)
        if current_hour is None:
            current_hour = hour
            current = [topic]
            continue
        if hour != current_hour:
            buckets.append((current_hour, current))
            current_hour = hour
            current = [topic]
        else:
            current.append(topic)
    if current:
        buckets.append((current_hour, current))
    return buckets


def topic_clip_filename(index, mark, source_path=None):
    """生成自动切片文件名；报告和实际 ffmpeg 输出必须共用此规则。"""
    title = str(mark.get("title", f"片段{index}")).strip() or f"片段{index}"
    safe_title = re.sub(r'[\\/:*?"<>|`]', '', title)
    safe_title = re.sub(r'\s+', ' ', safe_title).strip(' .')[:30]
    if not safe_title:
        safe_title = f"片段{index}"
    start_s = int(float(mark.get("start", 0)))
    output_extension = preferred_output_extension(source_path or ".flv")
    return f"{index:02d}_{start_s}s_{safe_title}{output_extension}"


def compatible_topic_clip_filenames(index, mark, source_path):
    """返回首选文件名以及读取历史产物时允许回退的文件名。"""

    preferred_name = topic_clip_filename(index, mark, source_path)
    filename_stem = os.path.splitext(preferred_name)[0]
    return tuple(
        filename_stem + extension
        for extension in compatible_output_extensions(source_path)
    )


def synchronise_selected_topic_ranges(topics, clip_marks):
    """将字幕证据后移的核心起点同步回报告，避免报告继续显示上一案例时间。"""
    used = set()
    for mark in clip_marks or []:
        candidates = [
            (index, topic)
            for index, topic in enumerate(topics or [])
            if index not in used and topic.get("title") == mark.get("title")
        ]
        if not candidates:
            continue
        report_start = int(mark.get("report_start", mark.get("topic_start", 0)))
        index, topic = min(
            candidates,
            key=lambda item: abs(int(item[1].get("start", 0)) - report_start),
        )
        used.add(index)
        if report_start > int(topic.get("start", report_start)):
            topic["start"] = report_start
            topic["start_str"] = topic_formatting.format_report_time(report_start)


def clip_subtitle_filename(clip_filename):
    """片段字幕与视频同名，便于剪映成对导入。"""
    return os.path.splitext(clip_filename)[0] + ".srt"


def resolve_clip_subtitle_source(flv_path, data):
    """优先使用流水线校对字幕，兼容旧 JSON 回退到同名 SRT。"""
    layout = None
    if isinstance(data, dict) and data.get("artifact_dir"):
        layout = artifact_bundle_layout(
            flv_path,
            artifact_dir=data.get("artifact_dir"),
        )
    video_base = os.path.splitext(flv_path)[0]
    candidates = [
        data.get("corrected_srt_path"),
        layout["corrected_srt_path"] if layout else None,
        video_base + "_校对字幕.srt",
        data.get("srt_path"),
        video_base + ".srt",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return None


def publish_title_report_lines(clip_marks, source_path=None):
    """生成 AutoCover 可直接解析的投稿标题区，只包含最终实际切片。"""
    marks = _dedupe_clip_marks(clip_marks or [])
    if not marks:
        return []
    lines = ["## 投稿标题建议", ""]
    for index, mark in enumerate(marks, 1):
        start = topic_formatting.format_report_time(mark["start"])
        end = topic_formatting.format_report_time(mark["end"])
        filename = topic_clip_filename(index, mark, source_path)
        publish_title = _normalise_publish_title(
            mark.get("publish_title"), mark.get("title", "未命名片段")
        )
        lines.extend([
            f"### {index:02d}（{start}－{end}）",
            "",
            f"原文件：`{filename}`",
            "",
            f"**{publish_title}**",
            "",
        ])
    return lines


def artifact_bundle_layout(video_path, output_dir=None, artifact_dir=None):
    return _calculate_artifact_bundle_layout(
        video_path,
        output_dir=output_dir,
        artifact_dir=artifact_dir,
        default_output_dir=DEFAULT_REFINEMENT_QUEUE_DIR,
    )


def render_artifact_overview(layout, clip_data=None, manifest=None, slice_dir=None):
    """渲染面向日常剪辑的短概览，不复制完整话题正文。"""
    clip_data = clip_data if isinstance(clip_data, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    marks = _dedupe_clip_marks(clip_data.get("clip_marks") or [])
    slice_dir = os.path.abspath(
        slice_dir or manifest.get("slice_output_dir") or layout["slice_dir"]
    )
    tasks_by_filename = {
        task.get("clip_filename"): task
        for task in manifest.get("tasks") or []
        if isinstance(task, dict) and task.get("clip_filename")
    }
    lines = [
        f"# {os.path.basename(layout['source_video_path'])} 自动切片概览",
        "",
        f"> 自动生成 | 最终切片 {len(marks)} 个 | "
        f"更新时间: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 入口",
        "",
        f"- 源录播: `{layout['source_video_path']}`",
        f"- 实际切片目录: `{slice_dir}`",
    ]
    readable_files = (
        ("完整话题分析", layout["report_path"], "01_话题分析.md"),
        ("精调任务清单", layout["task_manifest_md_path"], "02_精调任务.md"),
        ("字幕校准后的人工时间轴", layout["optimized_timeline_md_path"], "03_优化时间轴.md"),
    )
    for label, path, relative_name in readable_files:
        if os.path.isfile(path):
            lines.append(f"- {label}: [{relative_name}](./{relative_name})")
    lines.extend(["", "## 最终切片", ""])
    if not marks:
        lines.extend(["本次没有最终可切片段。", ""])
        return "\n".join(lines)
    for index, mark in enumerate(marks, 1):
        candidate_filenames = compatible_topic_clip_filenames(
            index,
            mark,
            layout["source_video_path"],
        )
        task = next(
            (
                tasks_by_filename[name]
                for name in candidate_filenames
                if name in tasks_by_filename
            ),
            {},
        )
        existing_filename = next(
            (
                name
                for name in candidate_filenames
                if os.path.isfile(os.path.join(slice_dir, name))
            ),
            None,
        )
        filename = (
            existing_filename
            or task.get("clip_filename")
            or candidate_filenames[0]
        )
        task_slice_path = task.get("slice_path")
        clip_path = (
            task_slice_path
            if task_slice_path and os.path.isfile(task_slice_path)
            else os.path.join(slice_dir, filename)
        )
        title = str(mark.get("title") or f"片段{index}").strip()
        publish_title = _normalise_publish_title(
            mark.get("publish_title") or task.get("publish_title"),
            title,
        )
        start = float(mark.get("start", 0) or 0)
        end = float(mark.get("end", start) or start)
        lines.extend([
            f"### {index:02d} {title}",
            "",
            f"- 视频内时间: {topic_formatting.format_report_time(start)}－{topic_formatting.format_report_time(end)}"
            f"（{max(0, int(round(end - start)))} 秒）",
            f"- 投稿标题: {publish_title}",
            *(
                [f"- 系列收播片: {mark.get('series_title') or title}"]
                if mark.get("clip_type") == "stream_outro" else []
            ),
            f"- 切片文件: `{os.path.abspath(clip_path)}`",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def organize_existing_artifacts(
        flv_path, output_dir=None, json_path=None, report_path=None,
        slice_dir=None, artifact_dir=None):
    """把旧版散落的小型产物复制进整理包，并改写整理包内部引用。"""
    flv_path = os.path.abspath(flv_path)
    if not os.path.isfile(flv_path):
        raise FileNotFoundError(f"录播文件不存在: {flv_path}")
    layout = artifact_bundle_layout(
        flv_path, output_dir=output_dir, artifact_dir=artifact_dir
    )
    os.makedirs(layout["data_dir"], exist_ok=True)
    legacy_base = os.path.splitext(flv_path)[0]
    legacy_clip_json_path = _first_existing_artifact_path(
        json_path,
        layout["clip_marks_path"],
        legacy_base + "_clip_marks.json",
    )
    legacy_clip_data = _load_artifact_json(legacy_clip_json_path)
    legacy_manual = legacy_clip_data.get("manual_timeline")
    legacy_manual = legacy_manual if isinstance(legacy_manual, dict) else {}
    source_paths = {
        "report_path": _first_existing_artifact_path(
            report_path, layout["report_path"], legacy_base + "_话题分析.md"
        ),
        "task_manifest_md_path": _first_existing_artifact_path(
            legacy_clip_data.get("task_manifest_md_path"),
            layout["task_manifest_md_path"],
            legacy_base + "_精调任务.md",
        ),
        "optimized_timeline_md_path": _first_existing_artifact_path(
            legacy_manual.get("optimized_md_path"),
            layout["optimized_timeline_md_path"],
            legacy_base + "_优化时间轴.md",
        ),
        "clip_marks_path": legacy_clip_json_path,
        "task_manifest_json_path": _first_existing_artifact_path(
            legacy_clip_data.get("task_manifest_json_path"),
            layout["task_manifest_json_path"],
            legacy_base + "_精调任务.json",
        ),
        "optimized_timeline_json_path": _first_existing_artifact_path(
            legacy_manual.get("optimized_json_path"),
            layout["optimized_timeline_json_path"],
            legacy_base + "_优化时间轴.json",
        ),
        "asr_checkpoint_path": _first_existing_artifact_path(
            layout["asr_checkpoint_path"], legacy_base + "_asr_checkpoint.json"
        ),
        "topic_analysis_checkpoint_path": _first_existing_artifact_path(
            legacy_clip_data.get("topic_analysis_checkpoint_path"),
            layout["topic_analysis_checkpoint_path"],
            legacy_base + "_topic_analysis_checkpoint.json",
        ),
        "clip_review_checkpoint_path": _first_existing_artifact_path(
            legacy_clip_data.get("clip_review_checkpoint_path"),
            layout["clip_review_checkpoint_path"],
            legacy_base + "_clip_review_checkpoint.json",
        ),
        "corrected_srt_path": _first_existing_artifact_path(
            legacy_clip_data.get("corrected_srt_path"),
            layout["corrected_srt_path"],
            legacy_base + "_校对字幕.srt",
        ),
    }
    copied_files = []
    for key, source_path in source_paths.items():
        copied = _copy_artifact_file(source_path, layout[key])
        if copied:
            copied_files.append(copied)

    clip_data = _load_artifact_json(layout["clip_marks_path"])
    manifest = _load_artifact_json(layout["task_manifest_json_path"])
    actual_slice_dir = os.path.abspath(
        slice_dir or manifest.get("slice_output_dir") or layout["slice_dir"]
    )
    if manifest:
        manifest.update({
            "artifact_layout_version": ARTIFACT_LAYOUT_VERSION,
            "artifact_dir": layout["artifact_dir"],
            "overview_path": layout["overview_path"],
            "analysis_report_path": (
                layout["report_path"] if os.path.isfile(layout["report_path"]) else None
            ),
            "clip_marks_path": layout["clip_marks_path"],
            "manifest_json_path": layout["task_manifest_json_path"],
            "manifest_md_path": layout["task_manifest_md_path"],
            "corrected_srt_path": (
                layout["corrected_srt_path"]
                if os.path.isfile(layout["corrected_srt_path"])
                else manifest.get("corrected_srt_path")
            ),
            "slice_output_dir": actual_slice_dir,
            "unified_queue_json_path": layout["unified_queue_json_path"],
            "unified_queue_md_path": layout["unified_queue_md_path"],
        })
        for task in manifest.get("tasks") or []:
            if not isinstance(task, dict) or not task.get("clip_filename"):
                continue
            filename_stem = os.path.splitext(task["clip_filename"])[0]
            candidate_paths = [
                os.path.abspath(os.path.join(
                    actual_slice_dir,
                    filename_stem + extension,
                ))
                for extension in compatible_output_extensions(flv_path)
            ]
            candidate = next(
                (path for path in candidate_paths if os.path.isfile(path)),
                candidate_paths[0],
            )
            subtitle = os.path.splitext(candidate)[0] + ".srt"
            if os.path.isfile(candidate):
                task["clip_filename"] = os.path.basename(candidate)
                task["slice_path"] = candidate
            if os.path.isfile(subtitle):
                task["subtitle_path"] = subtitle
        try:
            upsert_unified_refinement_queue(
                manifest,
                queue_json_path=layout["unified_queue_json_path"],
                queue_md_path=layout["unified_queue_md_path"],
            )
            manifest["unified_queue_warning"] = None
        except (OSError, ValueError, TypeError) as exc:
            manifest["unified_queue_warning"] = f"精调总清单更新失败: {exc}"
        _write_artifact_json(layout["task_manifest_json_path"], manifest)
        _write_artifact_text(
            layout["task_manifest_md_path"],
            render_refinement_manifest_markdown(manifest),
        )

    if clip_data:
        clip_data.update({
            "artifact_layout_version": ARTIFACT_LAYOUT_VERSION,
            "artifact_dir": layout["artifact_dir"],
            "overview_path": layout["overview_path"],
            "analysis_report_path": (
                layout["report_path"] if os.path.isfile(layout["report_path"]) else None
            ),
            "task_manifest_json_path": layout["task_manifest_json_path"],
            "task_manifest_md_path": layout["task_manifest_md_path"],
            "unified_queue_json_path": layout["unified_queue_json_path"],
            "unified_queue_md_path": layout["unified_queue_md_path"],
            "clip_review_checkpoint_path": layout["clip_review_checkpoint_path"],
            "topic_analysis_checkpoint_path": layout["topic_analysis_checkpoint_path"],
            "corrected_srt_path": (
                layout["corrected_srt_path"]
                if os.path.isfile(layout["corrected_srt_path"])
                else clip_data.get("corrected_srt_path")
            ),
        })
        manual_timeline = clip_data.get("manual_timeline")
        if isinstance(manual_timeline, dict):
            if os.path.isfile(layout["optimized_timeline_json_path"]):
                manual_timeline["optimized_json_path"] = layout[
                    "optimized_timeline_json_path"
                ]
            if os.path.isfile(layout["optimized_timeline_md_path"]):
                manual_timeline["optimized_md_path"] = layout[
                    "optimized_timeline_md_path"
                ]
        _write_artifact_json(layout["clip_marks_path"], clip_data)

    _rewrite_organized_report_links(layout)

    overview = render_artifact_overview(
        layout,
        clip_data=clip_data,
        manifest=manifest,
        slice_dir=actual_slice_dir,
    )
    _write_artifact_text(layout["overview_path"], overview)
    _write_artifact_text(
        layout["slice_pointer_path"],
        actual_slice_dir + "\n",
    )
    copied_files.extend([layout["overview_path"], layout["slice_pointer_path"]])
    return {
        **layout,
        "slice_dir": actual_slice_dir,
        "clip_count": len(_dedupe_clip_marks(clip_data.get("clip_marks") or [])),
        "copied_files": sorted(set(copied_files)),
    }


def build_refinement_manifest(video_path, source_srt_path, corrected_srt_path,
                               analysis_report_path, clip_marks_path, clip_marks,
                               manifest_json_path, manifest_md_path):
    """构造一场录播的统一精调任务数据。"""
    tasks = []
    for index, mark in enumerate(_dedupe_clip_marks(clip_marks or []), 1):
        filename = topic_clip_filename(index, mark, video_path)
        tasks.append({
            "id": f"{index:02d}",
            "status": "等待自动切片",
            "clip_filename": filename,
            "slice_path": None,
            "subtitle_path": None,
            "start": int(mark["start"]),
            "end": int(mark["end"]),
            "duration": int(mark["end"] - mark["start"]),
            "topic_start": int(mark.get("topic_start", mark["start"])),
            "topic_end": int(mark.get("topic_end", mark["end"])),
            "topic_title": mark.get("title", "未命名片段"),
            "clip_type": mark.get("clip_type", "topic"),
            "series_title": mark.get("series_title"),
            "outro_trigger": mark.get("outro_trigger"),
            "preserve_to_video_end": bool(mark.get("preserve_to_video_end")),
            "publish_title": _normalise_publish_title(
                mark.get("publish_title"), mark.get("title", "未命名片段")
            ),
            "natural_boundary_pre_sec": int(mark.get("natural_boundary_pre_sec", 0)),
            "natural_boundary_post_sec": int(mark.get("natural_boundary_post_sec", 0)),
            "steps": [
                {"key": key, "label": label, "status": "待处理"}
                for key, label in REFINEMENT_WORKFLOW_STEPS
            ],
        })
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "schema_version": 1,
        "status": "等待自动切片" if tasks else "无可切片段",
        "generated_at": now,
        "updated_at": now,
        "video_name": os.path.basename(video_path),
        "source_video_path": os.path.abspath(video_path),
        "source_srt_path": os.path.abspath(source_srt_path) if source_srt_path else None,
        "corrected_srt_path": os.path.abspath(corrected_srt_path) if corrected_srt_path else None,
        "analysis_report_path": os.path.abspath(analysis_report_path),
        "clip_marks_path": os.path.abspath(clip_marks_path),
        "manifest_json_path": os.path.abspath(manifest_json_path),
        "manifest_md_path": os.path.abspath(manifest_md_path),
        "slice_output_dir": None,
        "tasks": tasks,
    }


def render_refinement_manifest_markdown(manifest):
    """把精调任务数据渲染成可直接勾选的 Markdown。"""
    lines = [
        f"# {manifest.get('video_name', '录播')} 精调任务清单",
        f"> 自动生成 | 总状态: {manifest.get('status', '待处理')} | "
        f"更新时间: {manifest.get('updated_at', '')}",
        "",
        "## 文件",
        "",
        f"- 源录播: `{manifest.get('source_video_path') or '无'}`",
        f"- 校对字幕: `{manifest.get('corrected_srt_path') or '无'}`",
        f"- 话题报告: `{manifest.get('analysis_report_path') or '无'}`",
        f"- 切片标记: `{manifest.get('clip_marks_path') or '无'}`",
        f"- 切片目录: `{manifest.get('slice_output_dir') or '等待自动切片'}`",
        f"- 精调总清单: `{manifest.get('unified_queue_md_path') or '未启用'}`",
        "",
        "## 切片队列",
        "",
    ]
    tasks = manifest.get("tasks") or []
    if not tasks:
        lines.append("本次没有可切片段。")
        lines.append("")
        return "\n".join(lines)
    for task in tasks:
        lines.extend([
            f"### {task.get('id')} {task.get('topic_title', '未命名片段')}",
            "",
            f"- 状态: {task.get('status', '待处理')}",
            f"- 视频内时间: {topic_formatting.format_report_time(task.get('start', 0))}－"
            f"{topic_formatting.format_report_time(task.get('end', 0))}（{task.get('duration', 0)} 秒）",
            f"- 切片文件: `{task.get('slice_path') or task.get('clip_filename')}`",
            f"- 片段字幕: `{task.get('subtitle_path') or '精剪导出后在字幕校对页识别'}`",
            f"- 投稿标题: {task.get('publish_title', '')}",
        ])
        if task.get("clip_type") == "stream_outro":
            lines.append(
                f"- 系列收播片: {task.get('series_title') or task.get('topic_title')}"
                f"（触发语：{task.get('outro_trigger') or '收播口令'}）"
            )
        for step in task.get("steps") or []:
            checked = "x" if step.get("status") == "已完成" else " "
            lines.append(f"- [{checked}] {step.get('label')}（{step.get('status', '待处理')}）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _unified_refinement_queue_paths(queue_dir=None):
    root = os.path.abspath(
        queue_dir
        or os.path.join(DEFAULT_REFINEMENT_QUEUE_DIR, ARTIFACT_QUEUE_DIRNAME)
    )
    return (
        os.path.join(root, UNIFIED_REFINEMENT_QUEUE_JSON),
        os.path.join(root, UNIFIED_REFINEMENT_QUEUE_MD),
    )


def _refinement_task_is_completed(task):
    status = str(task.get("status", "")).strip()
    if status in {"已完成", "已发布", "已投稿"}:
        return True
    steps = task.get("steps") or []
    return bool(steps) and all(step.get("status") == "已完成" for step in steps)


def _unified_refinement_record(manifest):
    """从单场清单提取总队列需要的信息，保留剪映阶段的关键路径和首尾依据。"""
    tasks = []
    for task in manifest.get("tasks") or []:
        tasks.append({
            "id": task.get("id"),
            "status": task.get("status", "待处理"),
            "topic_title": task.get("topic_title", "未命名片段"),
            "clip_type": task.get("clip_type", "topic"),
            "series_title": task.get("series_title"),
            "outro_trigger": task.get("outro_trigger"),
            "preserve_to_video_end": bool(task.get("preserve_to_video_end")),
            "publish_title": task.get("publish_title", ""),
            "start": int(task.get("start", 0)),
            "end": int(task.get("end", 0)),
            "duration": int(task.get("duration", 0)),
            "topic_start": int(task.get("topic_start", task.get("start", 0))),
            "topic_end": int(task.get("topic_end", task.get("end", 0))),
            "natural_boundary_pre_sec": int(task.get("natural_boundary_pre_sec", 0)),
            "natural_boundary_post_sec": int(task.get("natural_boundary_post_sec", 0)),
            "clip_filename": task.get("clip_filename"),
            "slice_path": task.get("slice_path"),
            "subtitle_path": task.get("subtitle_path"),
            "steps": [dict(step) for step in task.get("steps") or []],
        })
    completed_count = sum(_refinement_task_is_completed(task) for task in tasks)
    ready_count = sum(
        not _refinement_task_is_completed(task) and task.get("status") == "待精调"
        for task in tasks
    )
    waiting_slice_count = sum(task.get("status") == "等待自动切片" for task in tasks)
    source_video_path = os.path.abspath(manifest.get("source_video_path") or manifest.get("video_name") or "")
    return {
        "recording_key": os.path.normcase(source_video_path),
        "video_name": manifest.get("video_name", os.path.basename(source_video_path)),
        "status": manifest.get("status", "待处理"),
        "updated_at": manifest.get("updated_at", datetime.now().isoformat(timespec="seconds")),
        "source_video_path": source_video_path,
        "corrected_srt_path": manifest.get("corrected_srt_path"),
        "analysis_report_path": manifest.get("analysis_report_path"),
        "manifest_json_path": manifest.get("manifest_json_path"),
        "manifest_md_path": manifest.get("manifest_md_path"),
        "slice_output_dir": manifest.get("slice_output_dir"),
        "task_count": len(tasks),
        "pending_count": len(tasks) - completed_count,
        "ready_count": ready_count,
        "waiting_slice_count": waiting_slice_count,
        "completed_count": completed_count,
        "tasks": tasks,
    }


def render_unified_refinement_queue_markdown(queue):
    """渲染跨录播总队列，优先展示真正需要进入剪映的任务。"""
    lines = [
        "# AutoSlice 精调任务总清单",
        f"> 自动生成 | {queue.get('recording_count', 0)} 场录播 | "
        f"待处理 {queue.get('pending_count', 0)} 个切片 | "
        f"可进剪映 {queue.get('ready_count', 0)} 个 | "
        f"更新时间: {queue.get('updated_at', '')}",
        "",
        "## 当前队列",
        "",
    ]
    recordings = queue.get("recordings") or []
    if not recordings:
        lines.extend(["目前没有精调任务。", ""])
        return "\n".join(lines)
    for recording in recordings:
        lines.extend([
            f"### {recording.get('video_name', '录播')}",
            "",
            f"- 状态: {recording.get('status', '待处理')}；"
            f"待处理 {recording.get('pending_count', 0)}/{recording.get('task_count', 0)}",
            f"- 校对字幕: `{recording.get('corrected_srt_path') or '无'}`",
            f"- 单场清单: `{recording.get('manifest_md_path') or '无'}`",
            f"- 切片目录: `{recording.get('slice_output_dir') or '等待自动切片'}`",
            "",
        ])
        tasks = recording.get("tasks") or []
        if not tasks:
            lines.extend(["本场没有可切片段。", ""])
            continue
        for task in tasks:
            completed = _refinement_task_is_completed(task)
            checked = "x" if completed else " "
            pre_context = max(0, int(task.get("topic_start", 0)) - int(task.get("start", 0)))
            post_context = max(0, int(task.get("end", 0)) - int(task.get("topic_end", 0)))
            lines.extend([
                f"- [{checked}] {task.get('id', '')} {task.get('topic_title', '未命名片段')}"
                f"（{task.get('status', '待处理')}，{task.get('duration', 0)} 秒）",
                f"  - 视频内时间: {topic_formatting.format_report_time(task.get('start', 0))}－"
                f"{topic_formatting.format_report_time(task.get('end', 0))}",
                f"  - 切片: `{task.get('slice_path') or task.get('clip_filename') or '等待自动切片'}`",
                f"  - 片段字幕: `{task.get('subtitle_path') or '精剪导出后在字幕校对页识别'}`",
                f"  - 首尾: 已在话题核心前保留 {pre_context} 秒、后保留 {post_context} 秒；"
                f"自然停顿额外调整前 {task.get('natural_boundary_pre_sec', 0)} 秒、"
                f"后 {task.get('natural_boundary_post_sec', 0)} 秒",
                f"  - 投稿标题: {task.get('publish_title', '')}",
            ])
            if task.get("clip_type") == "stream_outro":
                lines.append(
                    f"  - 系列收播片: {task.get('series_title') or task.get('topic_title')}"
                    f"（触发语：{task.get('outro_trigger') or '收播口令'}）"
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def upsert_unified_refinement_queue(manifest, queue_json_path=None, queue_md_path=None):
    """按源录播更新总队列；并发流水线通过进程内锁避免互相覆盖。"""
    default_json_path, default_md_path = _unified_refinement_queue_paths()
    json_path = os.path.abspath(queue_json_path or default_json_path)
    md_path = os.path.abspath(queue_md_path or default_md_path)
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    os.makedirs(os.path.dirname(md_path), exist_ok=True)
    record = _unified_refinement_record(manifest)
    with _UNIFIED_REFINEMENT_QUEUE_LOCK:
        queue = {"schema_version": 1, "recordings": []}
        if os.path.isfile(json_path):
            try:
                with open(json_path, encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict) and isinstance(existing.get("recordings"), list):
                    queue = existing
            except (OSError, ValueError, TypeError):
                pass
        recordings = [
            item for item in queue.get("recordings") or []
            if isinstance(item, dict) and item.get("recording_key") != record["recording_key"]
        ]
        recordings.append(record)
        recordings.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        queue.update({
            "schema_version": 1,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "recording_count": len(recordings),
            "task_count": sum(int(item.get("task_count", 0)) for item in recordings),
            "pending_count": sum(int(item.get("pending_count", 0)) for item in recordings),
            "ready_count": sum(int(item.get("ready_count", 0)) for item in recordings),
            "waiting_slice_count": sum(int(item.get("waiting_slice_count", 0)) for item in recordings),
            "completed_count": sum(int(item.get("completed_count", 0)) for item in recordings),
            "recordings": recordings,
        })
        json_temp_path = json_path + ".tmp"
        md_temp_path = md_path + ".tmp"
        with open(json_temp_path, "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
        with open(md_temp_path, "w", encoding="utf-8") as f:
            f.write(render_unified_refinement_queue_markdown(queue))
        os.replace(json_temp_path, json_path)
        os.replace(md_temp_path, md_path)
    return json_path, md_path


def write_refinement_manifest_files(manifest):
    """同步写入 JSON 和 Markdown 两种任务清单。"""
    json_path = manifest["manifest_json_path"]
    md_path = manifest["manifest_md_path"]
    os.makedirs(os.path.dirname(os.path.abspath(json_path)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(md_path)), exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_refinement_manifest_markdown(manifest))
    return json_path, md_path


def update_refinement_manifest_after_slice(manifest_json_path, report_dir, marks):
    """自动切片完成后回写实际文件路径，保留已有人工步骤状态。"""
    if not manifest_json_path or not os.path.isfile(manifest_json_path):
        return False
    with open(manifest_json_path, encoding="utf-8") as f:
        manifest = json.load(f)
    tasks_by_name = {
        task.get("clip_filename"): task
        for task in manifest.get("tasks") or []
        if task.get("clip_filename")
    }
    source_path = (
        manifest.get("source_video_path")
        or manifest.get("video_path")
        or ".flv"
    )
    found_count = 0
    for index, mark in enumerate(_dedupe_clip_marks(marks or []), 1):
        candidate_filenames = compatible_topic_clip_filenames(
            index,
            mark,
            source_path,
        )
        task = next(
            (
                tasks_by_name[name]
                for name in candidate_filenames
                if name in tasks_by_name
            ),
            None,
        )
        if not task:
            continue
        filename = next(
            (
                name
                for name in candidate_filenames
                if os.path.isfile(os.path.join(report_dir, name))
            ),
            candidate_filenames[0],
        )
        task["clip_filename"] = filename
        output_path = os.path.abspath(os.path.join(report_dir, filename))
        task["slice_path"] = output_path
        subtitle_path = os.path.abspath(
            os.path.join(report_dir, clip_subtitle_filename(filename))
        )
        task["subtitle_path"] = subtitle_path if os.path.isfile(subtitle_path) else None
        for step in task.get("steps") or []:
            if step.get("key") == "correct_subtitles":
                step["label"] = "精剪导出后自动识别、校对并压制字幕"
        if os.path.isfile(output_path):
            task["status"] = "待精调"
            found_count += 1
        else:
            task["status"] = "切片文件缺失"
    manifest["slice_output_dir"] = os.path.abspath(report_dir)
    manifest["status"] = "待精调" if found_count else "切片文件缺失"
    manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
    queue_json_path = manifest.get("unified_queue_json_path")
    queue_md_path = manifest.get("unified_queue_md_path")
    if queue_json_path or queue_md_path:
        try:
            upsert_unified_refinement_queue(
                manifest,
                queue_json_path=queue_json_path,
                queue_md_path=queue_md_path,
            )
            manifest["unified_queue_warning"] = None
        except (OSError, ValueError, TypeError) as e:
            manifest["unified_queue_warning"] = f"精调总清单更新失败: {e}"
    write_refinement_manifest_files(manifest)
    return True


def build_timeline_report(
        video_name, peak_info, topics, failed_chunks=None, api_warning=None,
        streamer_name="主播", group_by_hour=False, manual_timeline=None,
        clip_marks=None, corrected_srt_path=None, unified_queue_md_path=None,
        report_dir=None):
    """生成最终 Markdown：逐话题时间轴 + Part 分组。"""
    manual_timeline = manual_timeline or {}
    manual_entries = manual_timeline.get("entries") or []
    lines = [
        f"# {video_name} 话题分析报告",
        f"> 自动生成 | 模型: {LLM_ANALYSIS_MODEL}（整场话题） + "
        f"{LLM_REVIEW_MODEL}（人工时间轴/切片复核） | {peak_info}",
        "> 时间基准：视频内时间/播放进度（不是现实钟点）；实际切片会自动向前后扩展保留上下文",
    ]
    if corrected_srt_path:
        if report_dir:
            lines.append(
                "> 剪映校对字幕: "
                + _markdown_relative_artifact_link(
                    corrected_srt_path,
                    report_dir,
                )
            )
        else:
            lines.append(f"> 剪映校对字幕: {os.path.basename(corrected_srt_path)}")
    if unified_queue_md_path:
        if report_dir:
            queue_link = os.path.relpath(
                unified_queue_md_path, report_dir
            ).replace(os.sep, "/")
            lines.append(
                f"> 精调总清单: [{os.path.basename(unified_queue_md_path)}]"
                f"({queue_link})"
            )
        else:
            lines.append(f"> 精调总清单: {unified_queue_md_path}")
    if manual_timeline.get("path"):
        star_count = sum(1 for item in manual_entries if item.get("stars", 0) > 0)
        source_count = manual_timeline.get("source_entry_count", len(manual_entries))
        raw_count = manual_timeline.get("raw_entry_count", source_count)
        optimized_count = manual_timeline.get("optimized_entry_count")
        if optimized_count is not None:
            count_label = f"当前分段原始 {raw_count} 条 → 字幕优化 {optimized_count} 个候选"
        else:
            count_label = (
                f"当前分段 {len(manual_entries)}/{source_count} 条记录"
                if source_count != len(manual_entries)
                else f"{len(manual_entries)} 条记录"
            )
        lines.append(
            f"> 人工时间轴辅助: {os.path.basename(manual_timeline['path'])} | "
            f"{count_label}, ⭐重点 {star_count} 条"
        )
        if manual_timeline.get("optimized_md_path"):
            optimized_md_path = manual_timeline["optimized_md_path"]
            if report_dir:
                optimized_link = os.path.relpath(
                    optimized_md_path, report_dir
                ).replace(os.sep, "/")
                lines.append(
                    f"> 字幕优化时间轴: [{os.path.basename(optimized_md_path)}]"
                    f"({optimized_link})"
                )
            else:
                lines.append(f"> 字幕优化时间轴: {optimized_md_path}")
    lines.extend(["---", "", "## 逐话题时间轴", ""])

    groups = _group_topics_for_parts(topics)
    if not groups:
        lines.append("本次没有解析到有效话题。")
        lines.append("")
    else:
        topic_index = 1
        if group_by_hour:
            iterable = _group_topics_by_hour(topics)
        else:
            iterable = [(idx - 1, group) for idx, group in enumerate(_group_topics_for_parts(topics), 1)]
        for display_part_index, (part_index, group) in enumerate(iterable, 1):
            part_start = min(t["start"] for t in group)
            part_end = max(t["end"] for t in group)
            if group_by_hour:
                part_title = f"第{part_index + 1}小时重点"
            else:
                part_title = _make_part_title(group, streamer_name=streamer_name)
            lines.append(
                f"Part {display_part_index}: {part_title} "
                f"({topic_formatting.format_report_time(part_start)}－{topic_formatting.format_report_time(part_end)})"
            )
            for topic in group:
                lines.append(topic_formatting.format_topic_block(topic, topic_index, streamer_name=streamer_name))
                topic_index += 1
            lines.append("")

    publish_title_lines = publish_title_report_lines(
        clip_marks,
        source_path=video_name,
    )
    if publish_title_lines:
        lines.extend(publish_title_lines)

    if api_warning:
        lines.append("## 分析警告")
        lines.append("")
        lines.append(f"- {api_warning}")
        lines.append("")

    if failed_chunks:
        lines.append("## LLM 分块失败记录")
        lines.append("")
        for item in failed_chunks:
            lines.append(
                f"- 块 {item.get('index')} [{item.get('time')}] "
                f"连续失败，已跳过：{item.get('error')}"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
