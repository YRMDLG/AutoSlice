"""按看图标出的框取景：整张脸留在画面里，字带避开画面自带的界面和脸；再量一量成品里的字压没压到界面或脸。

框都用源图比例坐标 (x0, y0, x1, y1)。看图模型只负责“哪里是脸、哪里是界面”，怎么裁由这里算，
结果稳定、可测，不靠模型猜一个 4:3 的框。
"""

from __future__ import annotations

from .cover_ai import AIFrameNotes, Frac
from .cover_layout import background_geometry, canvas_size, text_layout
from .cover_model import BackgroundObject, CoverDocument, TextObject, object_for_profile

# 从头顶往下要露出几个脸高：宽松取景到胸口，特写到肩膀。
_SPAN = {"loose": 2.8, "tight": 1.7}
# 取景后脸高占画面高度的目标。
_FACE_SHARE = {"loose": 0.22, "tight": 0.36}
# 脸框上方大约还有这么多脸高的头发。
_HAIR = 0.3
# 人物（从头顶算）占画面高度的比例，其余留给字：字只放一侧 / 上下都放。
SUBJECT_SHARE = 0.62
SUBJECT_SHARE_SPLIT = 0.55
# 上下分置时上面那条（A，小字）占留白的比例。
_SPLIT_TOP_PART = 0.4
_MAX_ZOOM = 3.0
# 特写的高度最多是宽松取景的这么多。
_TIGHTER = 0.82
# 从原画面往里拉近的几档（相对能取的最大高度）。
_ZOOM_STEPS = (1.0, 0.85, 0.72, 0.6, 0.5, 0.42)
# 代价权重：字带里的界面、画面里的界面、字带压脸、脸偏离中线、脸大小偏离目标、剪掉头发。
_W_BAND_UI, _W_CROP_UI, _W_BAND_FACE, _W_OFF_CENTER, _W_SIZE, _W_HAIR = 4.0, 1.2, 6.0, 0.4, 1.0, 0.3


def area(box: Frac) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def overlap(first: Frac, second: Frac) -> float:
    return area((max(first[0], second[0]), max(first[1], second[1]), min(first[2], second[2]), min(first[3], second[3])))


def text_bands(crop: Frac, zone: str, share: float) -> tuple[Frac, ...]:
    """裁切框里留给字的带：下方 / 上方 / 上下各一条（上下分置）。"""

    left, top, right, bottom = crop
    height = bottom - top
    spare = (1 - share) * height
    if zone == "bottom":
        return ((left, bottom - spare, right, bottom),)
    if zone == "top":
        return ((left, top, right, top + spare),)
    return (
        (left, top, right, top + spare * _SPLIT_TOP_PART),
        (left, bottom - spare * (1 - _SPLIT_TOP_PART), right, bottom),
    )


def _pixels(box: Frac, size: tuple[int, int]) -> Frac:
    return box[0] * size[0], box[1] * size[1], box[2] * size[0], box[3] * size[1]


def subject_crop(
    source_size: tuple[int, int], canvas: tuple[int, int], notes: AIFrameNotes, zone: str, framing: str,
) -> tuple[Frac, float] | None:
    """在源图上找画布比例的裁切：整张脸在里面、人物在 zone 的另一侧，字带尽量不压界面和脸。

    zone 是字放哪：bottom / top / ""（上下各一条）；framing 是 loose（带上半身）或 tight（特写）。
    返回 (裁切框, 代价)；没标出脸或怎么裁都放不下整张脸时返回 None。
    """

    if notes.face is None:
        return None
    framing = framing if framing in _SPAN else "loose"
    width, height = max(1, source_size[0]), max(1, source_size[1])
    aspect = canvas[0] / max(1, canvas[1])
    face = _pixels(notes.face, (width, height))
    face_width, face_height = face[2] - face[0], face[3] - face[1]
    ui = tuple(_pixels(item, (width, height)) for item in notes.ui)
    share = SUBJECT_SHARE if zone in ("top", "bottom") else SUBJECT_SHARE_SPLIT
    head_top = face[1] - _HAIR * face_height
    if notes.person is not None and face[1] - face_height <= notes.person[1] * height <= face[1]:
        head_top = notes.person[1] * height
    largest = min(float(height), width / aspect)
    smallest = max(largest / _MAX_ZOOM, face_height * 1.3)
    if framing == "tight":
        # 特写至少比宽松取景再近一档；脸很大时两者容易算成同一个框，候选就重复了。
        loose = subject_crop(source_size, canvas, notes, zone, "loose")
        if loose is not None:
            largest = min(largest, (loose[0][3] - loose[0][1]) * height * _TIGHTER)
            if largest < smallest:
                return loose
    target = _SPAN[framing] * face_height / share
    # 候选高度：按脸大小算的目标附近，加上从原画面逐步拉近的几档（脸很大时目标会超出画面）。
    heights = sorted({
        round(max(smallest, min(largest, value)), 1)
        for value in (target * 0.85, target, target * 1.18, *(largest * ratio for ratio in _ZOOM_STEPS))
    })
    center = (face[0] + face[2]) / 2
    best: tuple[Frac, float] | None = None
    for crop_height in heights:
        crop_width = crop_height * aspect
        spare = (1 - share) * crop_height
        lead = {"bottom": 0.0, "top": 1.0}.get(zone, _SPLIT_TOP_PART) * spare
        # 头顶留全，或者剪掉一点头发、让脸往字带的反方向挪，给字腾地方。
        tops = {
            max(0.0, min(height - crop_height, value))
            for value in (head_top - lead - 0.04 * crop_height, face[1] - lead - 0.12 * face_height, face[1] - lead)
        }
        for top in tops:
            for shift in (-0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3):
                left = max(0.0, min(width - crop_width, center + shift * crop_width - crop_width / 2))
                crop = (left, top, left + crop_width, top + crop_height)
                # 整张脸（左右再留一点）必须在画面里。
                if face[0] - 0.1 * face_width < left or face[2] + 0.1 * face_width > crop[2] or face[1] < top or face[3] > crop[3]:
                    continue
                bands = text_bands(crop, zone, share)
                band_area = sum(area(band) for band in bands) or 1.0
                cost = (
                    _W_BAND_UI * sum(overlap(item, band) for item in ui for band in bands) / band_area
                    + _W_CROP_UI * sum(overlap(item, crop) for item in ui) / area(crop)
                    + _W_BAND_FACE * sum(overlap(face, band) for band in bands) / max(1.0, area(face))
                    + _W_OFF_CENTER * abs(center - (left + crop_width / 2)) / crop_width
                    + _W_SIZE * abs(face_height / crop_height - _FACE_SHARE[framing])
                    + _W_HAIR * max(0.0, top - head_top) / max(1.0, face_height)
                )
                if best is None or cost < best[1]:
                    best = (crop, cost)
    if best is None:
        return None
    (x0, y0, x1, y1), cost = best
    return (x0 / width, y0 / height, x1 / width, y1 / height), cost


def text_hit(document: CoverDocument, source_size: tuple[int, int], notes: AIFrameNotes, profile_key: str = "4x3") -> float:
    """成品里字压到画面自带界面或脸的程度：每个文字框被盖住的比例（压脸算两倍），取最大的那个。"""

    canvas = canvas_size(profile_key)
    background = next((item for item in document.objects if isinstance(item, BackgroundObject)), None)
    current = object_for_profile(document, background.id, profile_key) if background is not None else None
    if not isinstance(current, BackgroundObject):
        return 0.0
    drawn = background_geometry(current, canvas, source_size)
    worst = 0.0
    for item in document.objects:
        text = object_for_profile(document, item.id, profile_key) if isinstance(item, TextObject) else None
        if not isinstance(text, TextObject) or not text.visible or not text.text.strip():
            continue
        ink = text_layout(text, canvas).ink
        box = (
            (ink.left - drawn.left) / drawn.width, (ink.top - drawn.top) / drawn.height,
            (ink.right - drawn.left) / drawn.width, (ink.bottom - drawn.top) / drawn.height,
        )
        size = area(box)
        if size <= 0:
            continue
        hit = sum(overlap(box, ui_box) for ui_box in notes.ui) + (2 * overlap(box, notes.face) if notes.face else 0.0)
        worst = max(worst, hit / size)
    return worst
