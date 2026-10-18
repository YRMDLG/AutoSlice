"""识别引擎对比：拿你自己校对过的旧字幕当标准答案，量每个引擎、有无热词各错多少字。

字错率（CER）= 编辑距离 / 标准答案字数，直接等于“你拿到初稿后要手改的字数占比”，
比公开榜单上的数字更能说明哪个引擎适合你的直播（口癖、背景音乐、专有名词都在里面）。
只读你的旧文件夹；音频片段和中间结果放在临时目录，跑完即删。
"""

from __future__ import annotations

import difflib
import os
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from autoslice.desktop.archive.layout import classify_folder
from autoslice.desktop.archive.model import SKIP_DIRS
from autoslice.desktop.archive.srt import Cue, load_cues
from autoslice.desktop.foundation import DesktopStorage

from . import audio
from .engines import Engine, create_engine
from .hotwords import build_hotwords
from .pipeline import TranscribeOptions, transcribe
from .settings import AsrSettings
from .text import cer, edit_distance, normalise_for_cer
from .types import AsrCancelled


@dataclass(frozen=True)
class BenchCase:
    name: str
    video: str
    reference_srt: str
    start: float
    end: float


@dataclass(frozen=True)
class Variant:
    label: str
    settings: AsrSettings
    hotwords: bool


@dataclass
class CaseScore:
    case: str
    errors: int
    reference_chars: int
    seconds: float
    audio_seconds: float
    terms_total: int = 0
    terms_hit: int = 0
    examples: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class VariantScore:
    label: str
    cases: list[CaseScore] = field(default_factory=list)
    peak_vram_mb: float = 0.0
    error: str = ""

    @property
    def errors(self) -> int:
        return sum(item.errors for item in self.cases)

    @property
    def reference_chars(self) -> int:
        return sum(item.reference_chars for item in self.cases)

    @property
    def cer(self) -> float:
        return self.errors / self.reference_chars if self.reference_chars else 0.0

    @property
    def rtf(self) -> float:
        """处理时间 / 音频时间，小于 1 表示比实时快。"""
        seconds = sum(item.audio_seconds for item in self.cases)
        return sum(item.seconds for item in self.cases) / seconds if seconds else 0.0

    @property
    def term_recall(self) -> float | None:
        total = sum(item.terms_total for item in self.cases)
        return sum(item.terms_hit for item in self.cases) / total if total else None


# ── 选题：从旧文件夹里挑“有视频 + 有校对稿”的片段 ──

def discover_cases(roots: Sequence[str | Path], *, window_seconds: float = 180.0, windows_per_video: int = 2,
                   max_videos: int = 6) -> list[BenchCase]:
    cases: list[BenchCase] = []
    videos = 0
    for root in roots:
        for directory, subdirs, names in os.walk(root):
            subdirs[:] = sorted(item for item in subdirs if item.casefold() not in SKIP_DIRS)
            files = []
            for name in names:
                try:
                    stat = os.stat(os.path.join(directory, name))
                except OSError:
                    continue
                files.append((name, stat.st_size, stat.st_mtime_ns))
            entry = classify_folder(directory.replace("\\", "/"), os.path.basename(directory), files)
            if entry is None:
                continue
            for video in entry.videos:
                if not (video.path and video.corrected) or not os.path.isfile(video.path):
                    continue
                cues = load_cues(video.corrected)
                for start, end in pick_windows(cues, window_seconds, windows_per_video):
                    cases.append(BenchCase(f"{entry.title}#{int(start)}s", video.path, video.corrected, start, end))
                videos += 1
                if videos >= max_videos:
                    return cases
    return cases


def pick_windows(cues: Sequence[Cue], window_seconds: float, count: int) -> list[tuple[float, float]]:
    """沿字幕时间线均匀挑 count 段、互不重叠的窗口，起点对齐某条字幕的开头。"""

    if not cues or count <= 0:
        return []
    ordered = sorted(cues, key=lambda cue: cue.start)
    total = ordered[-1].end - ordered[0].start
    if total <= window_seconds * 1.2:
        return [(ordered[0].start, ordered[-1].end)]
    windows: list[tuple[float, float]] = []
    for slot in range(count):
        target = ordered[0].start + total * (slot + 0.5) / count - window_seconds / 2
        start = min((cue.start for cue in ordered if cue.start >= target), default=ordered[-1].start)
        if windows and start < windows[-1][1]:
            start = windows[-1][1]
        if start + window_seconds * 0.5 > ordered[-1].end:
            break
        windows.append((start, min(start + window_seconds, ordered[-1].end)))
    return windows


def reference_text(cues: Sequence[Cue], start: float, end: float) -> str:
    return "".join(cue.text for cue in cues if start <= (cue.start + cue.end) / 2 < end)


def diff_examples(reference: str, hypothesis: str, limit: int = 8) -> list[tuple[str, str]]:
    """最能说明问题的几处不同：（标准答案里的写法，引擎写成了什么），只取短的替换/漏字。"""

    ref, hyp = normalise_for_cer(reference), normalise_for_cer(hypothesis)
    pairs = []
    for tag, a0, a1, b0, b1 in difflib.SequenceMatcher(None, ref, hyp, autojunk=False).get_opcodes():
        if tag == "equal" or (a1 - a0) > 8 or (b1 - b0) > 8:
            continue
        # 带上前后各一个字，看得出是哪句话里的错
        pairs.append((ref[max(0, a0 - 1):a1 + 1], hyp[max(0, b0 - 1):b1 + 1]))
    return pairs[:limit]


# ── 跑分 ──

def score_window(engine: Engine, case: BenchCase, reference_cues: Sequence[Cue], terms: Sequence[str],
                 hotwords: Sequence[str]) -> CaseScore:
    reference = reference_text(reference_cues, case.start, case.end)
    with tempfile.TemporaryDirectory(prefix="autoslice-bench-") as directory:
        work = Path(directory)
        wav = work / "window.wav"
        audio.extract_wav(case.video, wav, start=case.start, duration=case.end - case.start)
        output = work / "window.srt"
        started = time.monotonic()
        result = transcribe(wav, output, engine, workdir=work / "w", hotwords=hotwords,
                            options=TranscribeOptions(overwrite=True), manage_engine=False)
        elapsed = time.monotonic() - started
        hypothesis = "".join(cue.text for cue in load_cues(output))
    errors, size = cer(reference, hypothesis)
    ref_norm, hyp_norm = normalise_for_cer(reference), normalise_for_cer(hypothesis)
    present = [term for term in terms if normalise_for_cer(term) and normalise_for_cer(term) in ref_norm]
    hit = sum(1 for term in present if normalise_for_cer(term) in hyp_norm)
    return CaseScore(case.name, errors, size, elapsed, result.audio_seconds, len(present), hit,
                     diff_examples(reference, hypothesis))


def run_bench(cases: Sequence[BenchCase], variants: Sequence[Variant], *, storage: DesktopStorage | None = None,
              progress: Callable[[str], None] | None = None, cancelled: Callable[[], bool] | None = None,
              engine_factory: Callable[[AsrSettings], Engine] = create_engine) -> list[VariantScore]:
    say = progress or (lambda _message: None)
    cue_cache: dict[str, list[Cue]] = {}
    results: list[VariantScore] = []
    for variant in variants:
        score = VariantScore(variant.label)
        results.append(score)
        engine = engine_factory(variant.settings)
        try:
            say(f"[{variant.label}] 加载模型…")
            terms_by_video: dict[str, list[str]] = {}
            current: tuple[str, ...] | None = None
            for index, case in enumerate(cases, 1):
                if cancelled and cancelled():
                    raise AsrCancelled()
                say(f"[{variant.label}] {index}/{len(cases)} {case.name}")
                cues = cue_cache.setdefault(case.reference_srt, load_cues(case.reference_srt))
                terms = terms_by_video.setdefault(
                    case.video, build_hotwords(case.video, storage=storage, title=Path(case.video).parent.name))
                wanted = tuple(terms) if variant.hotwords else ()
                if current is None:
                    engine.load(None, wanted)
                elif wanted != current:
                    # Qwen3 的热词是每次调用时传的，能直接换；其他引擎只能重新加载
                    setter = getattr(engine, "set_hotwords", None)
                    if setter:
                        setter(wanted)
                    else:
                        engine.close()
                        engine.load(None, wanted)
                current = wanted
                score.cases.append(score_window(engine, case, cues, terms, wanted))
            stats = getattr(engine, "stats", lambda: {})()
            score.peak_vram_mb = float(stats.get("peak_vram_mb", 0.0))
        except AsrCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - 单个方案失败（没装/显存不足）不能拖垮整个对比
            score.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            try:
                engine.close()
            except Exception:  # noqa: BLE001
                pass
    return results


def render_report(scores: Sequence[VariantScore], cases: Sequence[BenchCase]) -> str:
    lines = ["# 识别引擎对比", "",
             f"用 {len(cases)} 个片段、你校对后的字幕当标准答案。**字错率越低越好，等于你拿到初稿后要手改的字数占比。**", "",
             "| 方案 | 字错率 | 错字/总字 | 热词命中 | 速度（倍速） | 显存峰值 |", "|---|---|---|---|---|---|"]
    ranked = sorted(scores, key=lambda item: (bool(item.error), item.cer))
    for item in ranked:
        if item.error:
            lines.append(f"| {item.label} | 失败 | — | — | — | {item.error} |")
            continue
        recall = item.term_recall
        speed = f"{1 / item.rtf:.1f}×" if item.rtf else "—"
        vram = f"{item.peak_vram_mb / 1024:.1f} GB" if item.peak_vram_mb else "—"
        hit = "—" if recall is None else f"{recall * 100:.0f}%"
        lines.append(f"| {item.label} | **{item.cer * 100:.2f}%** | {item.errors}/{item.reference_chars} | {hit} | {speed} | {vram} |")
    lines += ["", "## 每个片段", ""]
    for item in ranked:
        if item.error:
            continue
        lines += [f"### {item.label}", "", "| 片段 | 字错率 | 错字/总字 |", "|---|---|---|"]
        lines += [f"| {case.case} | {(case.errors / case.reference_chars * 100 if case.reference_chars else 0):.2f}% | "
                  f"{case.errors}/{case.reference_chars} |" for case in item.cases]
        examples = [pair for case in item.cases for pair in case.examples][:12]
        if examples:
            lines += ["", "典型错处（标准答案 → 引擎写成）：" + "；".join(f"{a}→{b}" for a, b in examples)]
        lines.append("")
    return "\n".join(lines)


def to_json(scores: Sequence[VariantScore]) -> list[dict]:
    return [{"label": item.label, "cer": item.cer, "errors": item.errors, "reference_chars": item.reference_chars,
             "rtf": item.rtf, "term_recall": item.term_recall, "peak_vram_mb": item.peak_vram_mb,
             "error": item.error, "cases": [asdict(case) for case in item.cases]} for item in scores]


__all__ = ["BenchCase", "Variant", "discover_cases", "edit_distance", "pick_windows", "render_report", "run_bench"]
