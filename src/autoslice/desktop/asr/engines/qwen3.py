"""Qwen3-ASR 引擎（官方 qwen-asr 包）。

这份适配没能在写代码的机器上跑通真实模型（没装、也不该在别人电脑上下几个 GB），
所以三处对返回结构做了宽容解析，并在首次运行时用 `python -m autoslice.desktop.asr doctor --probe` 验证：
  1. transcribe 是否支持 context（热词）参数——不支持就不传，并在运行信息里说明；
  2. time_stamps 的条目是对象/字典/元组都能认；
  3. 时间单位秒或毫秒自动判断。
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Sequence

from ..settings import AsrSettings
from ..types import AsrError, Piece, Word
from .runtime import cuda_peak_mb, resolve_device

# 模型卡里热词的写法
_CONTEXT_PREFIX = "Vocabulary: "
_LANG_TAG = re.compile(r"^\s*language\s+\w+\s*<asr_text>", re.IGNORECASE)


def parse_time_stamps(raw, duration: float) -> list[Word]:
    """把对齐结果统一成 Word 列表。条目可以是对象、字典或元组；单位秒/毫秒自动判断。"""

    if raw is None:
        return []
    items = list(raw)
    # 批量结果会多一层：[[item, item, ...]]
    if len(items) == 1 and isinstance(items[0], (list, tuple)) and items[0] and not _is_triplet(items[0]):
        items = list(items[0])
    words: list[Word] = []
    for item in items:
        text = start = end = None
        if isinstance(item, dict):
            text = item.get("text", item.get("token", item.get("word")))
            start = item.get("start_time", item.get("start"))
            end = item.get("end_time", item.get("end"))
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            if isinstance(item[0], str):
                text, start, end = item[0], item[1], item[2]
            else:
                start, end, text = item[0], item[1], item[2]
        else:
            text = getattr(item, "text", None)
            start = getattr(item, "start_time", getattr(item, "start", None))
            end = getattr(item, "end_time", getattr(item, "end", None))
        if text is None or start is None or end is None:
            continue
        text = str(text).strip()
        if text:
            words.append(Word(float(start), float(end), text))
    # 末尾时间远超音频长度：单位是毫秒
    if words and duration > 0 and max(word.end for word in words) > duration * 3 + 5:
        words = [Word(word.start / 1000.0, word.end / 1000.0, word.text) for word in words]
    return words


def _is_triplet(value) -> bool:
    return len(value) == 3 and isinstance(value[0], (str, int, float)) and not isinstance(value[0], (list, tuple))


def clean_text(text: str) -> str:
    return _LANG_TAG.sub("", str(text or "")).strip()


class Qwen3Engine:
    id = "qwen3-asr"
    max_chunk_seconds = 45.0

    def __init__(self, settings: AsrSettings) -> None:
        self.settings = settings
        self.model = None
        self._context = ""
        self._accepts_context = False
        self._timestamps = bool(settings.aligner)
        self._device = "cpu"

    def load(self, progress: Callable[[str], None] | None = None, hotwords: Sequence[str] = ()) -> dict:
        try:
            import torch
            from qwen_asr import Qwen3ASRModel
        except ImportError as exc:
            raise AsrError(f"没有安装 qwen-asr 或 torch（{exc}）。先运行 `python -m autoslice.desktop.asr doctor` 看缺什么。") from exc
        self._device = resolve_device(self.settings.device)
        dtype = torch.bfloat16 if self._device.startswith("cuda") else torch.float32
        if progress:
            progress(f"加载 Qwen3-ASR（{self._device}）…")
        options = {"dtype": dtype, "device_map": self._device, "max_inference_batch_size": 4, "max_new_tokens": 512}
        if self.settings.aligner:
            options["forced_aligner"] = self.settings.aligner
            options["forced_aligner_kwargs"] = {"dtype": dtype, "device_map": self._device}
        try:
            self.model = Qwen3ASRModel.from_pretrained(self.settings.model, **options)
        except Exception as exc:  # noqa: BLE001 - 模型加载的失败类型太杂（网络/显存/文件），统一转成可读提示
            raise AsrError(f"Qwen3-ASR 模型加载失败：{str(exc)[:200]}") from exc
        parameters = inspect.signature(self.model.transcribe).parameters
        self._accepts_context = "context" in parameters or any(
            item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
        )
        self.set_hotwords(hotwords)
        return {
            "引擎": "Qwen3-ASR", "模型": self.settings.model, "设备": self._device,
            "字级时间": "有（对齐模型）" if self._timestamps else "无（只能按字数估算）",
            "热词": ("已启用" if self._context and self._accepts_context
                    else "此版本 qwen-asr 不接受热词参数，未生效" if self._context else "未使用"),
        }

    def set_hotwords(self, hotwords: Sequence[str]) -> None:
        """热词是每次调用时才传的，换一批不用重新加载模型。"""
        self._context = _CONTEXT_PREFIX + ", ".join(hotwords) + "." if hotwords else ""

    def recognize(self, wav_path: str) -> list[Piece]:
        if self.model is None:
            raise AsrError("引擎还没加载")
        options = {"audio": wav_path, "language": self.settings.language or None}
        if self._timestamps:
            options["return_time_stamps"] = True
        if self._context and self._accepts_context:
            options["context"] = self._context
        try:
            results = self.model.transcribe(**options)
        except Exception as exc:  # noqa: BLE001 - 显存不足等运行期错误统一给出可读消息
            raise AsrError(f"Qwen3-ASR 识别失败：{str(exc)[:200]}") from exc
        pieces = []
        from ..audio import wav_duration

        duration = wav_duration(wav_path)
        for result in results if isinstance(results, (list, tuple)) else [results]:
            text = clean_text(getattr(result, "text", ""))
            if not text:
                continue
            words = parse_time_stamps(getattr(result, "time_stamps", None), duration)
            if words:
                pieces.append(Piece(words[0].start, words[-1].end, text, tuple(words)))
            else:
                pieces.append(Piece(0.0, duration, text))
        return pieces

    def stats(self) -> dict:
        return {"peak_vram_mb": cuda_peak_mb(), "device": self._device}

    def close(self) -> None:
        self.model = None
        try:
            import gc

            import torch

            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except (ImportError, OSError, RuntimeError):
            pass
