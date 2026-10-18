"""脚本化的假引擎：测试和界面演示用，不依赖任何模型。"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ..types import AsrError, Piece


class ScriptedEngine:
    """按调用顺序返回预先给好的结果；脚本用完后返回空。raise_on 指定第几次调用失败。"""

    id = "scripted"

    def __init__(self, script: Sequence[Sequence[Piece]], *, max_chunk_seconds: float = 45.0,
                 raise_on: int | None = None) -> None:
        self.script = [list(item) for item in script]
        self.max_chunk_seconds = max_chunk_seconds
        self.raise_on = raise_on
        self.calls: list[str] = []
        self.hotwords: tuple[str, ...] = ()
        self.closed = False

    def load(self, progress: Callable[[str], None] | None = None, hotwords: Sequence[str] = ()) -> dict:
        self.hotwords = tuple(hotwords)
        return {"引擎": "脚本"}

    def recognize(self, wav_path: str) -> list[Piece]:
        call = len(self.calls)
        self.calls.append(wav_path)
        if self.raise_on == call:
            raise AsrError("脚本引擎故意失败")
        return list(self.script[call]) if call < len(self.script) else []

    def stats(self) -> dict:
        return {"peak_vram_mb": 0.0, "device": "cpu"}

    def close(self) -> None:
        self.closed = True
