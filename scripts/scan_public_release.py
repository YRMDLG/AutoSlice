"""扫描将进入公开仓库的文件，阻止凭据、本机路径和私有资产。"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_NAMES = {
    ".env",
    "api_config.json",
    "plan.md",
    "项目总结.md",
    "项目总结.json",
    "对话总结.md",
    "对话总结.json",
}
FORBIDDEN_SUFFIXES = {
    ".flv", ".mp4", ".mkv", ".mov", ".avi", ".ts",
    ".wav", ".mp3", ".srt", ".ass", ".xml", ".docx",
    ".ttf", ".otf", ".ttc", ".pt", ".onnx", ".ckpt",
}
FORBIDDEN_PARTS = {
    ".codex-tmp",
    ".cache",
    "__pycache__",
    "recordings",
    "output",
    "submissions",
    "covers",
    "stickers",
    "timelines",
}
GENERATED_NAME_RE = re.compile(
    r"(?:_话题分析\.md|_clip_marks\.json|_checkpoint\.json|"
    r"_优化时间轴\.(?:json|md)|_精调任务\.(?:json|md))$",
    re.IGNORECASE,
)
WINDOWS_PATH_RE = re.compile(r"(?i)\b([a-z]:\\[^\r\n'\"`]+)")
ALLOWED_SYNTHETIC_PATH_PREFIXES = (
    "x:\\fixtures",
    "x:\\runtime",
    "c:\\runtime",
    "c:\\python310",
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
)
SECRET_PATTERNS = (
    ("私钥", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("API token", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("Bearer token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{20,}\b")),
    ("Bilibili cookie", re.compile(r"(?i)\b(?:SESSDATA|bili_jct|DedeUserID)=")),
    ("file URL", re.compile(r"(?i)\bfile://")),
    ("外部开发工具凭据路径", re.compile(r"(?i)\\\.claude\\|/\.claude/")),
)
def _candidate_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    names = result.stdout.decode("utf-8", errors="strict").split("\0")
    return sorted(ROOT / name for name in names if name and (ROOT / name).is_file())


def _path_errors(path: Path) -> list[str]:
    relative = PurePosixPath(path.relative_to(ROOT).as_posix())
    lower_parts = tuple(part.casefold() for part in relative.parts)
    errors: list[str] = []
    if relative.name.casefold() in FORBIDDEN_NAMES:
        errors.append("禁止发布的本地配置或计划文件")
    if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
        errors.append("禁止发布的视频、字幕、字体、时间轴或模型资产")
    if any(
        part in FORBIDDEN_PARTS or part.startswith(".codex-tmp-")
        for part in lower_parts
    ):
        errors.append("禁止发布的运行目录或缓存")
    if "video-topic-analyzer" in relative.as_posix().casefold():
        errors.append("包含未获再分发许可的外部 skill")
    if GENERATED_NAME_RE.search(relative.name):
        errors.append("包含分析报告、检查点或精调产物")
    if len(lower_parts) >= 3 and lower_parts[-3:-1] == ("local", "fonts"):
        errors.append("包含 AutoCover 本机字体")
    return errors


def _text_errors(path: Path, text: str) -> list[str]:
    errors: list[str] = []
    relative = path.relative_to(ROOT).as_posix()
    if relative == "scripts/scan_public_release.py":
        return errors
    is_security_fixture = (
        relative.startswith("test_")
        or "/tests/" in relative
        or relative == "scripts/validate_public_docs.py"
    )
    for label, pattern in SECRET_PATTERNS:
        if is_security_fixture and label in {"file URL", "外部开发工具凭据路径"}:
            continue
        if pattern.search(text):
            errors.append(f"疑似包含{label}")
    for match in WINDOWS_PATH_RE.finditer(text):
        value = re.sub(r"\\+", r"\\", match.group(1).casefold())
        if not value.startswith(ALLOWED_SYNTHETIC_PATH_PREFIXES):
            errors.append(f"包含非测试用途的 Windows 绝对路径：{match.group(1)[:80]}")
    return errors


def _ignore_errors() -> list[str]:
    sentinels = (
        "api_config.json",
        ".env",
        "environment.local.ps1",
        "PLAN.md",
        "recordings/sample.flv",
        "output/sample.mp4",
        "timelines/sample.docx",
        "autocover_tool/local/fonts/sample.ttf",
        "autocover_tool/.cache/frame.jpg",
    )
    encoded = b"\0".join(item.encode("utf-8") for item in sentinels) + b"\0"
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-z", "--stdin"],
        cwd=ROOT,
        input=encoded,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ignored = {
        item.decode("utf-8").replace("\\", "/")
        for item in result.stdout.split(b"\0")
        if item
    }
    return [f".gitignore 未覆盖：{item}" for item in sentinels if item not in ignored]


def main() -> int:
    errors: list[str] = []
    files = _candidate_files()
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        for message in _path_errors(path):
            errors.append(f"{relative}：{message}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(f"{relative}：公开版源码必须是 UTF-8 文本，禁止未知二进制文件")
            continue
        except OSError as exc:
            errors.append(f"{relative}：无法读取：{exc}")
            continue
        for message in _text_errors(path, text):
            errors.append(f"{relative}：{message}")
    errors.extend(_ignore_errors())
    if errors:
        for error in errors:
            print(f"发布扫描失败：{error}", file=sys.stderr)
        return 1
    print(f"公开发布扫描通过：{len(files)} 个候选文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
