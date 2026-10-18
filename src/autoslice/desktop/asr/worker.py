"""识别子进程入口：`python -m autoslice.desktop.asr.worker <任务.json>`。不依赖 Qt。

被取消时由界面直接结束进程；检查点已经落盘，下次同一个视频会接着往下识别。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from autoslice.desktop.foundation import DesktopStorage

from .engines import Engine, create_engine
from .jobs import JobSpec, format_event
from .pipeline import TranscribeOptions, transcribe, work_dir_for
from .types import AsrCancelled, AsrError


def run_spec(spec: JobSpec, emit: Callable[[str], None], engine: Engine | None = None) -> int:
    """跑一个任务，把进度和结果当事件交给 emit；返回进程退出码。"""

    settings = spec.asr_settings()
    storage = DesktopStorage(spec.storage_root)
    try:
        engine = engine or create_engine(settings)
        result = transcribe(
            spec.video, spec.output, engine,
            workdir=work_dir_for(storage, Path(spec.video)),
            fingerprint=f"{settings.engine}|{settings.model}|{settings.aligner}|{settings.language}",
            hotwords=spec.hotwords,
            options=TranscribeOptions(foreground_filter=settings.foreground_filter,
                                      overwrite=spec.overwrite),
            progress=lambda message, fraction: emit(format_event("progress", message=message, fraction=round(fraction, 4))),
        )
    except AsrCancelled:
        emit(format_event("cancelled"))
        return 3
    except AsrError as exc:
        emit(format_event("error", code=exc.code, message=str(exc)))
        return 2
    except Exception as exc:  # noqa: BLE001 - 子进程最后一道防线：任何意外都要变成界面看得懂的错误事件
        emit(format_event("error", message=f"{type(exc).__name__}: {str(exc)[:300]}"))
        return 1
    emit(format_event("done", srt_path=result.srt_path, cue_count=result.cue_count,
                      seconds=round(result.seconds, 1), audio_seconds=round(result.audio_seconds, 1),
                      info=result.info, warnings=result.warnings))
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("用法：python -m autoslice.desktop.asr.worker <任务.json>", file=sys.stderr)
        return 64
    return run_spec(JobSpec.read(Path(arguments[0])), lambda line: print(line, flush=True))


if __name__ == "__main__":
    raise SystemExit(main())
