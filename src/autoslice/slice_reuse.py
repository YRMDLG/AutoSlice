"""自动切片产物清理与历史成片复用的唯一实现。"""

from __future__ import annotations

import os
import re
import shutil

from autoslice import media_probe
from autoslice.media_formats import (
    SUPPORTED_VIDEO_EXTENSIONS,
    compatible_output_extensions,
    normalise_video_extension,
)

FACADE_EXPORTS = {
    "_GENERATED_VIDEO_SUFFIX_PATTERN": "_GENERATED_VIDEO_SUFFIX_PATTERN",
    "_GENERATED_TOPIC_TEMP_RE": "_GENERATED_TOPIC_TEMP_RE",
    "SLICE_DURATION_TOLERANCE_SEC": "SLICE_DURATION_TOLERANCE_SEC",
    "_GENERATED_TOPIC_ARTIFACT_RE": "_GENERATED_TOPIC_ARTIFACT_RE",
    "_cleanup_stale_topic_clips": "cleanup_stale_topic_clips",
    "_is_reusable_topic_clip": "is_reusable_topic_clip",
    "_reuse_compatible_topic_clip": "reuse_compatible_topic_clip",
    "_reuse_topic_clip_after_title_change": "reuse_topic_clip_after_title_change",
}


SLICE_DURATION_TOLERANCE_SEC = 0.5


_GENERATED_VIDEO_SUFFIX_PATTERN = "|".join(
    re.escape(extension.removeprefix("."))
    for extension in SUPPORTED_VIDEO_EXTENSIONS
)


_GENERATED_TOPIC_ARTIFACT_RE = re.compile(
    # 这里只匹配自动生成的原始切片视频。片段目录中的 SRT、ASS、校对
    # 字幕和字幕版视频都可能已经进入用户的后续制作流程，不能在重复
    # 切片时被当作失效自动产物删除。
    rf"^\d{{2,3}}_\d+s_.+(?<!_字幕版)\.(?:{_GENERATED_VIDEO_SUFFIX_PATTERN})$",
    re.IGNORECASE,
)


_GENERATED_TOPIC_TEMP_RE = re.compile(
    rf"^(?:\d{{2,3}}_\d+s_.+\.(?:{_GENERATED_VIDEO_SUFFIX_PATTERN})"
    rf"\.part\.(?:{_GENERATED_VIDEO_SUFFIX_PATTERN})|"
    r"\.autoslice_seek_index_\d+\.mkv)$",
    re.IGNORECASE,
)


probe_video_duration = media_probe.probe_video_duration


def cleanup_stale_topic_clips(report_dir, preserve_names=None):
    """清理失效自动产物；可保留已通过校验的现有切片视频。"""

    if not os.path.isdir(report_dir):
        return 0
    preserved = {
        str(name).casefold()
        for name in (preserve_names or [])
        if str(name).strip()
    }
    removed = 0
    for name in os.listdir(report_dir):
        if not (
            _GENERATED_TOPIC_ARTIFACT_RE.fullmatch(name)
            or _GENERATED_TOPIC_TEMP_RE.fullmatch(name)
        ):
            continue
        if (
            _GENERATED_TOPIC_ARTIFACT_RE.fullmatch(name)
            and name.casefold() in preserved
        ):
            continue
        path = os.path.join(report_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
        except OSError:
            continue
        removed += 1
    return removed


def is_reusable_topic_clip(output_path, source_path, expected_duration):
    """校验已有切片是否仍对应当前源录播和当前计划时长。"""

    force_rebuild = os.environ.get("AUTOSLICE_FORCE_RESLICE", "").strip().lower()
    if force_rebuild in {"1", "true", "yes", "on"}:
        return False
    try:
        output_stat = os.stat(output_path)
        source_stat = os.stat(source_path)
    except OSError:
        return False
    if output_stat.st_size <= 0 or output_stat.st_mtime_ns < source_stat.st_mtime_ns:
        return False
    actual_duration = probe_video_duration(output_path)
    return (
        actual_duration is not None
        and abs(float(actual_duration) - float(expected_duration))
        <= SLICE_DURATION_TOLERANCE_SEC
    )


def reuse_compatible_topic_clip(job, source_path):
    """首选产物不存在时，原路径复用兼容的历史容器产物。"""

    preferred_path = os.path.abspath(job["output_path"])
    output_stem = os.path.splitext(preferred_path)[0]
    for extension in compatible_output_extensions(source_path):
        candidate_path = output_stem + extension
        if os.path.normcase(candidate_path) == os.path.normcase(preferred_path):
            continue
        if not is_reusable_topic_clip(
            candidate_path,
            source_path,
            job["duration"],
        ):
            continue
        job["output_path"] = candidate_path
        job["output_name"] = os.path.basename(candidate_path)
        return True
    return False


def reuse_topic_clip_after_title_change(job, report_dir, source_path):
    """起点和时长未变时复用旧视频，允许标题或候选编号发生变化。"""

    expected_name = str(job["output_name"])
    start_marker = f'_{int(job["start"])}s_'.casefold()
    try:
        names = os.listdir(report_dir)
    except OSError:
        return False
    candidates = []
    compatible_extensions = set(compatible_output_extensions(source_path))
    for name in names:
        if name.casefold() == expected_name.casefold():
            continue
        if not _GENERATED_TOPIC_ARTIFACT_RE.fullmatch(name):
            continue
        if (
            start_marker not in name.casefold()
            or normalise_video_extension(name) not in compatible_extensions
        ):
            continue
        path = os.path.join(report_dir, name)
        try:
            modified_ns = os.stat(path).st_mtime_ns
        except OSError:
            continue
        candidates.append((modified_ns, path))
    for _modified_ns, candidate_path in sorted(candidates, reverse=True):
        if not is_reusable_topic_clip(
            candidate_path,
            source_path,
            job["duration"],
        ):
            continue
        target_extension = normalise_video_extension(candidate_path)
        target_path = os.path.splitext(job["output_path"])[0] + target_extension
        try:
            os.replace(candidate_path, target_path)
        except OSError:
            try:
                shutil.copy2(candidate_path, target_path)
            except OSError:
                try:
                    os.remove(target_path)
                except OSError:
                    pass
                continue
        job["output_path"] = target_path
        job["output_name"] = os.path.basename(target_path)
        return True
    return False
