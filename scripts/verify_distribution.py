"""在仓库外构建并验证 AutoSlice wheel 安装。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BAD_WHEEL_MEMBER = "autoslice/analysis/topic/response.py"


def _release_files(root: Path = ROOT) -> list[Path]:
    """返回 Git 会纳入发布候选的文件，排除本机忽略数据。"""

    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    names = result.stdout.decode("utf-8", errors="strict").split("\0")
    return [root / name for name in names if name and (root / name).is_file()]


def _copy_release(destination: Path, root: Path = ROOT) -> None:
    for source in _release_files(root):
        target = destination / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _venv_executable(venv_root: Path, name: str) -> Path:
    if os.name == "nt":
        return venv_root / "Scripts" / f"{name}.exe"
    return venv_root / "bin" / name


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float = 300,
) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"命令失败 ({result.returncode}): {' '.join(command)}\n"
            f"{result.stdout[-5000:]}"
        )
    if result.stdout.strip():
        print(result.stdout.rstrip())
    return result.stdout


def _direct_build_environment() -> dict[str, str]:
    """只为构建子进程清除代理，不修改系统服务或父进程环境。"""

    env = os.environ.copy()
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env.pop(name, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def _runtime_environment(data_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "AUTOSLICE_WORKSPACE_DIR": "",
            "AUTOSLICE_USER_DATA_DIR": str(data_root / "autoslice"),
            "AUTOSLICE_STATE_DIR": str(data_root / "state"),
            "AUTOCOVER_DATA_DIR": str(data_root / "autocover"),
        }
    )
    return env


def _package_tree(package_root: Path) -> dict[str, tuple[int, str]]:
    """记录已安装包内容，检测帮助和导入命令是否回写 site-packages。"""

    result: dict[str, tuple[int, str]] = {}
    for path in sorted(package_root.rglob("*")):
        if not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result[path.relative_to(package_root).as_posix()] = (path.stat().st_size, digest)
    return result


def _installed_origins(
    python: Path,
    *,
    cwd: Path,
    env: dict[str, str],
) -> dict[str, str]:
    script = (
        "import json, autoslice, autoslice_cover; "
        "print(json.dumps({"
        "'autoslice': autoslice.__file__, "
        "'autoslice_cover': autoslice_cover.__file__"
        "}, ensure_ascii=False))"
    )
    output = _run_checked([str(python), "-B", "-c", script], cwd=cwd, env=env)
    return json.loads(output.splitlines()[-1])


def _assert_installed_from_venv(
    origins: dict[str, str],
    *,
    venv_root: Path,
    source_root: Path,
) -> tuple[Path, Path]:
    resolved_venv = venv_root.resolve()
    resolved_source = source_root.resolve()
    package_roots: list[Path] = []
    for name in ("autoslice", "autoslice_cover"):
        origin = Path(origins[name]).resolve()
        if resolved_source == origin or resolved_source in origin.parents:
            raise RuntimeError(f"{name} 仍从源码副本导入: {origin}")
        if resolved_venv not in origin.parents:
            raise RuntimeError(f"{name} 未从临时安装环境导入: {origin}")
        package_roots.append(origin.parent)
    return package_roots[0], package_roots[1]


def _build_and_install_wheel(
    *,
    source_root: Path,
    wheel_dir: Path,
    venv_root: Path,
    outside_root: Path,
) -> tuple[Path, Path]:
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(venv_root)
    python = _venv_executable(venv_root, "python")
    build_env = _direct_build_environment()
    _run_checked(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "pip>=24,<27",
        ],
        cwd=outside_root,
        env=build_env,
    )
    _run_checked(
        [
            str(python),
            "-m",
            "pip",
            "wheel",
            str(source_root),
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
        ],
        cwd=outside_root,
        env=build_env,
    )
    wheels = sorted(wheel_dir.glob("autoslice-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"预期生成 1 个 wheel，实际为 {len(wheels)} 个")
    _run_checked(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--force-reinstall",
            str(wheels[0]),
        ],
        cwd=outside_root,
        env=build_env,
    )
    return python, wheels[0]


def _make_bad_wheel(wheel_path: Path, output_path: Path) -> None:
    """从正常 wheel 制造一个仅缺少深层模块的受控反例。"""

    with zipfile.ZipFile(wheel_path) as source:
        names = set(source.namelist())
        if BAD_WHEEL_MEMBER not in names:
            raise RuntimeError(f"正常 wheel 缺少反例目标：{BAD_WHEEL_MEMBER}")
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                if info.filename == BAD_WHEEL_MEMBER:
                    continue
                target.writestr(info, source.read(info.filename))


def _verify_bad_wheel(
    *,
    wheel_path: Path,
    source_root: Path,
    outside_root: Path,
    temp_root: Path,
) -> None:
    """在无系统包污染的仓库外环境确认缺包会被真实发现。"""

    bad_wheel_dir = temp_root / "bad-wheel"
    bad_wheel_dir.mkdir()
    bad_wheel = bad_wheel_dir / wheel_path.name
    bad_venv = temp_root / "bad-venv"
    _make_bad_wheel(wheel_path, bad_wheel)
    venv.EnvBuilder(with_pip=True, system_site_packages=False).create(bad_venv)
    python = _venv_executable(bad_venv, "python")
    runtime_env = _runtime_environment(temp_root / "bad-user-data")
    expected_source = repr(str(source_root.resolve()))
    script = f"""
import importlib
import json
from pathlib import Path

import autoslice

origin = Path(autoslice.__file__).resolve()
source_root = Path({expected_source})
if origin == source_root or source_root in origin.parents:
    raise SystemExit(f"源码树污染了坏 wheel 验证：{{origin}}")
try:
    importlib.import_module("autoslice.analysis.topic.response")
except ModuleNotFoundError as exc:
    payload = {{"missing": exc.name}}
    print(json.dumps(payload, ensure_ascii=False))
    raise SystemExit(0 if exc.name == "autoslice.analysis.topic.response" else 1)
except Exception as exc:
    print(json.dumps({{"unexpected": type(exc).__name__}}, ensure_ascii=False))
    raise SystemExit(1)
else:
    raise SystemExit("坏 wheel 未暴露预期的缺包失败")
"""
    _run_checked(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--force-reinstall",
            str(bad_wheel),
        ],
        cwd=outside_root,
        env=_direct_build_environment(),
    )
    _run_checked(
        [str(python), "-B", "-c", script],
        cwd=outside_root,
        env=runtime_env,
    )


def _verify_installed_runtime(
    *,
    python: Path,
    source_root: Path,
    venv_root: Path,
    outside_root: Path,
    data_root: Path,
) -> None:
    runtime_env = _runtime_environment(data_root)
    origins = _installed_origins(python, cwd=outside_root, env=runtime_env)
    autoslice_root, cover_root = _assert_installed_from_venv(
        origins,
        venv_root=venv_root,
        source_root=source_root,
    )
    before = {
        "autoslice": _package_tree(autoslice_root),
        "autoslice_cover": _package_tree(cover_root),
    }
    commands = (
        [str(python), "-B", str(source_root / "scripts" / "smoke_packaging.py")],
        [str(_venv_executable(venv_root, "autoslice")), "--help"],
        [str(_venv_executable(venv_root, "autoslice-cover")), "--help"],
    )
    for command in commands:
        _run_checked(command, cwd=outside_root, env=runtime_env)
    after = {
        "autoslice": _package_tree(autoslice_root),
        "autoslice_cover": _package_tree(cover_root),
    }
    if after != before:
        raise RuntimeError("安装包在帮助或资源冒烟期间回写了 site-packages")


def _verify_distribution(temp_root: Path) -> None:
    source_root = temp_root / "source"
    wheel_dir = temp_root / "wheel"
    venv_root = temp_root / "venv"
    outside_root = temp_root / "outside-repo"
    data_root = temp_root / "user-data"
    for directory in (source_root, wheel_dir, outside_root, data_root):
        directory.mkdir()
    _copy_release(source_root)
    python, wheel_path = _build_and_install_wheel(
        source_root=source_root,
        wheel_dir=wheel_dir,
        venv_root=venv_root,
        outside_root=outside_root,
    )
    _verify_installed_runtime(
        python=python,
        source_root=source_root,
        venv_root=venv_root,
        outside_root=outside_root,
        data_root=data_root,
    )
    _verify_bad_wheel(
        wheel_path=wheel_path,
        source_root=source_root,
        outside_root=outside_root,
        temp_root=temp_root,
    )


def main() -> int:
    if sys.version_info[:2] != (3, 10):
        print(
            f"wheel 验收要求 Python 3.10，当前为 {sys.version.split()[0]}",
            file=sys.stderr,
        )
        return 1

    try:
        with tempfile.TemporaryDirectory(prefix="autoslice-wheel-") as temporary:
            _verify_distribution(Path(temporary))
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print(f"wheel 安装验收失败：{exc}", file=sys.stderr)
        return 1

    print("wheel 安装验收通过：仓库外 CLI、包资源和只读安装边界均正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
