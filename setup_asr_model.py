"""一次性安装 AutoSlice 推荐的免费开源中文语音识别模型。"""

import os
from pathlib import Path
import time


RECOMMENDED_MODELS = (
    "FunAudioLLM/Fun-ASR-Nano-2512",
    "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
)


def _download_model(model_id, snapshot_loader, attempts=3):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            path = Path(snapshot_loader(model_id)).resolve()
            if not (path / "model.pt").is_file():
                raise RuntimeError(f"模型缓存不完整: {path}")
            return path
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                wait_seconds = 3 * attempt
                print(f"  下载失败，{wait_seconds}s 后重试 ({attempt}/{attempts}): {exc}")
                time.sleep(wait_seconds)
    raise RuntimeError(f"模型 {model_id} 下载失败: {last_error}") from last_error


def install_recommended_models(snapshot_loader=None):
    if snapshot_loader is None:
        try:
            from modelscope import snapshot_download
        except ImportError as exc:
            raise RuntimeError("缺少 modelscope，请先运行 python -m pip install -r requirements.txt") from exc
        snapshot_loader = snapshot_download

    # 启动器日常运行保持离线；安装脚本被明确执行时才临时允许联网下载。
    os.environ.pop("MODELSCOPE_LOCAL_ONLY", None)
    # Windows 当前系统代理可能指向已失效的本地端口；安装脚本才临时直连。
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ[key] = ""
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    installed = []
    for index, model_id in enumerate(RECOMMENDED_MODELS, 1):
        print(f"[{index}/{len(RECOMMENDED_MODELS)}] 检查/下载 {model_id}")
        path = _download_model(model_id, snapshot_loader)
        installed.append(path)
        print(f"  已就绪: {path}")
    return installed


def main():
    print("=" * 62)
    print("  AutoSlice 高精度中文语音模型安装")
    print("  Fun-ASR-Nano + VAD + 标点（免费开源）")
    print("=" * 62)
    try:
        paths = install_recommended_models()
    except (OSError, RuntimeError) as exc:
        print(f"\n安装失败: {exc}")
        return 1
    print("\n安装完成。以后直接运行 python 启动.py，AutoSlice 会自动优先使用高精度模型。")
    print(f"共检查 {len(paths)} 个模型。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
