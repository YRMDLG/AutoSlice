"""校验公开版文档的必需入口、相对链接和基本脱敏规则。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = (ROOT / "README.md", ROOT / "docs" / "配置说明.md", ROOT / "docs" / "日常工作流.md", ROOT / "docs" / "故障排查.md")
EXAMPLE_FILES = (
    ROOT / "api_config.example.json",
    ROOT / "environment.example.ps1",
    ROOT / "title_style_profile.example.json",
)
REQUIRED = {
    "README.md": (
        "完整工作流", "快速开始", "配置说明", "MIT License",
        "Windows 10/11", "Linux logic-only GitHub Actions 不代表完整 Linux 支持",
        ".autoslice-state/tasks.sqlite3", "interrupted",
    ),
    "docs/配置说明.md": (
        "API 与代理配置", "base_url", "OpenAI", "Anthropic", "FunASR",
        "人工时间轴", "trust_env=False", "AUTOSLICE_LLM_PROXY_HTTPS",
        "至少 32 个字符", "SQLite 任务历史与检查点",
        "gpt-5.6-luna", "gpt-5.6-terra",
    ),
    "docs/日常工作流.md": (
        "运行话题分析", "自动切片", "字幕校对与压制", "AutoCover",
        "投稿前检查", "generic", "interrupted",
    ),
    "docs/故障排查.md": (
        "API 返回 500", "HTTP 503", "LLM 代理连接失败", "direct",
        "system", "custom", "任务冲突、中断或无法恢复", "安全校验失败",
        "FunASR 模型下载失败", "GPU 不可用", "端口", "ffmpeg", "扫描不到视频",
    ),
}
MEDIA_EXTENSIONS = (".flv", ".mp4", ".mkv", ".mov", ".avi")
STALE_PHRASES = (
    "AT_LEAST_24_RANDOM_CHARACTERS",
    "至少 24 个字符",
)
LINK_RE = re.compile(r"\]\(([^)#]+)(?:#[^)]+)?\)")
WINDOWS_PATH_RE = re.compile(r"(?i)(?:[a-z]:\\|file://)")
PRIVATE_MARKER_RE = re.compile(r"(?i)(?:api[_ -]?key|secret|private[_ -]?key)\s*[:=]\s*['\"]?(?!YOUR_|示例|占位)")


def _validate_dependency_contract(errors: list[str]) -> None:
    root_path = ROOT / "requirements.txt"
    autocover_path = ROOT / "autocover_tool" / "requirements.txt"
    root_lines = {
        line.strip().casefold()
        for line in root_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    required_root = {
        "flask>=3.0",
        "funasr>=1.4.1",
        "soxr>=1.0",
        "python-docx",
        "requests>=2.31",
    }
    missing = sorted(required_root - root_lines)
    if missing:
        errors.append(f"requirements.txt 缺少公开根依赖：{', '.join(missing)}")
    forbidden_root = ("pillow", "torch", "torchaudio")
    for dependency in forbidden_root:
        if any(re.match(rf"{dependency}(?:$|[<>=!~;\[])", line) for line in root_lines):
            errors.append(f"requirements.txt 不应包含隔离依赖：{dependency}")

    autocover_text = autocover_path.read_text(encoding="utf-8").casefold()
    for dependency in ("flask", "pillow"):
        if not re.search(rf"(?m)^{dependency}(?:$|[<>=!~;\[])", autocover_text):
            errors.append(f"autocover_tool/requirements.txt 缺少：{dependency}")


def _validate_examples(errors: list[str]) -> None:
    api_path = ROOT / "api_config.example.json"
    try:
        payload = json.loads(api_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"api_config.example.json 无效：{exc}")
    else:
        expected = {
            "token": "YOUR_API_TOKEN",
            "proxy_mode": "direct",
            "http_proxy": None,
            "https_proxy": None,
        }
        for field, value in expected.items():
            if payload.get(field) != value:
                errors.append(f"api_config.example.json 字段 {field} 必须为安全默认示例")

    environment = (ROOT / "environment.example.ps1").read_text(encoding="utf-8")
    for phrase in (
        'AUTOSLICE_LLM_PROXY_MODE = "direct"',
        "AT_LEAST_32_RANDOM_CHARACTERS",
    ):
        if phrase not in environment:
            errors.append(f"environment.example.ps1 缺少安全示例：{phrase}")
    for phrase in STALE_PHRASES:
        if phrase in environment:
            errors.append(f"environment.example.ps1 含过期示例：{phrase}")


def main() -> int:
    errors: list[str] = []
    for path in EXAMPLE_FILES:
        if not path.is_file():
            errors.append(f"缺少示例配置：{path.relative_to(ROOT)}")
    for path in DOCS:
        if not path.is_file():
            errors.append(f"缺少文档：{path.relative_to(ROOT)}")
            continue
        relative = str(path.relative_to(ROOT)).replace("\\", "/")
        text = path.read_text(encoding="utf-8")
        for phrase in REQUIRED.get(relative, ()):
            if phrase not in text:
                errors.append(f"{relative} 缺少必需内容：{phrase}")
        if relative in {"README.md", "docs/配置说明.md"}:
            for extension in MEDIA_EXTENSIONS:
                if extension not in text:
                    errors.append(f"{relative} 缺少支持格式：{extension}")
        for phrase in STALE_PHRASES:
            if phrase in text:
                errors.append(f"{relative} 含过期安全说明：{phrase}")
        if WINDOWS_PATH_RE.search(text):
            errors.append(f"{relative} 含有本机绝对路径或 file URL")
        if PRIVATE_MARKER_RE.search(text):
            errors.append(f"{relative} 疑似包含未脱敏凭据字段")
        for target in LINK_RE.findall(text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            target_path = (path.parent / target).resolve()
            try:
                target_path.relative_to(ROOT)
            except ValueError:
                errors.append(f"{relative} 链接越过项目根目录：{target}")
                continue
            if not target_path.is_file():
                errors.append(f"{relative} 链接目标不存在：{target}")

    _validate_dependency_contract(errors)
    _validate_examples(errors)

    if errors:
        for error in errors:
            print(f"文档校验失败：{error}", file=sys.stderr)
        return 1
    print("公开文档校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
