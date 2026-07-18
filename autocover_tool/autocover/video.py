"""视频候选帧提取、质量评分与缓存。"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import shutil
import statistics
import subprocess
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat


CACHE_VERSION = 3
LOW_SUBTITLE_RISK = 0.05
SUBTITLE_NEIGHBOR_OFFSETS = (-1.0, 1.0, -0.5, 0.5)
FFPROBE_TIMEOUT_SECONDS = 30
FFMPEG_FRAME_TIMEOUT_SECONDS = 90
_CACHE_LOCKS: dict[str, threading.Lock] = {}
_CACHE_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    """封面选帧所需的视频基础信息。"""

    path: str
    duration: float
    width: int
    height: int
    fps: float

    def to_dict(self) -> dict[str, float | int | str]:
        """返回可序列化的视频信息。"""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class FrameMetrics:
    """归一化到 0-1 的候选帧质量指标。"""

    brightness: float
    exposure: float
    contrast: float
    sharpness: float
    saturation: float
    subtitle_risk: float

    def to_dict(self) -> dict[str, float]:
        """返回可序列化的指标。"""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class FrameCandidate:
    """一张已评分的候选帧。"""

    path: str
    timestamp: float
    score: float
    metrics: FrameMetrics
    cached: bool = False

    def to_dict(self) -> dict[str, bool | float | str | dict[str, float]]:
        """返回适合 API 输出的候选帧数据。"""

        payload = asdict(self)
        payload["metrics"] = self.metrics.to_dict()
        return payload


def find_media_binary(name: str, explicit_path: str | Path | None = None) -> str:
    """查找 ffmpeg 或 ffprobe，找不到时给出中文错误。"""

    if explicit_path is not None:
        path = Path(explicit_path).expanduser()
        if path.is_file():
            return str(path.resolve())
        raise FileNotFoundError(f"找不到 {name}：{path}")

    detected = shutil.which(name)
    if detected:
        return detected
    raise FileNotFoundError(f"找不到 {name}，请先安装并加入 PATH")


def _parse_frame_rate(value: str | None) -> float:
    if not value or value == "0/0":
        return 0.0
    numerator, separator, denominator = value.partition("/")
    try:
        if separator:
            return float(numerator) / float(denominator)
        return float(numerator)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def probe_video(
    video_path: str | Path,
    *,
    ffprobe_path: str | Path | None = None,
) -> VideoMetadata:
    """使用 ffprobe 读取首个视频流的时长、尺寸和帧率。"""

    source = Path(video_path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"视频文件不存在：{source}")

    ffprobe = find_media_binary("ffprobe", ffprobe_path)
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,duration:format=duration",
        "-of",
        "json",
        str(source),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"ffprobe 读取超时（{FFPROBE_TIMEOUT_SECONDS} 秒），请检查视频文件"
        ) from exc
    if result.returncode != 0:
        message = _short_process_error(result.stderr)
        raise RuntimeError(f"ffprobe 读取失败：{message}")

    try:
        payload = json.loads(result.stdout)
        stream = payload["streams"][0]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"视频中没有可用的视频流：{source}") from exc

    raw_duration = stream.get("duration") or payload.get("format", {}).get("duration")
    try:
        duration = float(raw_duration)
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"视频信息不完整：{source}") from exc
    if duration <= 0 or width <= 0 or height <= 0:
        raise ValueError(f"视频时长或尺寸无效：{source}")

    return VideoMetadata(
        path=str(source.resolve()),
        duration=duration,
        width=width,
        height=height,
        fps=_parse_frame_rate(stream.get("avg_frame_rate")),
    )


def plan_candidate_timestamps(
    duration: float,
    count: int = 12,
    *,
    intro_seconds: float = 4.0,
    outro_seconds: float = 3.0,
) -> list[float]:
    """避开成品片头片尾，在主体内容区间内均匀规划时间点。"""

    if duration <= 0:
        raise ValueError("视频时长必须为正数")
    if count <= 0:
        raise ValueError("候选帧数量必须为正数")
    if intro_seconds < 0 or outro_seconds < 0:
        raise ValueError("片头片尾安全时长不能为负数")

    intro_margin = min(max(duration * 0.06, intro_seconds), duration * 0.22)
    outro_margin = min(max(duration * 0.06, outro_seconds), duration * 0.22)
    usable_duration = duration - intro_margin - outro_margin
    if usable_duration <= 0:
        intro_margin = duration * 0.20
        outro_margin = duration * 0.20
        usable_duration = duration * 0.60
    start = intro_margin
    step = usable_duration / count
    return [round(start + step * (index + 0.5), 3) for index in range(count)]


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _subtitle_risk(image: Image.Image) -> float:
    """检测中部和底部窄横向文字带，估算烧录字幕风险。"""

    rgb = image.convert("RGB")
    preview = rgb.copy()
    preview.thumbnail((640, 480), Image.Resampling.LANCZOS)
    gray_edges = preview.convert("L").filter(ImageFilter.FIND_EDGES)
    left = int(gray_edges.width * 0.16)
    right = int(gray_edges.width * 0.84)
    top = int(gray_edges.height * 0.28)
    bottom = int(gray_edges.height * 0.88)
    zone = gray_edges.crop((left, top, right, bottom))
    pixels = zone.load()
    if pixels is None or zone.width <= 0 or zone.height <= 0:
        return 0.0

    row_densities = []
    for y in range(zone.height):
        strong_edges = sum(1 for x in range(zone.width) if pixels[x, y] > 55)
        row_densities.append(strong_edges / zone.width)

    window_size = max(4, int(zone.height * 0.05))
    window_densities = [
        sum(row_densities[index : index + window_size]) / window_size
        for index in range(len(row_densities) - window_size + 1)
    ]
    if not window_densities:
        return 0.0

    peak_concentration = max(window_densities) - statistics.median(row_densities)
    return _clamp((peak_concentration - 0.08) / 0.18)


def score_frame(image_path: str | Path) -> tuple[float, FrameMetrics]:
    """按画质评分，并降低含烧录字幕画面的优先级。"""

    source = Path(image_path)
    if not source.is_file():
        raise FileNotFoundError(f"候选帧不存在：{source}")

    with Image.open(source) as image:
        rgb = image.convert("RGB")
        gray = rgb.convert("L")
        brightness = ImageStat.Stat(gray).mean[0] / 255.0
        contrast = _clamp(math.sqrt(ImageStat.Stat(gray).var[0]) / 72.0)

        edge_image = gray.filter(ImageFilter.FIND_EDGES)
        border = max(1, min(edge_image.size) // 80)
        if edge_image.width > border * 2 and edge_image.height > border * 2:
            edge_image = edge_image.crop(
                (border, border, edge_image.width - border, edge_image.height - border)
            )
        sharpness = _clamp(math.sqrt(ImageStat.Stat(edge_image).var[0]) / 64.0)
        saturation = ImageStat.Stat(rgb.convert("HSV").getchannel("S")).mean[0] / 255.0
        subtitle_risk = _subtitle_risk(rgb)

    exposure = _clamp(1.0 - abs(brightness - 0.52) / 0.52)
    base_score = (
        exposure * 0.30
        + contrast * 0.25
        + sharpness * 0.30
        + saturation * 0.15
    )
    score = max(0.0, 100.0 * base_score - subtitle_risk * 42.0)
    metrics = FrameMetrics(
        brightness=round(brightness, 4),
        exposure=round(exposure, 4),
        contrast=round(contrast, 4),
        sharpness=round(sharpness, 4),
        saturation=round(saturation, 4),
        subtitle_risk=round(subtitle_risk, 4),
    )
    return round(score, 2), metrics


def _cache_key(source: Path, count: int, intro_seconds: float, outro_seconds: float) -> str:
    stat = source.stat()
    raw = (
        f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{count}|"
        f"{intro_seconds:.3f}|{outro_seconds:.3f}|{CACHE_VERSION}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _cache_lock(frame_dir: Path) -> threading.Lock:
    key = str(frame_dir.resolve()).casefold()
    with _CACHE_LOCKS_GUARD:
        return _CACHE_LOCKS.setdefault(key, threading.Lock())


def _short_process_error(stderr: str | None) -> str:
    message = " ".join(str(stderr or "").split())
    return (message or "未知错误")[:500]


def _valid_metric_values(raw_metrics) -> FrameMetrics | None:
    if not isinstance(raw_metrics, dict):
        return None
    values = {}
    for name in FrameMetrics.__dataclass_fields__:
        value = raw_metrics.get(name)
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            return None
        values[name] = number
    return FrameMetrics(**values)


def _read_cached_candidates(
    manifest_path: Path,
    *,
    expected_count: int | None = None,
) -> list[FrameCandidate] | None:
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
            return None
        raw_candidates = payload.get("candidates")
        if not isinstance(raw_candidates, list) or not raw_candidates:
            return None
        if expected_count is not None and len(raw_candidates) != expected_count:
            return None
        cache_root = manifest_path.parent.resolve()
        candidates = []
        filenames = set()
        for item in raw_candidates:
            if not isinstance(item, dict):
                return None
            filename = item.get("filename")
            if (
                    not isinstance(filename, str)
                    or not filename
                    or Path(filename).name != filename
                    or Path(filename).suffix.casefold() not in {".jpg", ".jpeg"}
                    or filename.casefold() in filenames):
                return None
            filenames.add(filename.casefold())
            frame_path = (cache_root / filename).resolve()
            if frame_path.parent != cache_root:
                return None
            if not frame_path.is_file():
                return None
            with Image.open(frame_path) as image:
                image.verify()
            timestamp = float(item["timestamp"])
            score = float(item["score"])
            metrics = _valid_metric_values(item.get("metrics"))
            if (
                    not math.isfinite(timestamp)
                    or timestamp < 0
                    or not math.isfinite(score)
                    or score < 0
                    or metrics is None):
                return None
            candidates.append(
                FrameCandidate(
                    path=str(frame_path.resolve()),
                    timestamp=timestamp,
                    score=score,
                    metrics=metrics,
                    cached=True,
                )
            )
        return sorted(candidates, key=_subtitle_candidate_key)
    except (
        KeyError,
        OSError,
        SyntaxError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None


def _extract_frame(
    ffmpeg: str,
    source: Path,
    timestamp: float,
    output: Path,
    label: str,
) -> None:
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-y",
        str(output),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=FFMPEG_FRAME_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            f"候选帧 {label} 提取超时（{FFMPEG_FRAME_TIMEOUT_SECONDS} 秒）"
        ) from exc
    if result.returncode != 0 or not output.is_file():
        output.unlink(missing_ok=True)
        message = _short_process_error(result.stderr) if result.stderr else "没有生成图片"
        raise RuntimeError(f"候选帧 {label} 提取失败：{message}")


def _subtitle_candidate_key(candidate: FrameCandidate) -> tuple[float, float, float]:
    risk = candidate.metrics.subtitle_risk
    if risk <= LOW_SUBTITLE_RISK:
        return 0.0, -candidate.score, risk
    return 1.0, risk, -candidate.score


def _improve_subtitle_candidates(
    candidates: list[FrameCandidate],
    *,
    ffmpeg: str,
    source: Path,
    duration: float,
) -> list[FrameCandidate]:
    """仅在低风险帧不足时搜索邻近瞬间，优先避开烧录字幕。"""

    target_count = min(3, len(candidates))
    low_risk_count = sum(
        candidate.metrics.subtitle_risk <= LOW_SUBTITLE_RISK for candidate in candidates
    )
    if low_risk_count >= target_count:
        return candidates

    improved = list(candidates)
    for index, candidate in enumerate(candidates):
        if candidate.metrics.subtitle_risk <= LOW_SUBTITLE_RISK:
            continue
        best = candidate
        temporary_paths: list[Path] = []
        try:
            for offset_index, offset in enumerate(SUBTITLE_NEIGHBOR_OFFSETS, start=1):
                timestamp = max(0.05, min(duration - 0.05, candidate.timestamp + offset))
                temporary = Path(candidate.path).with_name(
                    f"{Path(candidate.path).stem}-neighbor-{offset_index}.jpg"
                )
                temporary_paths.append(temporary)
                _extract_frame(
                    ffmpeg,
                    source,
                    timestamp,
                    temporary,
                    f"{index + 1} 邻域 {offset_index}",
                )
                score, metrics = score_frame(temporary)
                alternative = FrameCandidate(
                    path=str(temporary.resolve()),
                    timestamp=round(timestamp, 3),
                    score=score,
                    metrics=metrics,
                )
                if _subtitle_candidate_key(alternative) < _subtitle_candidate_key(best):
                    best = alternative
                if best.metrics.subtitle_risk <= LOW_SUBTITLE_RISK:
                    break

            if best.path != candidate.path:
                canonical = Path(candidate.path)
                shutil.copyfile(best.path, canonical)
                best = replace(best, path=str(canonical.resolve()))
                improved[index] = best
            if improved[index].metrics.subtitle_risk <= LOW_SUBTITLE_RISK:
                low_risk_count += 1
                if low_risk_count >= target_count:
                    break
        finally:
            for temporary in temporary_paths:
                temporary.unlink(missing_ok=True)
    return improved


def extract_candidate_frames(
    video_path: str | Path,
    *,
    cache_dir: str | Path | None = None,
    count: int = 12,
    intro_seconds: float = 4.0,
    outro_seconds: float = 3.0,
    force: bool = False,
    ffmpeg_path: str | Path | None = None,
    ffprobe_path: str | Path | None = None,
) -> list[FrameCandidate]:
    """提取并按质量排序候选帧，源视频未变化时复用缓存。"""

    source = Path(video_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"视频文件不存在：{source}")
    cache_root = Path(cache_dir or Path.cwd() / ".cache" / "frames").expanduser().resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    frame_dir = cache_root / _cache_key(source, count, intro_seconds, outro_seconds)
    manifest_path = frame_dir / "manifest.json"
    with _cache_lock(frame_dir):
        if not force:
            cached = _read_cached_candidates(
                manifest_path,
                expected_count=count,
            )
            if cached is not None:
                return cached

        source_stat = source.stat()
        source_signature = (source_stat.st_size, source_stat.st_mtime_ns)
        metadata = probe_video(source, ffprobe_path=ffprobe_path)
        timestamps = plan_candidate_timestamps(
            metadata.duration,
            count,
            intro_seconds=intro_seconds,
            outro_seconds=outro_seconds,
        )
        ffmpeg = find_media_binary("ffmpeg", ffmpeg_path)
        staging_dir = cache_root / (
            f".{frame_dir.name}.{secrets.token_hex(6)}.tmp"
        )
        staging_dir.mkdir()
        try:
            candidates: list[FrameCandidate] = []
            for index, timestamp in enumerate(timestamps, start=1):
                output = staging_dir / f"frame-{index:02d}.jpg"
                _extract_frame(ffmpeg, source, timestamp, output, str(index))
                score, metrics = score_frame(output)
                candidates.append(
                    FrameCandidate(
                        path=str(output.resolve()),
                        timestamp=timestamp,
                        score=score,
                        metrics=metrics,
                    )
                )

            candidates = _improve_subtitle_candidates(
                candidates,
                ffmpeg=ffmpeg,
                source=source,
                duration=metadata.duration,
            )
            candidates.sort(key=_subtitle_candidate_key)
            current_stat = source.stat()
            if (current_stat.st_size, current_stat.st_mtime_ns) != source_signature:
                raise RuntimeError("源视频在候选帧提取期间发生变化，请重新生成")
            manifest = {
                "version": CACHE_VERSION,
                "video": metadata.to_dict(),
                "candidates": [
                    {
                        "filename": Path(candidate.path).name,
                        "timestamp": candidate.timestamp,
                        "score": candidate.score,
                        "metrics": candidate.metrics.to_dict(),
                    }
                    for candidate in candidates
                ],
            }
            (staging_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _replace_cache_directory(staging_dir, frame_dir)
            return [
                replace(
                    candidate,
                    path=str((frame_dir / Path(candidate.path).name).resolve()),
                    cached=False,
                )
                for candidate in candidates
            ]
        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)


def _replace_cache_directory(staging_dir: Path, frame_dir: Path) -> None:
    """用已完整构建的目录替换旧缓存，提交失败时恢复旧目录。"""

    backup_dir = frame_dir.parent / (
        f".{frame_dir.name}.{secrets.token_hex(6)}.backup"
    )
    had_previous = frame_dir.exists()
    if had_previous:
        frame_dir.replace(backup_dir)
    try:
        staging_dir.replace(frame_dir)
    except BaseException:
        if had_previous and backup_dir.exists():
            if frame_dir.exists():
                shutil.rmtree(frame_dir, ignore_errors=True)
            backup_dir.replace(frame_dir)
        raise
    else:
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
