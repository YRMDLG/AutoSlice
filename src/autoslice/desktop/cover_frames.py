"""取帧：候选帧与画质推荐、附近帧、更大范围和全片候选、视频元信息缓存。"""

from __future__ import annotations

import statistics
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from autoslice.desktop.projects import ProjectVideo
from autoslice_cover.video import (
    FrameMetrics,
    VideoMetadata,
    extract_frame_at_timestamp,
    plan_candidate_timestamps,
    probe_video,
)


@dataclass(frozen=True)
class CoverFrame:
    """一张已取出的候选帧及其画质评分（复用旧版 AutoCover 评分）。"""

    path: Path
    timestamp: float
    score: float
    metrics: FrameMetrics | None = None

    @property
    def subtitle_risk(self) -> float:
        return self.metrics.subtitle_risk if self.metrics else 0.0

def recommended_frame(frames: tuple[CoverFrame, ...]) -> CoverFrame | None:
    """附近帧里明显更好的一张；差距不明显时不推荐，避免噪声。"""

    scored = [item for item in frames if item.metrics is not None]
    if len(scored) < 3:
        return None
    median = statistics.median(item.score for item in scored)
    best = max(scored, key=lambda item: (item.score - item.subtitle_risk * 20, -abs(item.timestamp)))
    return best if best.score - median >= 4.0 and best.subtitle_risk < 0.5 else None

def best_overview_frame(frames: tuple[CoverFrame, ...]) -> CoverFrame | None:
    """全片候选里画质最好、字幕风险低的一张。"""

    if not frames:
        return None
    return max(frames, key=lambda item: (item.score - item.subtitle_risk * 20, -item.timestamp))


class CoverFrameService:
    """混入 CoverService；共享 storage、缓存与锁等状态。"""

    def _video_metadata(self, video: ProjectVideo) -> VideoMetadata:
        source = Path(video.path).expanduser().resolve()
        stat = source.stat()
        key = (str(source), stat.st_size, stat.st_mtime_ns)
        with self._metadata_lock:
            cached = self._metadata.get(key)
        if cached is None:
            cached = probe_video(source)
            with self._metadata_lock:
                self._metadata[key] = cached
        return cached

    def extract_frame_candidate(self, video: ProjectVideo, timestamp: float) -> CoverFrame:
        metadata = self._video_metadata(video)
        candidate, _metadata = extract_frame_at_timestamp(
            video.path,
            min(max(0.0, float(timestamp)), metadata.duration),
            cache_dir=self.storage.thumbnails / "cover-frames",
            metadata=metadata,
        )
        return CoverFrame(Path(candidate.path), candidate.timestamp, candidate.score, candidate.metrics)

    def extract_frame(self, video: ProjectVideo, timestamp: float) -> tuple[Path, float]:
        frame = self.extract_frame_candidate(video, timestamp)
        return frame.path, frame.timestamp

    def nearby_candidates(
        self,
        video: ProjectVideo,
        center: float,
        offsets: tuple[float, ...],
    ) -> tuple[CoverFrame, ...]:
        """顺序提取当前时刻附近帧并评分，供后台缩略条使用。"""

        frames: list[CoverFrame] = []
        seen: set[int] = set()
        for offset in offsets:
            timestamp = max(0.0, float(center) + float(offset))
            key = round(timestamp * 1000)
            if key in seen:
                continue
            seen.add(key)
            frames.append(self.extract_frame_candidate(video, timestamp))
        return tuple(frames)

    def extract_nearby_frames(
        self,
        video: ProjectVideo,
        center: float,
        offsets: tuple[float, ...],
    ) -> tuple[tuple[Path, float], ...]:
        return tuple((frame.path, frame.timestamp) for frame in self.nearby_candidates(video, center, offsets))

    @staticmethod
    def _wider_offsets(span: float, count: int) -> tuple[float, ...]:
        count = max(3, min(31, int(count)))
        span = max(1.0, min(180.0, float(span)))
        step = (span * 2.0) / (count - 1)
        return tuple(-span + index * step for index in range(count))

    def wider_candidates(
        self, video: ProjectVideo, center: float, *, span: float = 12.0, count: int = 13,
    ) -> tuple[CoverFrame, ...]:
        """显式“寻找更多画面”入口；只按需采样，不在切项目时阻塞。"""

        return self.nearby_candidates(video, center, self._wider_offsets(span, count))

    def overview_candidates(self, video: ProjectVideo, *, count: int = 8) -> tuple[CoverFrame, ...]:
        """全片均匀候选（避开片头片尾），没有播放位置时据此自动挑首帧。"""

        metadata = self._video_metadata(video)
        timestamps = plan_candidate_timestamps(metadata.duration, count)
        with ThreadPoolExecutor(max_workers=4) as pool:
            frames = list(pool.map(lambda value: self.extract_frame_candidate(video, value), timestamps))
        return tuple(sorted(frames, key=lambda item: item.timestamp))

    def video_duration(self, video: ProjectVideo) -> float:
        return float(self._video_metadata(video).duration)
