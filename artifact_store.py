"""AutoSlice 产物目录、原子文件写入与旧产物迁移工具。"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading


ARTIFACT_LAYOUT_VERSION = 1
ARTIFACT_BUNDLE_SUFFIX = "_自动切片"
ARTIFACT_DATA_DIRNAME = "数据"
ARTIFACT_QUEUE_DIRNAME = "_总清单"
UNIFIED_REFINEMENT_QUEUE_JSON = "精调任务总清单.json"
UNIFIED_REFINEMENT_QUEUE_MD = "精调任务总清单.md"


def artifact_bundle_stem(video_path):
    """从录播文件名生成稳定且适合 Windows 目录的整理包名称。"""
    stem = os.path.splitext(os.path.basename(str(video_path or "")))[0]
    stem = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    return stem[:180].rstrip(" .") or "录播"


def artifact_bundle_layout(
    video_path,
    output_dir=None,
    artifact_dir=None,
    *,
    default_output_dir,
):
    """返回单场录播的规范产物路径；本函数只计算路径，不创建文件。"""
    source_video_path = os.path.abspath(str(video_path))
    video_stem = artifact_bundle_stem(source_video_path)
    if artifact_dir:
        artifact_dir = os.path.abspath(artifact_dir)
        output_root = os.path.dirname(artifact_dir)
    else:
        output_root = os.path.abspath(output_dir or default_output_dir)
        artifact_dir = os.path.join(
            output_root,
            video_stem + ARTIFACT_BUNDLE_SUFFIX,
        )
    data_dir = os.path.join(artifact_dir, ARTIFACT_DATA_DIRNAME)
    queue_dir = os.path.join(output_root, ARTIFACT_QUEUE_DIRNAME)
    return {
        "layout_version": ARTIFACT_LAYOUT_VERSION,
        "source_video_path": source_video_path,
        "video_stem": video_stem,
        "output_root": output_root,
        "artifact_dir": artifact_dir,
        "data_dir": data_dir,
        "overview_path": os.path.join(artifact_dir, "00_概览.md"),
        "report_path": os.path.join(artifact_dir, "01_话题分析.md"),
        "task_manifest_md_path": os.path.join(artifact_dir, "02_精调任务.md"),
        "optimized_timeline_md_path": os.path.join(
            artifact_dir,
            "03_优化时间轴.md",
        ),
        "slice_pointer_path": os.path.join(artifact_dir, "切片路径.txt"),
        "clip_marks_path": os.path.join(data_dir, "clip_marks.json"),
        "task_manifest_json_path": os.path.join(data_dir, "精调任务.json"),
        "optimized_timeline_json_path": os.path.join(
            data_dir,
            "优化时间轴.json",
        ),
        "asr_checkpoint_path": os.path.join(data_dir, "asr_checkpoint.json"),
        "topic_analysis_checkpoint_path": os.path.join(
            data_dir,
            "topic_analysis_checkpoint.json",
        ),
        "clip_review_checkpoint_path": os.path.join(
            data_dir,
            "clip_review_checkpoint.json",
        ),
        "candidate_review_audit_path": os.path.join(
            data_dir,
            "候选复核明细.json",
        ),
        "corrected_srt_path": os.path.join(data_dir, "校对字幕.srt"),
        "slice_dir": os.path.join(output_root, video_stem + "_话题切片"),
        "unified_queue_dir": queue_dir,
        "unified_queue_json_path": os.path.join(
            queue_dir,
            UNIFIED_REFINEMENT_QUEUE_JSON,
        ),
        "unified_queue_md_path": os.path.join(
            queue_dir,
            UNIFIED_REFINEMENT_QUEUE_MD,
        ),
    }


def write_artifact_text(path, content):
    """原子写入整理包文本，失败时不破坏上一个完整版本。"""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(str(content))
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
    return path


def write_artifact_json(path, payload):
    return write_artifact_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def copy_artifact_file(source_path, destination_path):
    """把旧产物安全复制到整理包；绝不删除或移动源文件。"""
    if not source_path or not os.path.isfile(source_path):
        return None
    source_path = os.path.abspath(source_path)
    destination_path = os.path.abspath(destination_path)
    if os.path.normcase(source_path) == os.path.normcase(destination_path):
        return destination_path
    os.makedirs(os.path.dirname(destination_path), exist_ok=True)
    temp_path = (
        f"{destination_path}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    try:
        shutil.copy2(source_path, temp_path)
        os.replace(temp_path, destination_path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
    return destination_path


def load_artifact_json(path):
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def first_existing_artifact_path(*paths):
    for path in paths:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return None


def seed_artifact_from_legacy(canonical_path, *legacy_paths):
    """规范产物缺失时复制旧检查点；已有规范文件始终优先。"""
    if canonical_path and os.path.isfile(canonical_path):
        return os.path.abspath(canonical_path)
    source_path = first_existing_artifact_path(*legacy_paths)
    return (
        copy_artifact_file(source_path, canonical_path)
        if source_path
        else None
    )


def markdown_relative_artifact_link(target_path, base_dir, label=None):
    """生成整理包 Markdown 使用的相对链接，避免重复显示本机绝对路径。"""
    relative_path = os.path.relpath(target_path, base_dir).replace(os.sep, "/")
    if not relative_path.startswith("."):
        relative_path = "./" + relative_path
    return f"[{label or os.path.basename(target_path)}]({relative_path})"


def rewrite_organized_report_links(layout):
    """只更新旧报告头部的产物入口，保留完整话题正文不变。"""
    report_path = layout["report_path"]
    if not os.path.isfile(report_path):
        return None
    targets = {
        "> 剪映校对字幕:": layout["corrected_srt_path"],
        "> 精调总清单:": layout["unified_queue_md_path"],
        "> 字幕优化时间轴:": layout["optimized_timeline_md_path"],
    }
    with open(report_path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    rewritten = []
    for line in lines:
        replacement = None
        for prefix, target_path in targets.items():
            if line.startswith(prefix) and os.path.isfile(target_path):
                replacement = (
                    f"{prefix} "
                    f"{markdown_relative_artifact_link(target_path, layout['artifact_dir'])}"
                )
                break
        rewritten.append(replacement if replacement is not None else line)
    write_artifact_text(
        report_path,
        "\n".join(rewritten).rstrip() + "\n",
    )
    return report_path
