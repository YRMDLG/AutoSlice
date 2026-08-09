"""校验公开版文档的必需入口、相对链接和基本脱敏规则。"""

from __future__ import annotations

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
    "README.md": ("完整工作流", "快速开始", "配置说明", "MIT License"),
    "docs/配置说明.md": ("API 配置", "base_url", "OpenAI", "Anthropic", "FunASR", "人工时间轴"),
    "docs/日常工作流.md": ("运行话题分析", "自动切片", "字幕校对与压制", "AutoCover", "投稿前检查"),
    "docs/故障排查.md": ("API 返回 500", "FunASR 模型下载失败", "GPU 不可用", "端口", "ffmpeg", "扫描不到视频"),
}
LINK_RE = re.compile(r"\]\(([^)#]+)(?:#[^)]+)?\)")
WINDOWS_PATH_RE = re.compile(r"(?i)(?:[a-z]:\\|file://)")
PRIVATE_MARKER_RE = re.compile(r"(?i)(?:api[_ -]?key|secret|private[_ -]?key)\s*[:=]\s*['\"]?(?!YOUR_|示例|占位)")


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

    if errors:
        for error in errors:
            print(f"文档校验失败：{error}", file=sys.stderr)
        return 1
    print("公开文档校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
