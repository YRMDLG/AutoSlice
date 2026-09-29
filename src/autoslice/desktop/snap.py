"""按屏幕像素计算的字幕时间轴吸附。"""

from __future__ import annotations


class SnapEngine:
    def __init__(self, threshold_px: float = 8.0, release_px: float = 13.0):
        self.threshold_px = threshold_px
        self.release_px = release_px
        self.target: float | None = None

    def snap(self, seconds: float, targets: list[float], pixels_per_second: float,
             *, enabled: bool = True, bypass: bool = False) -> float:
        if not enabled or bypass or pixels_per_second <= 0:
            self.target = None
            return seconds
        if self.target is not None and abs(seconds - self.target) * pixels_per_second <= self.release_px:
            return self.target
        nearest = min(targets, key=lambda target: abs(target - seconds), default=None)
        if nearest is not None and abs(nearest - seconds) * pixels_per_second <= self.threshold_px:
            self.target = nearest
            return nearest
        self.target = None
        return seconds

    def reset(self) -> None:
        self.target = None
