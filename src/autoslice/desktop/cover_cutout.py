"""人物抠图：返回和底图同尺寸的透明 PNG（人物 + 白色外发光），底图按同一位置把它盖回去。

两条路，按精细程度从高到低：
1. 本机有 BiRefNet（ONNX，放在数据目录 models/birefnet_lite.onnx）：用模型在人物区域里抠，发丝级边缘。
2. 没有模型：OpenCV 的 GrabCut 在人物区域里按原分辨率抠，补洞，再用彩色导向滤波把边缘贴到真实发丝上。
两条路都以看图标出的人物框、脸框为准，直播界面算背景；没标出人物时按画面中间估。不联网、不下载。
"""

from __future__ import annotations

import hashlib
import threading
from functools import lru_cache
from pathlib import Path

from PIL import Image

from .cover_ai import AIFrameNotes

# 模型文件名（放在数据目录 models/ 下）与输入边长、归一化参数（BiRefNet 用 ImageNet 均值方差）。
MODEL_FILE = "birefnet_lite.onnx"
_MODEL_SIDE = 1024
_MEAN, _STD = (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)
# 人物框往外扩多少当工作区（框外是背景样本，也给飘出去的头发留地方）。
_MARGIN_X, _MARGIN_Y = 0.22, 0.10
# GrabCut 工作区长边上限（只定大轮廓，细边交给导向滤波按原分辨率贴；再大很慢）、迭代次数；
# 导向滤波在缩小这么多倍的图上算系数（快），半径占工作区短边的比例。
_GRABCUT_SIDE = 1100
_GRABCUT_ITERATIONS = 5
_GUIDE_SUBSAMPLE = 4
_GUIDE_RADIUS = 0.006
_GUIDE_EPS = 1e-4
# 外发光宽度占画面高度的比例；没标出人物时的默认人物框。
_GLOW_RATIO = 0.012
_DEFAULT_PERSON = (0.22, 0.06, 0.78, 1.0)
# 抠完默认把人物以外压暗这么多。
CUTOUT_DIM = 0.45
_SESSION_LOCK = threading.Lock()


def cutout_available() -> bool:
    """本机有没有 OpenCV（两条路都要用它读图和找人）。"""

    try:
        import cv2  # noqa: F401
    except ImportError:
        return False
    return True


def model_ready(model: Path | None) -> bool:
    """有模型文件，且装了 onnxruntime。"""

    if model is None or not model.is_file():
        return False
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


@lru_cache(maxsize=2)
def model_session(path: str, _mtime: int):
    """加载（并缓存）模型；测试可以换掉它。"""

    import onnxruntime

    return onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"])


def _work_area(notes: AIFrameNotes | None, width: int, height: int) -> tuple[int, int, int, int]:
    person = notes.person if notes is not None and notes.person is not None else _DEFAULT_PERSON
    margin_x, margin_y = (person[2] - person[0]) * _MARGIN_X, (person[3] - person[1]) * _MARGIN_Y
    return (
        int(max(0.0, person[0] - margin_x) * width), int(max(0.0, person[1] - margin_y) * height),
        int(min(1.0, person[2] + margin_x) * width), int(min(1.0, person[3] + margin_y) * height),
    )


def _guided_filter(guide, source, radius: int, eps: float):
    """快速彩色导向滤波：系数在缩小的图上算，再放大回来乘原图；边缘贴住原图的颜色变化。"""

    import cv2
    import numpy as np

    height, width = source.shape
    step = _GUIDE_SUBSAMPLE
    small_size = (max(1, width // step), max(1, height // step))
    small_guide = cv2.resize(guide, small_size, interpolation=cv2.INTER_AREA)
    small_source = cv2.resize(source, small_size, interpolation=cv2.INTER_AREA)
    size = (2 * max(1, radius // step) + 1,) * 2

    def box(image):
        return cv2.boxFilter(image, cv2.CV_32F, size)

    mean_i = box(small_guide)
    mean_p = box(small_source)
    cov_ip = box(small_guide * small_source[..., None]) - mean_i * mean_p[..., None]
    var = np.empty(small_guide.shape[:2] + (3, 3), np.float32)
    for row in range(3):
        for col in range(row, 3):
            var[..., row, col] = var[..., col, row] = (
                box(small_guide[..., row] * small_guide[..., col]) - mean_i[..., row] * mean_i[..., col]
            )
    var += np.eye(3, dtype=np.float32) * eps
    a = np.linalg.solve(var, cov_ip[..., None])[..., 0]
    b = mean_p - (a * mean_i).sum(axis=2)
    a = cv2.resize(box(a), (width, height), interpolation=cv2.INTER_LINEAR)
    b = cv2.resize(box(b), (width, height), interpolation=cv2.INTER_LINEAR)
    return (a * guide).sum(axis=2) + b


def _keep_person(alpha, notes: AIFrameNotes | None, area: tuple[int, int, int, int], size: tuple[int, int]):
    """只留和人连着的那一块：去掉工作区里被当成前景的界面碎片；透明度的柔边保留。"""

    import cv2
    import numpy as np

    solid = (alpha > 0.5).astype(np.uint8)
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(solid)
    if count <= 2:
        return alpha
    keep = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if notes is not None and notes.face is not None:
        width, height = size
        center = ((notes.face[0] + notes.face[2]) / 2 * width - area[0], (notes.face[1] + notes.face[3]) / 2 * height - area[1])
        label = labels[min(labels.shape[0] - 1, max(0, int(center[1]))), min(labels.shape[1] - 1, max(0, int(center[0])))]
        keep = int(label) or keep
    # 主体那块往外扩一点，柔边和发丝不被一刀切掉。
    near = cv2.dilate((labels == keep).astype(np.uint8), np.ones((15, 15), np.uint8))
    return alpha * near


def _model_alpha(model: Path, crop):
    """BiRefNet 在工作区上推理，返回 0~1 的透明度。"""

    import cv2
    import numpy as np

    height, width = crop.shape[:2]
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    tensor = cv2.resize(rgb, (_MODEL_SIDE, _MODEL_SIDE), interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255
    tensor = ((tensor - np.array(_MEAN, np.float32)) / np.array(_STD, np.float32)).transpose(2, 0, 1)[None]
    session = model_session(str(model), model.stat().st_mtime_ns)
    with _SESSION_LOCK:
        output = session.run(None, {session.get_inputs()[0].name: tensor})[-1]
    mask = np.asarray(output, np.float32).reshape(output.shape[-2], output.shape[-1])
    if mask.min() < 0 or mask.max() > 1:
        mask = 1 / (1 + np.exp(-mask))  # 输出是 logits
    return np.clip(cv2.resize(mask, (width, height), interpolation=cv2.INTER_LINEAR), 0, 1)


def _grabcut_alpha(crop, notes: AIFrameNotes | None, area: tuple[int, int, int, int], size: tuple[int, int]):
    """GrabCut 在工作区里抠，补洞，再用导向滤波把边缘贴到发丝上；返回 0~1 的透明度。"""

    import cv2
    import numpy as np

    width, height = size
    crop_height, crop_width = crop.shape[:2]
    scale = min(1.0, _GRABCUT_SIDE / max(crop_height, crop_width))
    work = cv2.resize(crop, (int(crop_width * scale), int(crop_height * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else crop
    work_height, work_width = work.shape[:2]

    def local(box):
        return (
            max(0, int((box[0] * width - area[0]) * scale)), max(0, int((box[1] * height - area[1]) * scale)),
            max(0, int((box[2] * width - area[0]) * scale)), max(0, int((box[3] * height - area[1]) * scale)),
        )

    person = notes.person if notes is not None and notes.person is not None else _DEFAULT_PERSON
    mask = np.full((work_height, work_width), cv2.GC_BGD, np.uint8)
    x0, y0, x1, y1 = local(person)
    mask[y0:y1, x0:x1] = cv2.GC_PR_FGD
    for box in notes.ui if notes is not None else ():
        # 直播界面肯定不是人。
        ux0, uy0, ux1, uy1 = local(box)
        mask[uy0:uy1, ux0:ux1] = cv2.GC_BGD
    if notes is not None and notes.face is not None:
        # 脸框往里收一圈，肯定是人。
        fx0, fy0, fx1, fy1 = local(notes.face)
        dx, dy = (fx1 - fx0) // 6, (fy1 - fy0) // 6
        mask[fy0 + dy:fy1 - dy, fx0 + dx:fx1 - dx] = cv2.GC_FGD
    background_model, foreground_model = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    cv2.grabCut(work, mask, None, background_model, foreground_model, _GRABCUT_ITERATIONS, cv2.GC_INIT_WITH_MASK)
    hard = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(hard)
    if count > 1:
        hard = np.where(labels == 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA])), 255, 0).astype(np.uint8)
    # 补洞：从边上灌水，灌不到的“背景”是人身上的洞。
    flood = hard.copy()
    cv2.floodFill(flood, np.zeros((work_height + 2, work_width + 2), np.uint8), (0, 0), 255)
    hard = hard | cv2.bitwise_not(flood)
    if scale < 1:
        hard = cv2.resize(hard, (crop_width, crop_height), interpolation=cv2.INTER_LINEAR)
    radius = max(4, round(min(crop_height, crop_width) * _GUIDE_RADIUS))
    alpha = _guided_filter(crop.astype(np.float32) / 255, hard.astype(np.float32) / 255, radius, _GUIDE_EPS)
    return np.clip((alpha - 0.08) / 0.84, 0, 1)


def make_cutout(image_path: str | Path, notes: AIFrameNotes | None, out_dir: Path, *, model: Path | None = None) -> Path:
    """把人物抠出来，返回透明 PNG；有模型用模型，没有用 GrabCut。同一张图、同样的框、同一条路直接复用。"""

    import cv2
    import numpy as np

    source = Path(image_path)
    use_model = model_ready(model)
    stat = source.stat()
    key = hashlib.sha1(repr((
        str(source.resolve()), stat.st_size, stat.st_mtime_ns,
        (notes.person, notes.face, notes.ui) if notes is not None else None, "model" if use_model else "grabcut-v2",
    )).encode("utf-8"))
    output = out_dir / f"{key.hexdigest()[:20]}.png"
    if output.is_file():
        return output
    image = cv2.imdecode(np.fromfile(str(source), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"底图读不出来：{source}")
    height, width = image.shape[:2]
    area = _work_area(notes, width, height)
    crop = image[area[1]:area[3], area[0]:area[2]]
    alpha = _model_alpha(model, crop) if use_model else _grabcut_alpha(crop, notes, area, (width, height))
    alpha = _keep_person(alpha, notes, area, (width, height))
    full = np.zeros((height, width), np.float32)
    full[area[1]:area[3], area[0]:area[2]] = alpha
    alpha_bytes = (full * 255).astype(np.uint8)
    edge = Image.fromarray(alpha_bytes)
    with Image.open(source) as opened:
        picture = opened.convert("RGB")
    glow_size = max(3, round(height * _GLOW_RATIO))
    # 外发光按“够实”的部分外扩，发丝的半透明不把光圈撑得毛糙。
    solid = np.where(alpha_bytes > 128, 255, 0).astype(np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (glow_size * 2 + 1,) * 2)
    glow = cv2.GaussianBlur(cv2.dilate(solid, kernel), (0, 0), glow_size / 4)
    result = Image.new("RGBA", picture.size, (255, 255, 255, 0))
    result.putalpha(Image.fromarray(glow))
    person_layer = picture.convert("RGBA")
    person_layer.putalpha(edge)
    result.alpha_composite(person_layer)
    out_dir.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp.png")
    result.save(temporary)
    temporary.replace(output)
    return output
