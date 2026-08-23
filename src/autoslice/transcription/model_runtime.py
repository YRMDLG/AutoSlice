"""FunASR 模型发现、设备选择、加载与生成参数的唯一实现。"""

from __future__ import annotations

import gc
import os
import re

from autoslice.streamer_profiles import current_streamer_profile
from autoslice.transcription import background_filter
from autoslice.transcription.contracts import DEFAULT_SUBTITLE_GLOSSARY

FACADE_EXPORTS = {
    "FUNASR_BATCH_SIZE_SEC": "FUNASR_BATCH_SIZE_SEC",
    "FUNASR_CACHE_MODEL_DIR": "FUNASR_CACHE_MODEL_DIR",
    "FUNASR_CONTEXTUAL_CACHE_MODEL_DIR": "FUNASR_CONTEXTUAL_CACHE_MODEL_DIR",
    "FUNASR_CONTEXTUAL_CACHE_MODEL_DIR_IIC": (
        "FUNASR_CONTEXTUAL_CACHE_MODEL_DIR_IIC"
    ),
    "FUNASR_CONTEXTUAL_MODEL": "FUNASR_CONTEXTUAL_MODEL",
    "FUNASR_CPU_RETRY_DELAY_SEC": "FUNASR_CPU_RETRY_DELAY_SEC",
    "FUNASR_DEFAULT_DEVICE": "FUNASR_DEFAULT_DEVICE",
    "FUNASR_FOREGROUND_AUDIO_FILTER": "FUNASR_FOREGROUND_AUDIO_FILTER",
    "FUNASR_HOTWORD_MAX_CHARS": "FUNASR_HOTWORD_MAX_CHARS",
    "FUNASR_HOTWORD_MAX_COUNT": "FUNASR_HOTWORD_MAX_COUNT",
    "FUNASR_MODEL": "FUNASR_MODEL",
    "FUNASR_NANO_CACHE_ROOTS": "FUNASR_NANO_CACHE_ROOTS",
    "FUNASR_NANO_MODEL": "FUNASR_NANO_MODEL",
    "FUNASR_PUNC_CACHE_MODEL_DIR": "FUNASR_PUNC_CACHE_MODEL_DIR",
    "FUNASR_PUNC_MODEL": "FUNASR_PUNC_MODEL",
    "FUNASR_SPK_CACHE_MODEL_DIR": "FUNASR_SPK_CACHE_MODEL_DIR",
    "FUNASR_SPK_MODEL": "FUNASR_SPK_MODEL",
    "FUNASR_SPK_WEIGHT_FILES": "FUNASR_SPK_WEIGHT_FILES",
    "FUNASR_VAD_CACHE_MODEL_DIR": "FUNASR_VAD_CACHE_MODEL_DIR",
    "FUNASR_VAD_MODEL": "FUNASR_VAD_MODEL",
    "_prepare_funasr_environment": "prepare_funasr_environment",
    "_funasr_model_cache_candidates": "funasr_model_cache_candidates",
    "_funasr_nano_cache_candidates": "funasr_nano_cache_candidates",
    "_resolve_funasr_model_source": "resolve_funasr_model_source",
    "_resolve_funasr_aux_model_source": "resolve_funasr_aux_model_source",
    "_resolve_funasr_speaker_model_source": (
        "resolve_funasr_speaker_model_source"
    ),
    "_funasr_hotwords": "funasr_hotwords",
    "_funasr_generate_kwargs": "funasr_generate_kwargs",
    "_resolve_funasr_device": "resolve_funasr_device",
    "funasr_public_status": "funasr_public_status",
    "_load_funasr_model": "load_funasr_model",
    "_clear_funasr_cuda_cache": "clear_funasr_cuda_cache",
}

FUNASR_MODEL = (
    os.environ.get("AUTOSLICE_FUNASR_MODEL", "").strip()
    or "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
)
FUNASR_CONTEXTUAL_MODEL = (
    os.environ.get("AUTOSLICE_FUNASR_CONTEXTUAL_MODEL", "").strip()
    or "damo/speech_paraformer-large-contextual_asr_nat-zh-cn-16k-common-vocab8404"
)
FUNASR_VAD_MODEL = (
    os.environ.get("AUTOSLICE_FUNASR_VAD_MODEL", "").strip()
    or "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
)
FUNASR_PUNC_MODEL = (
    os.environ.get("AUTOSLICE_FUNASR_PUNC_MODEL", "").strip()
    or "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch"
)
FUNASR_SPK_MODEL = (
    os.environ.get("AUTOSLICE_FUNASR_SPK_MODEL", "").strip()
    or "iic/speech_campplus_sv_zh-cn_16k-common"
)
FUNASR_DEFAULT_DEVICE = os.environ.get("AUTOSLICE_FUNASR_DEVICE", "auto")
FUNASR_BATCH_SIZE_SEC = 60
FUNASR_CPU_RETRY_DELAY_SEC = 1
FUNASR_CACHE_MODEL_DIR = os.path.expanduser(
    r"~\.cache\modelscope\hub\models\iic\speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
)
FUNASR_CONTEXTUAL_CACHE_MODEL_DIR = os.path.expanduser(
    r"~\.cache\modelscope\hub\models\damo\speech_paraformer-large-contextual_asr_nat-zh-cn-16k-common-vocab8404"
)
FUNASR_CONTEXTUAL_CACHE_MODEL_DIR_IIC = os.path.expanduser(
    r"~\.cache\modelscope\hub\models\iic\speech_paraformer-large-contextual_asr_nat-zh-cn-16k-common-vocab8404"
)
FUNASR_NANO_MODEL = (
    os.environ.get("AUTOSLICE_FUNASR_NANO_MODEL", "").strip()
    or "FunAudioLLM/Fun-ASR-Nano-2512"
)
FUNASR_NANO_CACHE_ROOTS = (
    os.path.expanduser(
        r"~\.cache\modelscope\hub\models\FunAudioLLM\Fun-ASR-Nano-2512"
    ),
    os.path.expanduser(
        r"~\.cache\huggingface\hub\models--FunAudioLLM--Fun-ASR-Nano-2512\snapshots"
    ),
)
FUNASR_VAD_CACHE_MODEL_DIR = os.path.expanduser(
    r"~\.cache\modelscope\hub\models\iic\speech_fsmn_vad_zh-cn-16k-common-pytorch"
)
FUNASR_PUNC_CACHE_MODEL_DIR = os.path.expanduser(
    r"~\.cache\modelscope\hub\models\iic\punc_ct-transformer_zh-cn-common-vocab272727-pytorch"
)
FUNASR_SPK_CACHE_MODEL_DIR = os.path.expanduser(
    os.path.join(
        r"~\.cache\modelscope\hub\models",
        *FUNASR_SPK_MODEL.split("/"),
    )
)
FUNASR_SPK_WEIGHT_FILES = ("campplus_cn_common.bin", "model.pt")
FUNASR_FOREGROUND_AUDIO_FILTER = (
    "highpass=f=80,lowpass=f=12000,"
    "afftdn=nf=-32,"
    "agate=threshold=0.012:ratio=6:attack=8:release=220"
)
FUNASR_HOTWORD_MAX_COUNT = 80
FUNASR_HOTWORD_MAX_CHARS = 2400


def prepare_funasr_environment():
    """只使用本地缓存，避免 Web 任务在网络下载失败时长时间卡住。"""
    os.environ.setdefault("MODELSCOPE_LOCAL_ONLY", "1")


def funasr_model_cache_candidates():
    configured = os.environ.get("AUTOSLICE_FUNASR_MODEL_DIR", "").strip()
    candidates = []
    if configured:
        candidates.append(os.path.abspath(os.path.expanduser(configured)))
    candidates.extend(funasr_nano_cache_candidates())
    candidates.extend((
        FUNASR_CONTEXTUAL_CACHE_MODEL_DIR,
        FUNASR_CONTEXTUAL_CACHE_MODEL_DIR_IIC,
        FUNASR_CACHE_MODEL_DIR,
    ))
    return list(dict.fromkeys(candidates))


def funasr_nano_cache_candidates():
    candidates = []
    for root in FUNASR_NANO_CACHE_ROOTS:
        if os.path.isfile(os.path.join(root, "model.pt")):
            candidates.append(root)
        elif os.path.isdir(root):
            candidates.extend(
                os.path.join(root, name)
                for name in sorted(os.listdir(root), reverse=True)
                if os.path.isfile(os.path.join(root, name, "model.pt"))
            )
    return candidates


def resolve_funasr_model_source():
    """优先返回本地缓存目录，避免 AutoModel 用模型 ID 访问 ModelScope API。"""
    for model_dir in funasr_model_cache_candidates():
        if model_dir and os.path.isfile(os.path.join(model_dir, "model.pt")):
            return model_dir
    return FUNASR_MODEL


def resolve_funasr_aux_model_source(model_id, cache_dir):
    """只返回完整的本地辅助模型，避免启动时因网络问题隐式下载。"""
    configured = os.environ.get(
        (
            "AUTOSLICE_FUNASR_VAD_DIR"
            if model_id == FUNASR_VAD_MODEL
            else "AUTOSLICE_FUNASR_PUNC_DIR"
        ),
        "",
    ).strip()
    candidates = []
    if configured:
        candidates.append(os.path.abspath(os.path.expanduser(configured)))
    candidates.append(cache_dir)
    for model_dir in dict.fromkeys(candidates):
        if model_dir and os.path.isfile(os.path.join(model_dir, "model.pt")):
            return model_dir
    return None


def resolve_funasr_speaker_model_source():
    """只启用已完整下载到本机的 CAM++，日常识别绝不隐式联网。"""
    configured = os.environ.get("AUTOSLICE_FUNASR_SPK_DIR", "").strip()
    candidates = []
    if configured:
        candidates.append(os.path.abspath(os.path.expanduser(configured)))
    candidates.append(FUNASR_SPK_CACHE_MODEL_DIR)
    for model_dir in dict.fromkeys(candidates):
        if model_dir and any(
                os.path.isfile(os.path.join(model_dir, filename))
                for filename in FUNASR_SPK_WEIGHT_FILES):
            return model_dir
    return None


def funasr_speaker_model_ready():
    """CAM++ 与其说话人结果所需标点模型都完整时才视为可用。"""

    return bool(
        resolve_funasr_speaker_model_source()
        and resolve_funasr_aux_model_source(
            FUNASR_PUNC_MODEL,
            FUNASR_PUNC_CACHE_MODEL_DIR,
        )
    )


def funasr_hotwords(video_path=None, streamer_name=""):
    """构造受限的 ASR 热词串；普通 Paraformer 会忽略它。"""
    del video_path
    values = []
    configured = os.environ.get("AUTOSLICE_FUNASR_HOTWORDS", "")
    values.extend(re.split(r"[,，、;；\n\r\t ]+", configured))
    values.extend(DEFAULT_SUBTITLE_GLOSSARY)
    profile = current_streamer_profile()
    values.extend(profile.subtitle_glossary)
    values.extend((
        streamer_name,
        profile.canonical_name,
        profile.report_name,
        *profile.aliases,
    ))
    values.extend(source for source, _ in profile.asr_replacements)
    values.extend(target for _, target in profile.asr_replacements)
    result = []
    seen = set()
    for value in values:
        value = str(value or "").strip()
        if not value or len(value) < 2 or len(value) > 40:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
        if len(result) >= FUNASR_HOTWORD_MAX_COUNT:
            break
    return " ".join(result)[:FUNASR_HOTWORD_MAX_CHARS]


def funasr_generate_kwargs(model, hotwords=""):
    kwargs = {
        "batch_size_s": FUNASR_BATCH_SIZE_SEC,
        "disable_pbar": True,
        "return_raw_text": True,
    }
    model_class = type(getattr(model, "model", model)).__name__.casefold()
    if "funasrnano" in model_class:
        kwargs.update({
            "hotwords": str(hotwords or "").split(),
            "language": "中文",
            "itn": True,
            "max_length": 1024,
        })
        kwargs.pop("return_raw_text", None)
        if getattr(model, "_autoslice_spk_source", None):
            kwargs["return_spk_res"] = True
        return kwargs
    if hotwords and "contextual" in model_class:
        kwargs["hotword"] = hotwords
    if getattr(model, "_autoslice_spk_source", None):
        kwargs["return_spk_res"] = True
    return kwargs


def resolve_funasr_device(requested_device=None):
    """优先使用可用的 CUDA；显卡运行时缺失时保持 CPU 路径。"""
    requested = str(
        requested_device
        or os.environ.get("AUTOSLICE_FUNASR_DEVICE", FUNASR_DEFAULT_DEVICE)
        or "auto"
    ).strip().lower()
    if requested == "cuda":
        return "cuda:0"
    if requested not in {"", "auto"}:
        return requested
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda:0"
    except (ImportError, OSError, RuntimeError):
        pass
    return "cpu"


def funasr_public_status():
    """返回不含本机模型路径的 FunASR 状态和可执行调整提示。"""
    model_source = resolve_funasr_model_source()
    source_text = str(model_source or "")
    source_key = source_text.replace("\\", "/").casefold()
    is_nano = "fun-asr-nano" in source_key
    is_contextual = "contextual" in source_key
    is_local_model = os.path.isfile(os.path.join(source_text, "model.pt"))
    selected_device = resolve_funasr_device()
    custom_hotwords = bool(
        str(os.environ.get("AUTOSLICE_FUNASR_HOTWORDS", "")).strip()
    )
    speaker_filter_ready = funasr_speaker_model_ready()

    if is_nano:
        model_key = "nano"
        display_name = "Fun-ASR-Nano-2512"
        hotword_mode = "native"
    elif is_contextual:
        model_key = "contextual_paraformer"
        display_name = "Contextual Paraformer"
        hotword_mode = "native"
    else:
        model_key = "paraformer"
        display_name = "Paraformer（兼容回退）"
        hotword_mode = "post_correction_only"

    needs_setup = not is_nano
    model_ready = is_local_model
    if not model_ready:
        summary = f"未检测到可用的本地 {display_name} 模型缓存"
        recommendation = "关闭服务后运行 python setup_asr_model.py，再重新启动。"
    elif needs_setup:
        summary = f"当前使用 {display_name}，可以识别，但不是推荐模型"
        recommendation = "建议关闭服务后运行 python setup_asr_model.py，安装推荐 Nano 模型。"
    else:
        summary = f"当前使用 {display_name}（推荐）"
        recommendation = "识别专名不准时，调整热词或主播专名纠错规则。"

    if hotword_mode == "native":
        hotword_hint = (
            "已读取自定义热词；长期错字仍建议写入主播专名纠错规则。"
            if custom_hotwords
            else "可在 autoslice.local.json 设置 AUTOSLICE_FUNASR_HOTWORDS 追加临时热词。"
        )
    else:
        hotword_hint = (
            "普通 Paraformer 不直接接收热词；当前只应用识别后的主播专名纠错规则。"
        )

    return {
        "model_key": model_key,
        "display_name": display_name,
        "device": selected_device,
        "model_ready": model_ready,
        "recommended": is_nano and model_ready,
        "needs_setup": needs_setup or not model_ready,
        "hotword_mode": hotword_mode,
        "custom_hotwords": custom_hotwords,
        "summary": summary,
        "recommendation": recommendation,
        "hotword_hint": hotword_hint,
        "correction_hint": (
            "长期固定纠错：编辑 streamer_profiles.json 中对应主播的 asr_replacements。"
        ),
        "foreground_filter_available": True,
        "speaker_filter_ready": speaker_filter_ready,
        "background_filter_modes": (
            background_filter.public_background_filter_modes()
        ),
        "subtitle_background_filter_default": (
            background_filter.BACKGROUND_FILTER_SOFT
        ),
        "analysis_background_filter_default": (
            background_filter.BACKGROUND_FILTER_OFF
        ),
        "background_filter_limit": (
            "单一混合音轨无法保证 100% 分离同时且等音量的人声；"
            "严格过滤可能吞掉有效人声。"
        ),
        "foreground_filter_mode": (
            background_filter.technical_filter_mode(
                background_filter.BACKGROUND_FILTER_SOFT,
                speaker_model_used=speaker_filter_ready,
            )
        ),
        "foreground_filter_hint": (
            "CAM++ 已就绪：软过滤只统计并保留候选，严格过滤才删除非主要说话人。"
            if speaker_filter_ready
            else "软/严格模式可使用基础背景音门限；重新运行 python setup_asr_model.py "
                 "可安装 CAM++。缺少 CAM++ 时不会声称已经区分说话人。"
        ),
    }


def _annotate_model_runtime(
        model, *, device, model_source, vad_source, punc_source,
        speaker_source, background_filter_mode,
        speaker_model_load_failed=False):
    policy = background_filter.background_filter_policy(background_filter_mode)
    try:
        model._autoslice_device = device
        model._autoslice_model_source = model_source
        model._autoslice_vad_source = vad_source
        model._autoslice_punc_source = punc_source
        model._autoslice_spk_source = speaker_source
        model._autoslice_speaker_model_load_failed = bool(
            speaker_model_load_failed
        )
        model._autoslice_background_filter_mode = policy.mode
        model._autoslice_foreground_filter = (
            background_filter.technical_filter_mode(
                policy.mode,
                speaker_model_used=bool(speaker_source),
            )
        )
    except (AttributeError, TypeError):
        pass
    return model


def _funasr_load_error_message(exc, *, model_source, selected_device):
    """把常见的 ASR 依赖缺失转换成可直接执行的排查提示。"""
    error_text = str(exc).casefold()
    missing_torch = isinstance(exc, ModuleNotFoundError) and (
        getattr(exc, "name", "") == "torch"
    )
    missing_torch = missing_torch or (
        "no module named 'torch'" in error_text
        or 'no module named "torch"' in error_text
    )
    if missing_torch:
        if str(selected_device).casefold().startswith("cuda"):
            install_hint = (
                '请先运行 python setup_gpu_runtime.py 安装隔离 CUDA 运行时，'
                "或关闭 GPU 后执行：python -m pip install -e \".[asr-cpu]\"。"
            )
        else:
            install_hint = (
                '请执行：python -m pip install -e ".[asr-cpu]"，'
                "然后重新启动 AutoSlice。"
            )
        return f"FunASR 缺少 PyTorch 运行依赖。{install_hint}"
    return (
        "FunASR 模型加载失败：本地 ModelScope 缓存不可用，或模型下载被网络/SSL 中断。"
        "请先生成同名 SRT，或在网络正常时预下载 FunASR 模型后重试。"
    )


def load_funasr_model(
        AutoModel, progress_callback=None, device=None, foreground_only=None,
        background_filter_mode=None):
    """加载 FunASR 模型；本地无缓存时抛出带排查提示的异常。"""
    policy = background_filter.background_filter_policy(
        background_filter_mode,
        foreground_only=foreground_only,
    )
    prepare_funasr_environment()
    selected_device = resolve_funasr_device(device)
    model_source = resolve_funasr_model_source()
    is_nano = "fun-asr-nano" in str(model_source).casefold()
    vad_source = resolve_funasr_aux_model_source(
        FUNASR_VAD_MODEL,
        FUNASR_VAD_CACHE_MODEL_DIR,
    )
    speaker_source = (
        resolve_funasr_speaker_model_source()
        if policy.request_speaker_model
        else None
    )
    punc_source = None if is_nano and not speaker_source else (
        resolve_funasr_aux_model_source(
            FUNASR_PUNC_MODEL,
            FUNASR_PUNC_CACHE_MODEL_DIR,
        )
    )
    if speaker_source and not punc_source:
        speaker_source = None
    model_kwargs = {
        "model": model_source,
        "device": selected_device,
        "disable_update": True,
        "disable_pbar": True,
    }
    if is_nano:
        model_kwargs["trust_remote_code"] = True
    if vad_source:
        model_kwargs["vad_model"] = vad_source
        model_kwargs["vad_kwargs"] = {
            "max_single_segment_time": 30000 if is_nano else 60000
        }
    if punc_source:
        model_kwargs["punc_model"] = punc_source
    if speaker_source:
        model_kwargs.update({
            "spk_model": speaker_source,
            "spk_mode": "vad_segment",
        })

    def annotate(
            model, *, actual_device, actual_speaker_source,
            speaker_model_load_failed=False):
        return _annotate_model_runtime(
            model,
            device=actual_device,
            model_source=model_source,
            vad_source=vad_source,
            punc_source=punc_source,
            speaker_source=actual_speaker_source,
            background_filter_mode=policy.mode,
            speaker_model_load_failed=speaker_model_load_failed,
        )

    load_error = None
    if speaker_source:
        try:
            model = AutoModel(**model_kwargs)
            return annotate(
                model,
                actual_device=selected_device,
                actual_speaker_source=speaker_source,
            )
        except Exception as exc:
            load_error = exc
            if selected_device.startswith("cuda"):
                if progress_callback:
                    progress_callback(
                        "FunASR GPU + CAM++ 加载失败，自动改用 CPU + CAM++ 重试",
                        10,
                        100,
                    )
                try:
                    cpu_kwargs = dict(model_kwargs)
                    cpu_kwargs["device"] = "cpu"
                    model = AutoModel(**cpu_kwargs)
                    return annotate(
                        model,
                        actual_device="cpu",
                        actual_speaker_source=speaker_source,
                    )
                except Exception as cpu_exc:
                    load_error = cpu_exc

            base_kwargs = dict(model_kwargs)
            base_kwargs["device"] = "cpu"
            base_kwargs.pop("spk_model", None)
            base_kwargs.pop("spk_mode", None)
            try:
                model = AutoModel(**base_kwargs)
            except Exception as base_exc:
                load_error = base_exc
            else:
                if progress_callback:
                    progress_callback(
                        "CAM++ 文件已检测到但加载失败，已回退到 CPU 基础 ASR；"
                        "未启用说话人区分",
                        10,
                        100,
                    )
                return annotate(
                    model,
                    actual_device="cpu",
                    actual_speaker_source=None,
                    speaker_model_load_failed=True,
                )
    else:
        try:
            model = AutoModel(**model_kwargs)
            return annotate(
                model,
                actual_device=selected_device,
                actual_speaker_source=None,
            )
        except Exception as exc:
            load_error = exc
            if selected_device.startswith("cuda"):
                if progress_callback:
                    progress_callback(
                        f"FunASR GPU 加载失败，自动改用 CPU: {exc}",
                        10,
                        100,
                    )
                try:
                    cpu_kwargs = dict(model_kwargs)
                    cpu_kwargs["device"] = "cpu"
                    model = AutoModel(**cpu_kwargs)
                    return annotate(
                        model,
                        actual_device="cpu",
                        actual_speaker_source=None,
                    )
                except Exception as cpu_exc:
                    load_error = cpu_exc

    failure_device = "cpu" if speaker_source else selected_device
    message = _funasr_load_error_message(
        load_error,
        model_source=model_source,
        selected_device=failure_device,
    )
    if progress_callback:
        progress_callback(f"{message} 原始错误: {load_error}", 0, 100)
    raise RuntimeError(message) from load_error


def clear_funasr_cuda_cache():
    try:
        import torch
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except (ImportError, OSError, RuntimeError):
        pass
