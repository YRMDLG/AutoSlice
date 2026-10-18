"""识别引擎：统一接口 + 注册表。具体引擎的重依赖（torch、识别库）都在用到时才导入。

加新引擎只要：写一个有 load/recognize/close 的类，在 ENGINES 里登记一行。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ..settings import AsrSettings
from ..types import AsrError, Piece


class Engine(Protocol):
    id: str
    # 一次喂给模型的最长音频（秒）；流水线按它切块
    max_chunk_seconds: float

    def load(self, progress: Callable[[str], None] | None = None, hotwords: Sequence[str] = ()) -> dict:
        """加载模型；返回给人看的运行信息（设备、模型、是否有字级时间等）。"""

    def recognize(self, wav_path: str) -> list[Piece]:
        """识别一段 16k 单声道 wav；时间从这段音频的 0 秒算起。"""

    def close(self) -> None:
        """释放显存。"""


@dataclass(frozen=True)
class EngineSpec:
    id: str
    label: str
    summary: str
    # 需要能导入的 Python 包（自检用）
    packages: tuple[str, ...]
    factory: Callable[[AsrSettings], Engine]


def _qwen3(settings: AsrSettings) -> Engine:
    from .qwen3 import Qwen3Engine

    return Qwen3Engine(settings)


def _funasr(settings: AsrSettings) -> Engine:
    from .funasr_legacy import FunAsrEngine

    return FunAsrEngine(settings)


ENGINES: dict[str, EngineSpec] = {
    "qwen3-asr": EngineSpec(
        "qwen3-asr", "Qwen3-ASR 1.7B（推荐）",
        "开源模型里抗噪最好，支持热词，配合对齐模型给每个字时间；约 5GB 显存，8GB 够用。",
        ("torch", "qwen_asr"), _qwen3,
    ),
    "funasr": EngineSpec(
        "funasr", "FunASR（原来网页版用的）",
        "Paraformer-large + 断句 + 标点，之前在 2060 上用的就是它；作为对照基线。",
        ("torch", "funasr"), _funasr,
    ),
}


def create_engine(settings: AsrSettings) -> Engine:
    spec = ENGINES.get(settings.engine)
    if spec is None:
        raise AsrError(f"不认识的识别引擎：{settings.engine}")
    return spec.factory(settings)
