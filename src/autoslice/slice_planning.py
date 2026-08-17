"""切片任务构造与既有产物分类的唯一实现。"""

from __future__ import annotations

import os
from dataclasses import dataclass

from autoslice import reporting as reporting_service
from autoslice import slice_reuse


@dataclass(frozen=True)
class SliceJobPartition:
    """按是否需要重新编码划分的一组切片任务。"""

    reusable_jobs: tuple[dict, ...]
    pending_jobs: tuple[dict, ...]
    title_renamed_count: int

    @property
    def reusable_output_names(self):
        return tuple(str(job["output_name"]) for job in self.reusable_jobs)


def build_slice_jobs(marks, source_path, report_dir, precise_video_end=None):
    """把最终 mark 转换为切片任务，并修正需要保留到关播的真实终点。"""

    jobs = []
    for index, mark in enumerate(marks, 1):
        start_s = float(mark["start"])
        end_s = float(mark["end"])
        if mark.get("preserve_to_video_end") and precise_video_end is not None:
            # 报告中的整秒终点便于阅读；实际任务必须使用 ffprobe 浮点终点，
            # 否则最后一片可能多编码近一秒并触发时长校验失败。
            end_s = min(end_s, float(precise_video_end))
        duration = end_s - start_s
        if duration <= 0:
            continue
        output_name = reporting_service.topic_clip_filename(
            index,
            mark,
            source_path,
        )
        jobs.append({
            "index": index,
            "mark": mark,
            "start": start_s,
            "end": end_s,
            "duration": duration,
            "title": mark.get("title", f"片段{index}"),
            "output_name": output_name,
            "output_path": os.path.join(report_dir, output_name),
        })
    return jobs


def partition_slice_jobs(slice_jobs, source_path, report_dir):
    """按现有产物可复用性划分任务；复用 owner 可更新历史容器路径。"""

    reusable_jobs = []
    pending_jobs = []
    title_renamed_count = 0
    for job in slice_jobs:
        if slice_reuse.is_reusable_topic_clip(
            job["output_path"],
            source_path,
            job["duration"],
        ):
            reusable_jobs.append(job)
        elif slice_reuse.reuse_compatible_topic_clip(job, source_path):
            reusable_jobs.append(job)
        elif slice_reuse.reuse_topic_clip_after_title_change(
            job,
            report_dir,
            source_path,
        ):
            reusable_jobs.append(job)
            title_renamed_count += 1
        else:
            pending_jobs.append(job)
    return SliceJobPartition(
        reusable_jobs=tuple(reusable_jobs),
        pending_jobs=tuple(pending_jobs),
        title_renamed_count=title_renamed_count,
    )
