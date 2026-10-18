"""旧切片档案学习：数据模型与常量。

档案 = 用户本机保留的历史投稿文件夹（成片、字幕初稿、校对字幕、封面、标题……）。
这里只描述“识别出了什么”，不读文件内容、不碰网络，也绝不修改档案里的任何文件。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 数据库结构版本；改表结构时加一并写迁移。
SCHEMA_VERSION = 2
# 抽取逻辑版本；任何抽取器的输出含义变了就加一，旧条目下次扫描时自动重新抽取。
EXTRACT_VERSION = 2

VIDEO_SUFFIXES = frozenset({".mp4", ".mkv", ".mov", ".flv", ".avi", ".webm", ".ts", ".m4v", ".wmv"})
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp"})
NOTE_SUFFIXES = frozenset({".txt", ".md"})
SRT_SUFFIX = ".srt"

# 工作台自己生成的文件名后缀：不当成原始素材（和 subtitle_workflow 的约定一致，另加几种常见写法）。
GENERATED_MARKERS = ("_排版", "_校对", "_校对字幕", "_字幕版", "_字幕预览", "_reflow", "_asr_checkpoint")
CORRECTED_MARKERS = ("_校对", "_校对字幕")
BURNED_MARKERS = ("_字幕版",)
# 文件名里带这些词的图片优先当封面。
COVER_NAME_HINTS = ("封面", "cover", "autocover", "thumb", "缩略图")
# 不进入的目录名（回收站、系统目录、版本库、缓存）。
SKIP_DIRS = frozenset({
    "$recycle.bin", "system volume information", ".git", ".svn", "node_modules", "__pycache__", ".idea", ".vscode",
})
MAX_DEPTH = 10


@dataclass(frozen=True, slots=True)
class ArchiveVideo:
    """一个成片及与它配套的字幕文件（路径为空表示没有）。"""

    name: str
    path: str
    size: int
    mtime_ns: int
    srt: str = ""
    corrected: str = ""
    burned: str = ""


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    """一个投稿文件夹：标题取文件夹名，按文件名约定把成片、字幕、封面配起来。"""

    folder: str
    title: str
    videos: tuple[ArchiveVideo, ...] = ()
    covers: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    # 最可信的日期（YYYY-MM-DD），猜不出来为空。
    date: str = ""
    # 相关文件的名字、大小、修改时间的指纹：没变就不用重新抽取。
    signature: str = ""

    @property
    def has_pairs(self) -> bool:
        """有没有“初稿 + 校对稿”这样能学到改动的成对字幕。"""

        return any(video.srt and video.corrected for video in self.videos)


@dataclass(slots=True)
class ScanOptions:
    """扫描怎么跑：默认安全、轻量、可中断续跑。"""

    # 分析旧封面（要打开图片，较慢）；每个文件夹最多分析几张。
    covers: bool = True
    cover_limit: int = 1
    # 每个文件夹最多记几个候选术语。
    term_cap: int = 80
    # 只扫前 N 个文件夹（试跑用）；None 为不限。
    limit: int | None = None
    # 重新抽取所有条目（忽略指纹）。
    force: bool = False
    # 扫完整个根目录后，把已经不存在的文件夹从库里清掉。
    cleanup_missing: bool = True
    # 进度回调至少隔这么多秒才发一次（界面不需要比这更勤）。
    progress_interval: float = 0.25


@dataclass(slots=True)
class ScanProgress:
    """给界面和命令行看的进度。"""

    root: str = ""
    folders: int = 0
    entries: int = 0
    changed: int = 0
    skipped: int = 0
    errors: int = 0
    current: str = ""
    finished: bool = False
    cancelled: bool = False


@dataclass(slots=True)
class ScanResult:
    entries: int = 0
    changed: int = 0
    skipped: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    seconds: float = 0.0
