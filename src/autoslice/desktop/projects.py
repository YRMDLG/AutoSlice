"""桌面端投稿项目服务；旧扫描规则只在此处适配。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from autoslice import runtime_config
from autoslice.subtitle_workflow import scan_submission_pairs


def configured_submission_root() -> Path:
    """与 Web 端同一配置 owner：显式环境、本机配置，最后是仓库内 submissions。"""

    return runtime_config.configured_path("AUTOSLICE_SUBMISSION_DIR", "submissions")


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
            try:
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
            projects.append(SubmissionProject(identifier, folder.name, str(folder), videos, error))

        status = "已刷新" if projects else "目录为空"
        self.snapshot = ProjectSnapshot(str(root), tuple(projects), status)
        return self.snapshot
