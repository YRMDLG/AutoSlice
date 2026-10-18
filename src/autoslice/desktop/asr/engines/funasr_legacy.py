"""原网页版用的 FunASR（Paraformer-large + 断句 + 标点），包一层统一接口，作为对照基线。

复用 autoslice.transcription 里已验证的会话、结果规范化和字级对齐，不重写。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from autoslice.transcription import background_filter, model_runtime, recognition, results, segments

from ..settings import AsrSettings
from ..types import AsrError, Piece, Word
from .runtime import cuda_peak_mb, resolve_device


class FunAsrEngine:
    id = "funasr"
    # 模型自带 VAD，可以一次吃长一点
    max_chunk_seconds = 120.0

    def __init__(self, settings: AsrSettings) -> None:
        self.settings = settings
        self.session = None

    def load(self, progress: Callable[[str], None] | None = None, hotwords: Sequence[str] = ()) -> dict:
        def relay(message, *_):
            if progress:
                progress(str(message))

        try:
            from funasr import AutoModel
        except ImportError as exc:
            raise AsrError("没有安装 funasr，先运行 `python -m autoslice.desktop.asr doctor` 看缺什么。") from exc
        try:
            device = resolve_device(self.settings.device)
            if self.settings.device.lower() != "auto" and device.startswith("cuda"):
                import torch

                if not torch.cuda.is_available():
                    raise AsrError("选了显卡，但这台机器没有可用的 CUDA")
            self.session = recognition.FunASRRecognitionSession.load(
                AutoModel,
                hotwords=" ".join(hotwords)[:model_runtime.FUNASR_HOTWORD_MAX_CHARS],
                background_filter_mode=background_filter.BACKGROUND_FILTER_OFF,
                progress_callback=relay,
                device=device,
            )
        except Exception as exc:  # noqa: BLE001 - 模型缺失/依赖缺失/显存，统一转成可读提示
            raise AsrError(f"FunASR 加载失败：{str(exc)[:200]}") from exc
        return {"引擎": "FunASR Paraformer", "设备": self.session.device,
                "字级时间": "有", "热词": "已传入（普通 Paraformer 会忽略）" if hotwords else "未使用"}

    def recognize(self, wav_path: str) -> list[Piece]:
        if self.session is None:
            raise AsrError("引擎还没加载")
        try:
            raw = self.session.generate(wav_path, chunk_index=0, chunk_count=1)
        except Exception as exc:  # noqa: BLE001
            raise AsrError(f"FunASR 识别失败：{str(exc)[:200]}") from exc
        pieces = []
        for item in results.normalise_funasr_result(raw):
            text, stamps = str(item.get("text", "")).strip(), item.get("timestamp") or []
            if not text or not stamps:
                continue
            tokens, aligned = segments.align_funasr_tokens(text, stamps, raw_text=item.get("raw_text"))
            clean = [pair for pair in stamps if isinstance(pair, (list, tuple)) and len(pair) == 2]
            if aligned and len(tokens) == len(clean):
                words = tuple(Word(a / 1000.0, b / 1000.0, token) for token, (a, b) in zip(tokens, clean))
                pieces.append(Piece(words[0].start, words[-1].end, text, words))
            elif clean:
                pieces.append(Piece(clean[0][0] / 1000.0, clean[-1][1] / 1000.0, text))
        return pieces

    def stats(self) -> dict:
        return {"peak_vram_mb": cuda_peak_mb(), "device": getattr(self.session, "device", "")}

    def close(self) -> None:
        self.session = None
        model_runtime.clear_funasr_cuda_cache()
