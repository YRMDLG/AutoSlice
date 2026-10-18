"""桌面端投稿项目服务；旧扫描规则只在此处适配。"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from pathlib import Path

from autoslice import runtime_config
from autoslice.subtitle_workflow import scan_submission_pairs

DEFAULT_SUBMISSION_ROOT = runtime_config.PROJECT_DIR / "submissions"
PROJECT_SORT_OPTIONS = (
    ("folder_created_desc", "创建时间：新到旧"),
    ("folder_created_asc", "创建时间：旧到新"),
    ("folder_modified_desc", "文件夹最近修改"),
    ("source_modified_desc", "视频最近修改"),
    ("name_asc", "名称：升序"),
    ("name_desc", "名称：降序"),
)


def configured_submission_root() -> Path:
    """显式环境、本机配置依次覆盖 Desktop 的默认投稿目录。"""

    value = (
        str(os.environ.get("AUTOSLICE_SUBMISSION_DIR", "")).strip()
        or runtime_config.LOCAL_ENVIRONMENT.get("AUTOSLICE_SUBMISSION_DIR", "")
    )
    return Path(value).expanduser() if value else DEFAULT_SUBMISSION_ROOT


@dataclass(frozen=True)
class ProjectVideo:
    """保留旧扫描器识别出的媒体与字幕状态。"""

    name: str
    path: str
    srt_path: str
    corrected_srt_path: str
    has_source_srt: bool
    has_corrected_srt: bool
    subtitle_error: str


@dataclass(frozen=True)
class SubmissionProject:
    """投稿根目录下一层标题文件夹对应的项目。"""

    id: str
    title: str
    directory: str
    videos: tuple[ProjectVideo, ...]
    error: str = ""
    folder_created_at: float = 0.0
    folder_modified_at: float = 0.0
    source_modified_at: float = 0.0

    @property
    def status(self) -> str:
        """给列表使用的简短状态。"""

        if self.error:
            return "目录不可读"
        if not self.videos:
            return "缺少视频"
        if any(video.subtitle_error for video in self.videos):
            return "字幕文件有误"
        if any(not video.has_source_srt for video in self.videos):
            return "缺少字幕"
        return "素材已识别"


@dataclass(frozen=True)
class ProjectSnapshot:
    """两页共同消费的一次完整刷新结果。"""

    root: str
    projects: tuple[SubmissionProject, ...]
    status: str
    message: str = ""


class SubmissionProjectService:
    """将旧视频/SRT 扫描结果按标题文件夹聚合并发布快照。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else configured_submission_root()
        self.snapshot = ProjectSnapshot(str(self.root), (), "未扫描")
        self.sort_mode = "name_asc"

    def set_sort_mode(self, mode: str) -> ProjectSnapshot:
        """重排已有快照；保留项目身份，不重新载入媒体或字幕。"""

        self.sort_mode = mode if isinstance(mode, str) and mode in dict(PROJECT_SORT_OPTIONS) else "folder_created_desc"
        projects = sorted(self.snapshot.projects, key=lambda item: (item.title.casefold(), item.directory.casefold()))
        if self.sort_mode == "name_desc":
            projects.reverse()
        elif self.sort_mode not in ("name_asc", "name_desc"):
            field = self.sort_mode.removesuffix("_desc").removesuffix("_asc") + "_at"
            descending = self.sort_mode != "folder_created_asc"
            projects.sort(key=lambda item: (
                getattr(item, field) <= 0,
                -getattr(item, field) if descending else getattr(item, field),
            ))
        self.snapshot = replace(self.snapshot, projects=tuple(projects))
        return self.snapshot

    def refresh(self) -> ProjectSnapshot:
        """重新读取磁盘；缺失和读取错误转换为可显示状态。"""

        root = self.root
        try:
            if not root.is_dir():
                self.snapshot = ProjectSnapshot(str(root), (), "目录不存在")
                return self.snapshot
            folders = sorted(
                (path for path in root.iterdir() if path.is_dir() and not path.is_symlink()),
                key=lambda path: path.name.casefold(),
            )
        except OSError as exc:
            self.snapshot = ProjectSnapshot(str(root), (), "目录不可读", str(exc))
            return self.snapshot

        projects = []
        for folder in folders:
            folder_created_at = folder_modified_at = 0.0
            try:
                stat = folder.stat()
                folder_created_at = float(getattr(stat, "st_birthtime", stat.st_ctime))
                folder_modified_at = float(stat.st_mtime)
                # os.walk 遇到不可读目录会静默跳过，先显式检查项目入口。
                next(folder.iterdir(), None)
                pairs = scan_submission_pairs(folder)
            except (OSError, ValueError) as exc:
                if not folder.exists():
                    continue
                pairs = []
                error = str(exc)
            else:
                error = ""
            videos = tuple(
                ProjectVideo(
                    name=pair["video_name"],
                    path=pair["video_path"],
                    srt_path=pair["srt_path"],
                    corrected_srt_path=pair["corrected_srt_path"],
                    has_source_srt=pair["has_source_srt"],
                    has_corrected_srt=pair["has_corrected_srt"],
                    subtitle_error=pair["subtitle_error"],
                )
                for pair in pairs
            )
            identifier = hashlib.sha256(str(folder).casefold().encode("utf-8")).hexdigest()[:16]
            projects.append(SubmissionProject(
                identifier, folder.name, str(folder), videos, error,
                folder_created_at, folder_modified_at,
                max((float(pair.get("source_modified_at", 0)) for pair in pairs), default=0.0),
            ))

        status = "已刷新" if projects else "目录为空"
        self.snapshot = ProjectSnapshot(str(root), tuple(projects), status)
        return self.set_sort_mode(self.sort_mode)
