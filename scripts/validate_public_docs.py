"""校验公开版文档、计划总账契约、相对链接和基本脱敏规则。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = (ROOT / "README.md", ROOT / "docs" / "配置说明.md", ROOT / "docs" / "日常工作流.md", ROOT / "docs" / "故障排查.md")
GOVERNANCE_DOCS = (
    ROOT / "ACTIVE_PLAN.md",
    ROOT / "docs" / "action-ledger.md",
    ROOT / "docs" / "plans" / "backend-hardening-next.md",
)
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
ACTION_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")
WINDOWS_PATH_RE = re.compile(r"(?i)(?:[a-z]:\\|file://)")
PRIVATE_MARKER_RE = re.compile(r"(?i)(?:api[_ -]?key|secret|private[_ -]?key)\s*[:=]\s*['\"]?(?!YOUR_|示例|占位)")


def _validate_dependency_contract(errors: list[str]) -> None:
    root_path = ROOT / "requirements.txt"
    pyproject_path = ROOT / "pyproject.toml"
    autocover_path = ROOT / "autocover_tool" / "requirements.txt"
    root_lines = {
        line.strip().casefold()
        for line in root_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    pyproject_text = pyproject_path.read_text(encoding="utf-8")
    dependency_block = re.search(
        r"(?ms)^dependencies\s*=\s*\[(.*?)^\]",
        pyproject_text,
    )
    if dependency_block is None:
        errors.append("pyproject.toml 缺少 [project].dependencies")
        project_dependencies: set[str] = set()
    else:
        project_dependencies = {
            value.casefold()
            for value in re.findall(r'["\']([^"\']+)["\']', dependency_block.group(1))
        }

    def dependency_names(specifications: set[str]) -> set[str]:
        names: set[str] = set()
        for specification in specifications:
            match = re.match(r"[a-z0-9_.-]+", specification)
            if match:
                names.add(match.group(0).replace("_", "-"))
        return names

    required_names = {
        "flask",
        "pillow",
        "funasr",
        "soxr",
        "python-docx",
        "requests",
    }
    root_names = dependency_names(root_lines)
    project_names = dependency_names(project_dependencies)
    missing_root = sorted(required_names - root_names)
    if missing_root:
        errors.append(f"requirements.txt 缺少公开根依赖：{', '.join(missing_root)}")
    missing_project = sorted(required_names - project_names)
    if missing_project:
        errors.append(f"pyproject.toml 缺少统一依赖：{', '.join(missing_project)}")
    if root_lines != project_dependencies:
        only_requirements = sorted(root_lines - project_dependencies)
        only_pyproject = sorted(project_dependencies - root_lines)
        details = []
        if only_requirements:
            details.append("仅 requirements.txt: " + ", ".join(only_requirements))
        if only_pyproject:
            details.append("仅 pyproject.toml: " + ", ".join(only_pyproject))
        errors.append("统一依赖声明不一致（" + "；".join(details) + "）")

    for dependency in ("torch", "torchaudio"):
        if dependency in root_names or dependency in project_names:
            errors.append(f"通用依赖不应包含隔离 GPU 依赖：{dependency}")

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


def _field(text: str, name: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(name)}:\s*([^\s#]+)", text)
    return match.group(1) if match else None


def _action_rows(text: str) -> list[tuple[str, str]]:
    rows = []
    row_re = re.compile(
        r"(?m)^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*[^|]+\s*\|\s*[^|]+\s*\|"
    )
    for match in row_re.finditer(text):
        action_id = match.group(1).strip()
        if ACTION_ID_RE.fullmatch(action_id):
            rows.append((action_id, match.group(2).strip()))
    return rows


def _validate_action_contract(
    plan_text: str,
    ledger_text: str,
    active_text: str,
    errors: list[str],
) -> None:
    plan_id = _field(plan_text, "plan_id")
    plan_version = _field(plan_text, "plan_version")
    active_plan_id = _field(active_text, "plan_id")
    active_plan_ref = _field(active_text, "plan_ref")
    active_ledger_ref = _field(active_text, "ledger_ref")
    if not plan_id or not plan_version:
        errors.append("backend-hardening-next.md 缺少 plan_id 或 plan_version")
    if active_plan_id != plan_id:
        errors.append("ACTIVE_PLAN.md 与专项计划的 plan_id 不一致")
    if active_plan_ref != "docs/plans/backend-hardening-next.md":
        errors.append("ACTIVE_PLAN.md 的 plan_ref 不指向 backend-hardening-next.md")
    if active_ledger_ref != "docs/action-ledger.md":
        errors.append("ACTIVE_PLAN.md 的 ledger_ref 不指向 docs/action-ledger.md")

    plan_rows = _action_rows(plan_text)
    plan_ids = [action_id for action_id, _ in plan_rows]
    duplicate_plan_ids = sorted({action_id for action_id in plan_ids if plan_ids.count(action_id) > 1})
    if duplicate_plan_ids:
        errors.append("专项计划存在重复 Action ID：" + ", ".join(duplicate_plan_ids))

    plan_id_set = set(plan_ids)
    graph = {action_id: set() for action_id in plan_ids}
    for action_id, dependency_text in plan_rows:
        dependencies = set(re.findall(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+", dependency_text))
        if dependency_text not in {"无", "-"}:
            unknown = sorted(dependencies - plan_id_set)
            if unknown:
                errors.append(
                    f"专项计划 {action_id} 引用了不存在的依赖：{', '.join(unknown)}"
                )
        graph[action_id].update(dependencies & plan_id_set)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(action_id: str) -> None:
        if action_id in visiting:
            errors.append("专项计划依赖存在环：" + action_id)
            return
        if action_id in visited:
            return
        visiting.add(action_id)
        for dependency in sorted(graph[action_id]):
            visit(dependency)
        visiting.remove(action_id)
        visited.add(action_id)

    for action_id in plan_ids:
        visit(action_id)

    ledger_ids = [action_id for action_id, _ in _action_rows(ledger_text)]
    duplicate_ledger_ids = sorted({action_id for action_id in ledger_ids if ledger_ids.count(action_id) > 1})
    if duplicate_ledger_ids:
        errors.append("Action ledger 存在重复 ID：" + ", ".join(duplicate_ledger_ids))


def _validate_governance_links(errors: list[str]) -> None:
    for path in GOVERNANCE_DOCS:
        if not path.is_file():
            errors.append(f"缺少治理文件：{path.relative_to(ROOT)}")
            continue
        text = path.read_text(encoding="utf-8")
        for target in LINK_RE.findall(text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if target.startswith("../commit/"):
                if not re.fullmatch(r"\.\./commit/[0-9a-fA-F]{40}", target):
                    errors.append(f"{path.relative_to(ROOT)} commit 链接不是 40 位 SHA：{target}")
                continue
            target_path = (path.parent / target).resolve()
            try:
                target_path.relative_to(ROOT)
            except ValueError:
                errors.append(f"{path.relative_to(ROOT)} 链接越过项目根目录：{target}")
                continue
            if not target_path.is_file() and not target_path.is_dir():
                errors.append(f"{path.relative_to(ROOT)} 链接目标不存在：{target}")

    plan_path = ROOT / "docs" / "plans" / "backend-hardening-next.md"
    ledger_path = ROOT / "docs" / "action-ledger.md"
    active_path = ROOT / "ACTIVE_PLAN.md"
    if all(path.is_file() for path in (plan_path, ledger_path, active_path)):
        _validate_action_contract(
            plan_path.read_text(encoding="utf-8"),
            ledger_path.read_text(encoding="utf-8"),
            active_path.read_text(encoding="utf-8"),
            errors,
        )


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
    _validate_governance_links(errors)

    if errors:
        for error in errors:
            print(f"文档校验失败：{error}", file=sys.stderr)
        return 1
    print("公开文档校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
