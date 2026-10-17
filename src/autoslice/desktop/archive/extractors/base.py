"""抽取器的约定：每个抽取器声明自己的表，给一个文件夹，返回要写进各表的行。

加新的抽取器（比如让大模型给旧封面打标签、从标题里挖系列）只需要：
1. 继承 Extractor，写 schema（建表语句）、tables（表名 → 列名，不含 entry_id）和 extract；
2. 在 extractors/__init__.py 的 DEFAULT_EXTRACTORS 里登记；
3. 抽取结果的含义变了，就把 model.EXTRACT_VERSION 加一，旧条目下次扫描会自动重抽。
抽取器不能修改档案里的任何文件，也不要联网；单个文件夹出错由扫描器兜住，不会中断整次扫描。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ..model import ArchiveEntry, ScanOptions


@dataclass(slots=True)
class ExtractContext:
    options: ScanOptions
    streamer: str = ""
    # 抽取器之间共享的缓存（比如同一份字幕只读一次）。
    cache: dict = field(default_factory=dict)


class Extractor:
    name: str = ""
    schema: str = ""
    tables: Mapping[str, Sequence[str]] = {}

    def enabled(self, options: ScanOptions) -> bool:
        return True

    def extract(self, entry: ArchiveEntry, context: ExtractContext) -> Mapping[str, Sequence[Sequence]]:
        """返回 {表名: [行, ...]}；没有可抽的内容返回空表。"""

        raise NotImplementedError
