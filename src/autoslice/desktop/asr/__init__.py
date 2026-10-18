"""桌面端字幕识别：多引擎、可续跑、能用你自己的校对稿评测。命令行见 __main__。"""

from .pipeline import TranscribeOptions, TranscribeResult, transcribe
from .settings import AsrSettings, load_settings, save_settings
from .types import AsrCancelled, AsrError, Piece, Word

__all__ = [
    "AsrCancelled", "AsrError", "AsrSettings", "Piece", "TranscribeOptions", "TranscribeResult",
    "Word", "load_settings", "save_settings", "transcribe",
]
