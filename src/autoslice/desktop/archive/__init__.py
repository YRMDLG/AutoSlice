"""旧切片档案学习：扫描本机历史投稿文件夹，学你的校对习惯、术语、标题口吻、封面风格。

入口：ArchiveService（界面和命令行共用）；命令行见 ``python -m autoslice.desktop.archive --help``；
设计、数据布局假设和给 GPT 的待办见 docs/desktop-vnext/ARCHIVE_LEARNING.md。
"""

from .knowledge import Knowledge
from .layout import StreamerMatcher, classify_folder
from .model import ArchiveEntry, ArchiveVideo, ScanOptions, ScanProgress, ScanResult
from .proposals import Proposal
from .service import ArchiveService

__all__ = [
    "ArchiveEntry", "ArchiveService", "ArchiveVideo", "Knowledge", "Proposal", "ScanOptions", "ScanProgress",
    "ScanResult", "StreamerMatcher", "classify_folder",
]
