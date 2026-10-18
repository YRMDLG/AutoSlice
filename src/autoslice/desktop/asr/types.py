"""识别模块共用的小数据类型；不依赖 Qt 和任何识别库。"""

from __future__ import annotations

from dataclasses import dataclass


class AsrCancelled(Exception):
    """用户取消；检查点已保留，可以续跑。"""


class AsrError(RuntimeError):
    """识别失败，消息可直接给用户看。"""

    code = "failed"


class AsrOutputExists(AsrError):
    """目标字幕已存在；保留用户文件及可另存的识别结果。"""

    code = "exists"


@dataclass(frozen=True)
class Word:
    """一个带时间的字/词（秒）。text 末尾可以带标点，用来帮分段找句子边界。"""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Piece:
    """引擎对一段音频的一句识别结果；words 为空表示引擎没给字级时间，只有整句起止。"""

    start: float
    end: float
    text: str
    words: tuple[Word, ...] = ()


@dataclass(frozen=True)
class Chunk:
    """切块：core 是这一块负责的时间段，input 比 core 两头各多一点上下文。"""

    index: int
    core_start: float
    core_end: float
    input_start: float
    input_end: float

    @property
    def input_duration(self) -> float:
        return self.input_end - self.input_start
