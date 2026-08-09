"""从待发布文件构造干净副本，并验证配置、帮助和一键启动。"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PORTS = (5002, 5010)


def _release_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    names = result.stdout.decode("utf-8").split("\0")
    return [ROOT / name for name in names if name and (ROOT / name).is_file()]


def _copy_release(destination: Path) -> None:
    for source in _release_files():
        target = destination / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _read_json(url: str, timeout: float = 0.8) -> dict[str, object] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return payload if isinstance(payload, dict) else None


def _wait_for_services(process: subprocess.Popen[str], timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("一键启动进程提前退出")
        autoslice = _read_json("http://127.0.0.1:5002/api/service")
        autocover = _read_json("http://127.0.0.1:5010/api/options")
        if (
            autoslice
            and autoslice.get("service") == "autoslice"
            and autoslice.get("api_version") == 1
            and autocover
            and autocover.get("service") == "autocover"
            and autocover.get("api_version") == 5
        ):
            return
        time.sleep(0.2)
    raise RuntimeError("等待 AutoSlice/AutoCover 服务就绪超时")


def _stop_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        process.wait(timeout=10)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _run_checked(command: list[str], cwd: Path) -> None:
    result = subprocess.run(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stdout[-2000:] or f"命令返回 {result.returncode}")


def main() -> int:
    occupied = [port for port in PORTS if not _port_is_free(port)]
    if occupied:
        print(f"启动冒烟测试失败：端口被占用：{occupied}", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="autoslice-public-") as temporary:
        clean_root = Path(temporary) / "AutoSlice"
        clean_root.mkdir()
        _copy_release(clean_root)
        config = json.loads((clean_root / "api_config.example.json").read_text(encoding="utf-8"))
        if set(config) != {"base_url", "token", "model", "api_type"}:
            print("启动冒烟测试失败：API 示例字段不完整", file=sys.stderr)
            return 1
        if config["token"] != "YOUR_API_TOKEN":
            print("启动冒烟测试失败：API 示例不是占位 token", file=sys.stderr)
            return 1

        _run_checked([sys.executable, "-B", "scripts/validate_public_docs.py"], clean_root)
        _run_checked([sys.executable, "-B", "scripts/compile_public.py"], clean_root)
        _run_checked(
            [sys.executable, "-B", "-m", "autocover_tool.autocover.cli", "--help"],
            clean_root,
        )

        env = os.environ.copy()
        for name in tuple(env):
            if name.startswith("AUTOSLICE_API_") or name in {
                "AUTOSLICE_AUTOCOVER_DIR",
                "AUTOSLICE_VIDEO_DIR",
                "AUTOSLICE_OUTPUT_DIR",
                "AUTOSLICE_TIMELINE_DIR",
                "AUTOSLICE_SUBMISSION_DIR",
                "AUTOCOVER_INPUT_DIR",
                "AUTOCOVER_OUTPUT_DIR",
                "AUTOCOVER_STICKER_DIR",
                "AUTOCOVER_FONT_PATH",
                "AUTOCOVER_URL",
            }:
                env.pop(name, None)
        env.update({
            "PYTHONUTF8": "1",
            "AUTOSLICE_DISABLE_GPU": "1",
            "AUTOSLICE_FUNASR_DEVICE": "cpu",
            "MODELSCOPE_LOCAL_ONLY": "1",
        })
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            [sys.executable, "-B", "启动.py"],
            cwd=clean_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_flags,
        )
        output = ""
        try:
            _wait_for_services(process)
        except Exception as exc:
            _stop_process_tree(process)
            if process.stdout is not None:
                output = process.stdout.read()
            print(f"启动冒烟测试失败：{exc}\n{output[-3000:]}", file=sys.stderr)
            return 1
        finally:
            _stop_process_tree(process)
        if process.stdout is not None:
            output = process.stdout.read()
        if "AutoSlice Web 已启动" not in output or "AutoCover" not in output:
            print(f"启动冒烟测试失败：启动日志不完整\n{output[-3000:]}", file=sys.stderr)
            return 1
    print("干净副本验证通过：配置、文档、CLI、AutoSlice 和 AutoCover 均可启动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
