"""以显式授权运行真实 AutoSlice 分析/切片流水线的安全冒烟入口。

默认只校验路径并打印脱敏计划。真实分析需要 ``--allow-paid-llm``，
实际切片还必须同时提供 ``--allow-ffmpeg``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VIDEO_EXTENSIONS = frozenset({".avi", ".flv", ".mkv", ".mov", ".mp4"})
DANMAKU_EXTENSIONS = frozenset({".ass", ".xml"})
TIMELINE_EXTENSIONS = frozenset({".docx"})
OUTPUT_MARKER_NAME = ".autoslice-real-smoke.json"
OUTPUT_MARKER_SCHEMA_VERSION = 1
_PROFILE_ID_RE = re.compile(r"^(?:auto|[a-z0-9][a-z0-9_-]{0,31})$")

AnalyzeRunner = Callable[..., Mapping[str, Any]]
SliceRunner = Callable[..., tuple[int, str]]


class SmokeValidationError(ValueError):
    """表示可安全展示、且不会包含用户输入正文的参数错误。"""


class SafeArgumentParser(argparse.ArgumentParser):
    """避免 argparse 在错误信息中回显任意命令行值。"""

    def error(self, _message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: 参数错误：缺少参数或参数无效。\n")


@dataclass(frozen=True)
class SmokePlan:
    """完成纯路径校验后的不可变执行计划。"""

    video: Path
    srt: Path
    danmaku: Path
    timeline: Path
    output_dir: Path
    streamer_profile: str
    allow_paid_llm: bool
    allow_ffmpeg: bool
    resume_existing_output: bool


def build_parser() -> argparse.ArgumentParser:
    """构造无隐式默认输入的命令行解析器。"""

    parser = SafeArgumentParser(
        description=(
            "安全的真实流水线冒烟入口。默认仅 dry-run；不会自动访问 LLM、"
            "FFmpeg、网络或启动 Flask 服务。"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--video", required=True, help="本地绝对视频路径")
    parser.add_argument(
        "--srt",
        required=True,
        help="本地绝对 SRT 路径，必须是视频同目录同 stem 的 sibling 文件",
    )
    parser.add_argument(
        "--danmaku",
        required=True,
        help="本地绝对弹幕路径，仅支持 .ass/.xml",
    )
    parser.add_argument(
        "--timeline",
        required=True,
        help="本地绝对人工时间轴路径，仅支持 .docx；只作为分析参考",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="本地绝对输出目录；默认必须尚不存在",
    )
    parser.add_argument(
        "--streamer-profile",
        default="generic",
        help=(
            "主播 profile；接受 generic、auto 或 streamer_profiles.json 中"
            "已配置的 id，未知值直接拒绝"
        ),
    )
    parser.add_argument(
        "--resume-existing-output",
        action="store_true",
        help=(
            "续跑由本脚本创建且输入身份一致的已有目录；不允许接管任意目录"
        ),
    )
    parser.add_argument(
        "--allow-paid-llm",
        action="store_true",
        help="显式授权真实 LLM 分析；该阶段可能调用 ffprobe 读取元数据",
    )
    parser.add_argument(
        "--allow-ffmpeg",
        action="store_true",
        help="显式授权实际切片；必须与 --allow-paid-llm 同时使用",
    )
    return parser


def _local_absolute_path(raw_value: str, option: str) -> Path:
    """只接受本地绝对路径，并拒绝 UNC/网络共享语法。"""

    if not raw_value or "\x00" in raw_value:
        raise SmokeValidationError(f"{option} 必须使用本地绝对路径")
    if raw_value.startswith(("\\\\", "//")):
        raise SmokeValidationError(f"{option} 不接受网络共享路径")
    path = Path(raw_value)
    if not path.is_absolute():
        raise SmokeValidationError(f"{option} 必须使用本地绝对路径")
    return path


def _existing_input(
        raw_value: str,
        option: str,
        extensions: frozenset[str],
) -> Path:
    """校验输入扩展名与存在性，不打开或读取文件正文。"""

    path = _local_absolute_path(raw_value, option)
    if path.suffix.casefold() not in extensions:
        allowed = "/".join(sorted(extensions))
        raise SmokeValidationError(f"{option} 仅接受 {allowed} 文件")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SmokeValidationError(f"{option} 指向的本地文件不存在") from exc
    if not resolved.is_file():
        raise SmokeValidationError(f"{option} 必须指向本地文件")
    return resolved


def _path_key(path: Path) -> str:
    """生成适用于当前平台的规范化路径身份。"""

    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _is_inside(path: Path, parent: Path) -> bool:
    """判断 path 是否严格位于 parent 内部。"""

    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return _path_key(path) != _path_key(parent)


def _resolve_streamer_profile_id(raw_value: str, video: Path) -> str:
    """验证任意已配置 profile，避免在人工脚本中维护硬编码列表。"""

    profile_id = str(raw_value or "").strip().casefold()
    if not _PROFILE_ID_RE.fullmatch(profile_id):
        raise SmokeValidationError("--streamer-profile 格式无效")
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from streamer_profiles import resolve_streamer_profile

        resolve_streamer_profile(profile_id, video)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SmokeValidationError(
            "--streamer-profile 未在当前主播配置中定义"
        ) from exc
    return profile_id


def _path_digest(path: Path) -> str:
    """以不可逆摘要记录输入身份，不把本机绝对路径写入标记。"""

    return hashlib.sha256(_path_key(path).encode("utf-8")).hexdigest()


def _marker_payload(plan: SmokePlan) -> dict[str, object]:
    """构造只用于确认目录归属和输入一致性的本地标记。"""

    return {
        "schema_version": OUTPUT_MARKER_SCHEMA_VERSION,
        "kind": "autoslice-real-pipeline-smoke",
        "inputs": {
            "video": _path_digest(plan.video),
            "srt": _path_digest(plan.srt),
            "danmaku": _path_digest(plan.danmaku),
            "timeline": _path_digest(plan.timeline),
        },
        "streamer_profile": plan.streamer_profile,
    }


def _marker_path(output_dir: Path) -> Path:
    return output_dir / OUTPUT_MARKER_NAME


def _validate_existing_output(plan: SmokePlan) -> None:
    """只接受由本脚本为同一组输入创建的续跑目录。"""

    marker_path = _marker_path(plan.output_dir)
    if marker_path.is_symlink() or not marker_path.is_file():
        raise SmokeValidationError(
            "--resume-existing-output 只接受本脚本创建的已有目录"
        )
    try:
        if marker_path.stat().st_size > 16 * 1024:
            raise SmokeValidationError("续跑目录归属标记无效")
        with marker_path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except SmokeValidationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SmokeValidationError("续跑目录归属标记无效") from exc
    if payload != _marker_payload(plan):
        raise SmokeValidationError("续跑目录与本次输入或主播配置不一致")


def _prepare_output_dir(plan: SmokePlan) -> None:
    """首次执行创建归属标记；续跑时再次确认标记，防止 TOCTOU。"""

    if plan.resume_existing_output:
        _validate_existing_output(plan)
        return

    plan.output_dir.mkdir(exist_ok=False)
    marker_path = _marker_path(plan.output_dir)
    temporary_marker = marker_path.with_suffix(marker_path.suffix + ".tmp")
    try:
        with temporary_marker.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                _marker_payload(plan),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
        os.replace(temporary_marker, marker_path)
    except Exception:
        temporary_marker.unlink(missing_ok=True)
        marker_path.unlink(missing_ok=True)
        try:
            plan.output_dir.rmdir()
        except OSError:
            pass
        raise


def validate_arguments(args: argparse.Namespace) -> SmokePlan:
    """执行不会读取媒体正文、不会写文件的完整安全前置校验。"""

    if args.allow_ffmpeg and not args.allow_paid_llm:
        raise SmokeValidationError(
            "--allow-ffmpeg 不能单独使用；实际切片还需要 --allow-paid-llm"
        )

    video = _existing_input(args.video, "--video", VIDEO_EXTENSIONS)
    srt = _existing_input(args.srt, "--srt", frozenset({".srt"}))
    danmaku = _existing_input(args.danmaku, "--danmaku", DANMAKU_EXTENSIONS)
    timeline = _existing_input(args.timeline, "--timeline", TIMELINE_EXTENSIONS)

    expected_srt = video.with_suffix(".srt")
    if _path_key(srt) != _path_key(expected_srt):
        raise SmokeValidationError(
            "--srt 必须是 --video 同目录同 stem 的 sibling .srt 文件"
        )

    inputs = (video, srt, danmaku, timeline)
    if len({_path_key(path) for path in inputs}) != len(inputs):
        raise SmokeValidationError("四个输入必须是彼此不同的本地文件")

    output_dir = _local_absolute_path(args.output_dir, "--output-dir")
    try:
        output_dir = output_dir.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise SmokeValidationError("--output-dir 无法解析") from exc

    for input_path in inputs:
        if (
                _path_key(output_dir) == _path_key(input_path)
                or _is_inside(output_dir, input_path)
                or _is_inside(input_path, output_dir)):
            raise SmokeValidationError(
                "--output-dir 不得等于、包含或位于任何输入路径内部"
            )

    if not output_dir.parent.is_dir():
        raise SmokeValidationError("--output-dir 的直接父目录必须已经存在")

    streamer_profile = _resolve_streamer_profile_id(
        args.streamer_profile,
        video,
    )
    if args.resume_existing_output:
        if not output_dir.is_dir():
            raise SmokeValidationError(
                "--resume-existing-output 要求输出目录已经存在"
            )
    elif output_dir.exists():
        raise SmokeValidationError(
            "--output-dir 必须尚不存在；续跑请显式使用 "
            "--resume-existing-output"
        )

    plan = SmokePlan(
        video=video,
        srt=srt,
        danmaku=danmaku,
        timeline=timeline,
        output_dir=output_dir,
        streamer_profile=streamer_profile,
        allow_paid_llm=args.allow_paid_llm,
        allow_ffmpeg=args.allow_ffmpeg,
        resume_existing_output=args.resume_existing_output,
    )
    if plan.resume_existing_output:
        _validate_existing_output(plan)
    return plan


def _redacted_file(path: Path) -> dict[str, str]:
    """只保留文件类型，不暴露本机目录或输入文件名。"""

    return {"location": "<local-file>", "extension": path.suffix.casefold()}


def _plan_summary(plan: SmokePlan) -> dict[str, Any]:
    """构造不含路径、凭据和输入正文的 dry-run/执行前计划。"""

    if not plan.allow_paid_llm:
        mode = "dry-run"
        analysis = "不会导入或调用真实分析 owner"
        slicing = "不会导入或调用真实切片 owner"
    elif not plan.allow_ffmpeg:
        mode = "paid-analysis-only"
        analysis = "将调用真实 LLM；可能使用 ffprobe 获取视频元数据"
        slicing = "不会调用切片 owner，不会生成成片"
    else:
        mode = "paid-analysis-and-slicing"
        analysis = "将调用真实 LLM；可能使用 ffprobe 获取视频元数据"
        slicing = "分析成功后将调用唯一切片 owner 生成成片"
    return {
        "mode": mode,
        "video": _redacted_file(plan.video),
        "srt": _redacted_file(plan.srt),
        "srt_relationship": "validated-video-sibling",
        "danmaku": _redacted_file(plan.danmaku),
        "timeline": _redacted_file(plan.timeline),
        "timeline_role": "reference-only",
        "output_dir": (
            "<validated-existing-smoke-directory>"
            if plan.resume_existing_output
            else "<new-local-directory-not-created>"
        ),
        "resume_existing_output": plan.resume_existing_output,
        "streamer_profile": plan.streamer_profile,
        "paid_llm_authorized": plan.allow_paid_llm,
        "ffmpeg_authorized": plan.allow_ffmpeg,
        "analysis": analysis,
        "slicing": slicing,
        "flask_services": "not-started",
    }


def _load_analyze_runner() -> AnalyzeRunner:
    """仅在付费分析已授权后延迟导入唯一分析 owner。"""

    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from autoslice.pipeline import run_pipeline

    return run_pipeline


def _load_slice_runner() -> SliceRunner:
    """仅在双重授权后延迟导入唯一切片 owner。"""

    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from autoslice.slicing import slice_from_marks

    return slice_from_marks


def _safe_failure(stage: str, exc: BaseException) -> None:
    """报告可定位的失败阶段，同时隐藏可能含凭据或正文的异常消息。"""

    print(
        f"真实流水线冒烟失败：{stage}（{type(exc).__name__}；详细信息已隐藏）。",
        file=sys.stderr,
    )


def _count(value: object) -> int:
    """把 runner 数量字段收敛为非负整数。"""

    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _result_path(result: Mapping[str, Any], key: str) -> str | None:
    """只从白名单结果字段提取可复核路径。"""

    value = result.get(key)
    if isinstance(value, (str, os.PathLike)):
        return os.fspath(value)
    return None


def run_authorized_pipeline(
        plan: SmokePlan,
        *,
        analyze_runner: AnalyzeRunner | None = None,
        slice_runner: SliceRunner | None = None,
) -> dict[str, Any]:
    """在明确授权后复用真实 owner；不复制任何分析或切片算法。"""

    analyzer = analyze_runner if analyze_runner is not None else _load_analyze_runner()
    slicer = None
    if plan.allow_ffmpeg:
        slicer = slice_runner if slice_runner is not None else _load_slice_runner()

    # 目录准备刻意放在所有 dry-run 分支之后；已有目录必须通过归属校验。
    _prepare_output_dir(plan)
    result = analyzer(
        os.fspath(plan.video),
        ass_path=os.fspath(plan.danmaku),
        manual_timeline_path=os.fspath(plan.timeline),
        output_dir=os.fspath(plan.output_dir),
        streamer_profile_id=plan.streamer_profile,
    )
    if not isinstance(result, Mapping):
        raise TypeError("分析 owner 必须返回 mapping")

    slice_count = 0
    slice_dir = None
    if slicer is not None:
        json_path = _result_path(result, "json_path")
        if not json_path:
            raise RuntimeError("分析结果缺少 json_path")
        resolved_profile = result.get("streamer_profile_id")
        if not isinstance(resolved_profile, str) or not resolved_profile.strip():
            resolved_profile = plan.streamer_profile
        slice_count, slice_dir = slicer(
            os.fspath(plan.video),
            json_path,
            os.fspath(plan.output_dir),
            streamer_profile_id=resolved_profile,
        )

    return {
        "status": "completed",
        "mode": (
            "paid-analysis-and-slicing"
            if plan.allow_ffmpeg
            else "paid-analysis-only"
        ),
        "output_dir": os.fspath(plan.output_dir),
        "artifact_dir": _result_path(result, "artifact_dir"),
        "overview_path": _result_path(result, "overview_path"),
        "json_path": _result_path(result, "json_path"),
        "topic_count": _count(result.get("topic_count")),
        "slice_count": _count(slice_count),
        "slice_dir": os.fspath(slice_dir) if slice_dir else None,
        "streamer_profile_id": (
            result.get("streamer_profile_id")
            if isinstance(result.get("streamer_profile_id"), str)
            else plan.streamer_profile
        ),
    }


def main(
        argv: list[str] | None = None,
        *,
        analyze_runner: AnalyzeRunner | None = None,
        slice_runner: SliceRunner | None = None,
) -> int:
    """解析参数并执行 dry-run 或已明确授权的真实流水线。"""

    args = build_parser().parse_args(argv)
    try:
        plan = validate_arguments(args)
    except SmokeValidationError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2

    print("安全冒烟计划：")
    print(json.dumps(_plan_summary(plan), ensure_ascii=False, indent=2))
    if not plan.allow_paid_llm:
        print("dry-run 完成：未导入真实 owner，未创建输出，未访问外部能力。")
        return 0

    try:
        summary = run_authorized_pipeline(
            plan,
            analyze_runner=analyze_runner,
            slice_runner=slice_runner,
        )
    except Exception as exc:
        _safe_failure("执行阶段失败", exc)
        return 1

    print("可复核执行结果：")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
