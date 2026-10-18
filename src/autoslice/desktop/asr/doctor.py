"""识别环境自检：缺什么、装哪里、显卡够不够，一次说清。用在跑识别的那个 Python 里。"""

from __future__ import annotations

import importlib
import importlib.metadata
import shutil
import sys
import tempfile
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

from .engines import ENGINES, create_engine
from .settings import AsrSettings

# Qwen3-ASR 1.7B + 对齐模型 + 解码缓存，8GB 显卡够用的参考值（MB）
VRAM_NEEDED_MB = {"qwen3-asr": 6500, "funasr": 3000}


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # ok / warn / fail
    detail: str
    hint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return ""


def check_transformers(installed: str, declarations: list[str] | None) -> Check:
    """按已安装 qwen-asr 的依赖元数据检查兼容性，不混用原生 HF 加载要求。"""

    if not installed:
        return Check("transformers", "fail", "没装", "在独立识别环境中安装 qwen-asr，让它安装匹配的依赖")
    try:
        from packaging.requirements import InvalidRequirement, Requirement
        from packaging.version import InvalidVersion
    except ImportError:
        return Check("transformers", "warn", installed, "缺少 packaging，未能验证依赖兼容性；请在识别环境中检查")
    try:
        requirements = [Requirement(line) for line in declarations or ()]
        wanted = [item for item in requirements
                  if item.name.casefold().replace("_", "-") == "transformers"
                  and (item.marker is None or item.marker.evaluate({"extra": ""}))]
        if not wanted:
            return Check("transformers", "warn", installed, "没有找到 qwen-asr 的有效依赖声明，兼容性未验证")
        for item in wanted:
            if not item.specifier.contains(installed, prereleases=True):
                return Check("transformers", "warn", installed,
                             f"与 qwen-asr 声明的 {item} 不一致；请在独立识别环境中重新安装当前版本的 qwen-asr，不要单独升级 transformers")
    except (InvalidRequirement, InvalidVersion, ValueError):
        return Check("transformers", "warn", installed, "依赖元数据或版本无法解析，兼容性未验证")
    return Check("transformers", "ok", installed)


def qwen_requirements() -> list[str] | None:
    """读取已安装识别包的依赖声明；不联网、不导入模型。"""

    try:
        return importlib.metadata.requires("qwen-asr")
    except importlib.metadata.PackageNotFoundError:
        return None


def _model_cached(model: str) -> str:
    """模型是不是已经在本机：本地目录，或 HF / ModelScope 缓存里。返回位置，没有返回空串。"""

    local = Path(model).expanduser()
    if local.is_dir():
        return str(local)
    if "/" not in model:
        return ""
    org, name = model.split("/", 1)
    candidates = [
        Path.home() / ".cache" / "huggingface" / "hub" / f"models--{org}--{name}",
        Path.home() / ".cache" / "modelscope" / "hub" / "models" / org / name,
        Path.home() / ".cache" / "modelscope" / "hub" / org / name,
    ]
    return next((str(path) for path in candidates if path.is_dir()), "")


def run_checks(settings: AsrSettings, *, storage_root: Path | None = None) -> list[Check]:
    checks = [Check("Python", "ok", f"{sys.version.split()[0]}  {sys.executable}")]
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        checks.append(Check(tool, "ok", path) if path else
                      Check(tool, "fail", "找不到", "安装 ffmpeg 并把它所在目录加入 PATH"))
    spec = ENGINES.get(settings.engine)
    if spec is None:
        checks.append(Check("识别引擎", "fail", f"不认识：{settings.engine}", "在设置里重新选择"))
        return checks
    checks.append(Check("识别引擎", "ok", spec.label))

    torch = None
    try:
        torch = importlib.import_module("torch")
    except ImportError as exc:
        checks.append(Check("torch", "fail", str(exc)[:120],
                            "安装带 CUDA 的 PyTorch：https://pytorch.org/get-started/locally/"))
    if torch is not None:
        cuda = torch.cuda.is_available()
        checks.append(Check("torch", "ok", f"{torch.__version__}  CUDA {'可用' if cuda else '不可用'}"))
        if cuda:
            properties = torch.cuda.get_device_properties(0)
            total = properties.total_memory / (1024 * 1024)
            need = VRAM_NEEDED_MB.get(settings.engine, 0)
            status = "ok" if total >= need else "warn"
            checks.append(Check("显卡", status, f"{properties.name}  {total / 1024:.1f} GB",
                                f"显存需求约 {need / 1024:.1f} GB（估算），以加载试跑的峰值为准"))
            bf16 = torch.cuda.is_bf16_supported()
            checks.append(Check("bf16", "ok" if bf16 else "warn", "支持" if bf16 else "不支持",
                                "" if bf16 else "30 系以下显卡不支持 bf16，Qwen3-ASR 可能需要改用 float16"))
        else:
            checks.append(Check("显卡", "warn", "没有可用的 CUDA 显卡，会用 CPU，非常慢",
                                "确认装的是 CUDA 版 PyTorch，并更新显卡驱动"))

    for package in spec.packages:
        if package == "torch":
            continue
        pip_name = package.replace("_", "-")
        version = _version(pip_name) or _version(package)
        if version:
            checks.append(Check(pip_name, "ok", version))
        else:
            checks.append(Check(pip_name, "fail", "没装", f"pip install -U {pip_name}"))
    if settings.engine == "qwen3-asr":
        checks.append(check_transformers(_version("transformers"), qwen_requirements()))
        for label, model in (("识别模型", settings.model), ("对齐模型", settings.aligner)):
            if not model:
                if label == "对齐模型":
                    checks.append(Check(label, "warn", "没配置", "没有对齐模型，字幕每个字的时间只能按字数估算"))
                continue
            where = _model_cached(model)
            checks.append(Check(label, "ok", f"{model}  本机已有：{where}") if where else
                          Check(label, "warn", f"{model}  本机没找到缓存", "第一次运行会联网下载（几个 GB）；也可以手动下载后把设置里的模型改成本地文件夹"))
    root = storage_root or Path(tempfile.gettempdir())
    try:
        free = shutil.disk_usage(root if root.exists() else root.parent).free / (1024 ** 3)
        checks.append(Check("磁盘空间", "ok" if free >= 5 else "warn", f"可用 {free:.0f} GB",
                            "" if free >= 5 else "识别过程要在缓存里放整段音频，至少留 5 GB"))
    except OSError:
        pass
    return checks


def probe(settings: AsrSettings) -> list[Check]:
    """真的把引擎加载起来，对一小段静音跑一遍：验证模型能加载、接口对得上、显存占多少。"""

    checks = []
    engine = create_engine(settings)
    with tempfile.TemporaryDirectory() as directory:
        wav = Path(directory) / "silence.wav"
        with wave.open(str(wav), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b"\x00\x00" * 16000 * 3)
        try:
            info = engine.load(None, ("探针词",))
            checks.append(Check("加载引擎", "ok", "；".join(f"{key}：{value}" for key, value in info.items())))
            engine.recognize(str(wav))
            checks.append(Check("试跑 3 秒静音", "ok", "接口调用成功"))
            stats = getattr(engine, "stats", lambda: {})()
            if stats.get("peak_vram_mb"):
                checks.append(Check("显存峰值", "ok", f"{stats['peak_vram_mb'] / 1024:.2f} GB"))
        except Exception as exc:  # noqa: BLE001 - 探针就是要把任何失败原因报出来
            checks.append(Check("探针", "fail", f"{type(exc).__name__}: {str(exc)[:240]}"))
        finally:
            try:
                engine.close()
            except Exception:  # noqa: BLE001
                pass
    return checks


def format_checks(checks: list[Check]) -> str:
    marks = {"ok": "✓", "warn": "!", "fail": "✗"}
    lines = []
    for check in checks:
        lines.append(f"{marks[check.status]} {check.name}：{check.detail}")
        if check.hint:
            lines.append(f"    → {check.hint}")
    return "\n".join(lines)
