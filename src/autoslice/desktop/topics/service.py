"""话题分析桌面服务：给已有字幕的录播跑旧流水线的话题分析，产出报告。

第一版只接“已有字幕 + 现有话题分析”：不识别、不切片、不精剪。
不依赖 Flask；进度和取消由调用方（任务中心）传进来，产物布局与网页端一致。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from autoslice.artifact_store import artifact_bundle_layout
from autoslice.desktop.ai_settings import read_ai_settings

# (提示文字, 百分比 0–100)
ProgressCallback = Callable[[str, int], None]


class TopicAnalysisCancelled(RuntimeError):
    """用户停止了分析；已完成的分块检查点留着，下次接着跑。"""


@dataclass(frozen=True)
class TopicRequest:
    video_path: str
    subtitle_path: str
    # 同名 .ass 弹幕，没有就只看字幕
    danmaku_path: str | None = None
    streamer_profile_id: str = "auto"


@dataclass(frozen=True)
class TopicReport:
    video_path: str
    report_path: str
    overview_path: str
    artifact_dir: str
    topic_count: int
    clip_count: int
    failed_chunk_count: int = 0
    warning: str = ""

    @property
    def summary(self) -> str:
        text = f"{self.topic_count} 个话题，{self.clip_count} 个可切片段"
        if self.failed_chunk_count:
            text += f"，{self.failed_chunk_count} 段没分析成功"
        return text


def _run_pipeline(*args, **kwargs):
    # 延迟导入：整条流水线很重，桌面端启动时不加载
    from autoslice.pipeline import run_pipeline

    return run_pipeline(*args, **kwargs)


def analysis_model() -> str:
    """话题分块分析实际用的模型名（沿用旧流程，可用 AUTOSLICE_ANALYSIS_MODEL 改）。"""

    from autoslice.analysis.topic.analysis import LLM_ANALYSIS_MODEL

    return LLM_ANALYSIS_MODEL


class TopicAnalysisService:
    """一次只管一场录播；线程由调用方安排，本类不碰界面。"""

    def __init__(self, output_root: str | Path, *, runner: Callable | None = None,
                 settings_reader: Callable | None = None, model_name: Callable[[], str] | None = None):
        self.output_root = Path(output_root)
        self._runner = runner or _run_pipeline
        self._settings_reader = settings_reader or read_ai_settings
        self._model_name = model_name or analysis_model

    def layout(self, video_path: str) -> dict:
        return artifact_bundle_layout(video_path, output_dir=str(self.output_root),
                                      default_output_dir=str(self.output_root))

    def existing_report(self, video_path: str) -> str | None:
        path = self.layout(video_path)["report_path"]
        return path if os.path.isfile(path) else None

    @staticmethod
    def request_for(video_path: str, subtitle_path: str, streamer_profile_id: str = "auto") -> TopicRequest:
        danmaku = os.path.splitext(video_path)[0] + ".ass"
        return TopicRequest(video_path, subtitle_path, danmaku if os.path.isfile(danmaku) else None,
                            streamer_profile_id)

    @staticmethod
    def problem(request: TopicRequest) -> str:
        """不能分析的原因；能分析返回空串。"""

        if not os.path.isfile(request.video_path):
            return "录播文件不存在"
        if not request.subtitle_path or not os.path.isfile(request.subtitle_path):
            return "这个视频还没有字幕，先识别或放入同名 SRT"
        if not os.path.getsize(request.subtitle_path):
            return "字幕文件是空的"
        return ""

    def ai_problem(self) -> str:
        settings = self._settings_reader()
        if settings.error:
            return f"AI 配置有误：{settings.error}"
        if not settings.configured:
            return "未配置 AI，请先到设置页填好接口地址、密钥和模型"
        return ""

    def model_notice(self) -> str:
        """分析模型和设置页模型不一样时提醒一句；一样就是空串。"""

        try:
            configured = self._settings_reader().text_model
            model = self._model_name()
        except (OSError, ValueError, ImportError):
            return ""
        if not configured or configured == model:
            return ""
        return (f"注意：话题分析沿用旧流程的模型 {model}，不是设置页的 {configured}；"
                "接口不认这个模型名时会分析失败（可用环境变量 AUTOSLICE_ANALYSIS_MODEL 改）。")

    def run(self, request: TopicRequest, *, progress: ProgressCallback | None = None,
            cancel: threading.Event | None = None) -> TopicReport:
        problem = self.problem(request)
        if problem:
            raise ValueError(problem)

        def stopped() -> bool:
            return cancel is not None and cancel.is_set()

        def report(message, step, total):
            # 流水线在每次报进度时检查取消；正在等的那次请求会先跑完
            if stopped():
                raise TopicAnalysisCancelled("话题分析已停止")
            if progress is not None:
                try:
                    percent = int(round(float(step) / max(1.0, float(total)) * 100))
                except (TypeError, ValueError):
                    percent = 0
                progress(str(message), min(100, max(0, percent)))

        if stopped():
            raise TopicAnalysisCancelled("话题分析已停止")
        try:
            result = self._runner(
                request.video_path,
                ass_path=request.danmaku_path,
                progress_callback=report,
                # 第一版不接人工时间轴
                manual_timeline_path="__none__",
                output_dir=str(self.output_root),
                streamer_profile_id=request.streamer_profile_id,
                subtitle_path=request.subtitle_path,
            )
        except TopicAnalysisCancelled:
            raise
        except Exception as exc:
            if stopped():
                raise TopicAnalysisCancelled("话题分析已停止") from exc
            raise
        warning = "；".join(str(item) for item in (result.get("api_precheck_warning"),
                                                   result.get("unified_queue_warning")) if item)
        return TopicReport(
            video_path=request.video_path,
            report_path=str(result.get("md_path") or ""),
            overview_path=str(result.get("overview_path") or ""),
            artifact_dir=str(result.get("artifact_dir") or ""),
            topic_count=int(result.get("topic_count") or 0),
            clip_count=len(result.get("clip_marks") or ()),
            failed_chunk_count=len(result.get("failed_chunks") or ()),
            warning=warning,
        )
