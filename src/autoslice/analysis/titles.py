"""标题理解、样式证据和 Luna/Terra 多阶段复核的唯一实现。"""

from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from autoslice.analysis import danmaku as danmaku_analysis
from autoslice.llm import transport as llm_gateway
from autoslice.llm.prompts import (
    FinalTitlePromptEvidence as _FinalTitlePromptEvidence,
    PromptContext as _PromptContext,
    TitleStylePromptEvidence as _TitleStylePromptEvidence,
    build_final_title_generation_prompt as _render_final_title_generation_prompt,
    build_final_title_judge_prompt as _render_final_title_judge_prompt,
    build_title_style_prompt as _render_title_style_prompt,
)
from autoslice.transcription import service as transcription_service
from autoslice.transcription.contracts import DEFAULT_MAX_PUBLISH_TITLE_CHARS
from autoslice.streamer_profiles import (
    active_streamer_profile,
    current_streamer_profile,
    streamer_profile_context,
)

FACADE_EXPORTS = {
    'MAX_PUBLISH_TITLE_CHARS': 'MAX_PUBLISH_TITLE_CHARS',
    'MAX_TOPIC_TITLE_CHARS': 'MAX_TOPIC_TITLE_CHARS',
    'TITLE_STYLE_EXAMPLE_LIMIT': 'TITLE_STYLE_EXAMPLE_LIMIT',
    'TITLE_STYLE_PROFILE_PATH': 'TITLE_STYLE_PROFILE_PATH',
    '_GENERIC_PUBLISH_TITLES': '_GENERIC_PUBLISH_TITLES',
    '_GENERIC_TOPIC_TITLES': '_GENERIC_TOPIC_TITLES',
    '_LEADING_ACCOUNT_PREFIX_RE': '_LEADING_ACCOUNT_PREFIX_RE',
    '_META_TITLE_KEYWORDS': '_META_TITLE_KEYWORDS',
    '_PLACEHOLDER_TITLES': '_PLACEHOLDER_TITLES',
    '_PUBLISH_TITLE_META_KEYWORDS': '_PUBLISH_TITLE_META_KEYWORDS',
    '_SUCCESSFUL_RAIL_EVIDENCE_RE': '_SUCCESSFUL_RAIL_EVIDENCE_RE',
    '_TITLE_STYLE_TAG_KEYWORDS': '_TITLE_STYLE_TAG_KEYWORDS',
    '_active_streamer_aliases': '_active_streamer_aliases',
    '_build_title_style_prompt': '_build_title_style_prompt',
    '_clean_topic_title': '_clean_topic_title',
    '_clip_candidate_danmaku_prompt_evidence': '_clip_candidate_danmaku_prompt_evidence',
    '_clip_candidate_reference_publish_titles': '_clip_candidate_reference_publish_titles',
    '_compact_topic_phrase': '_compact_topic_phrase',
    '_derive_topic_title': '_derive_topic_title',
    '_fallback_publish_title': '_fallback_publish_title',
    '_fallback_title_from_text': '_fallback_title_from_text',
    '_is_bad_topic_title': '_is_bad_topic_title',
    '_is_generic_topic_title': '_is_generic_topic_title',
    '_is_incomplete_ai_title': '_is_incomplete_ai_title',
    '_is_placeholder_title': '_is_placeholder_title',
    '_load_title_style_profile': 'load_title_style_profile',
    '_manual_title_from_text': '_manual_title_from_text',
    '_normalise_obvious_report_terms': '_normalise_obvious_report_terms',
    '_normalise_publish_title': '_normalise_publish_title',
    '_normalise_title_hook': '_normalise_title_hook',
    '_profile_formal_names': '_profile_formal_names',
    '_prompt_context': '_prompt_context',
    '_prompt_streamer_name': '_prompt_streamer_name',
    '_publish_title_example': '_publish_title_example',
    '_publish_title_instruction': '_publish_title_instruction',
    '_publish_title_prefix': '_publish_title_prefix',
    '_replace_streamer_role': '_replace_streamer_role',
    '_sanitize_transport_claims': '_sanitize_transport_claims',
    '_select_title_style_examples': '_select_title_style_examples',
    '_specific_topic_phrase': '_specific_topic_phrase',
    '_streamer_report_name': '_streamer_report_name',
    '_streamer_role_pattern': '_streamer_role_pattern',
    '_strip_body_prefix': '_strip_body_prefix',
    '_strip_title_meta': '_strip_title_meta',
    '_title_style_profile_path': '_title_style_profile_path',
    '_final_title_review_payload': '_final_title_review_payload',
    '_build_final_title_generation_prompt': '_build_final_title_generation_prompt',
    '_normalise_final_title_option': '_normalise_final_title_option',
    '_parse_final_title_candidates': '_parse_final_title_candidates',
    '_build_final_title_judge_prompt': '_build_final_title_judge_prompt',
    '_parse_final_title_judgement': '_parse_final_title_judgement',
    '_review_selected_publish_titles': 'review_selected_publish_titles',
}


_profile_matches_streamer = transcription_service.profile_matches_streamer
_normalise_streamer_terms = transcription_service._normalise_streamer_terms
_danmaku_prompt_evidence = danmaku_analysis._danmaku_prompt_evidence
format_elapsed_time = danmaku_analysis.format_elapsed_time
DANMAKU_WINDOW = danmaku_analysis.DANMAKU_WINDOW
LLMStructuredOutputError = llm_gateway.LLMStructuredOutputError
_extract_json_payload = llm_gateway.extract_json_payload
_short_llm_error = llm_gateway.short_llm_error

TITLE_REVIEW_BATCH_SIZE = 3
TITLE_REVIEW_DEFAULT_CONCURRENCY = 3
TITLE_REVIEW_MAX_CONCURRENCY = 4


def _configured_title_review_concurrency():
    """读取标题复核并发数，沿用话题复核的受控环境变量。"""
    raw_value = os.environ.get(
        "AUTOSLICE_LLM_CONCURRENCY",
        str(TITLE_REVIEW_DEFAULT_CONCURRENCY),
    )
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = TITLE_REVIEW_DEFAULT_CONCURRENCY
    return max(1, min(TITLE_REVIEW_MAX_CONCURRENCY, value))


def _serialized_title_review_progress_callback(progress_callback):
    """并行标题复核时串行转发完整进度消息。"""
    if not progress_callback:
        return None
    lock = threading.Lock()

    def report(message, step, total):
        with lock:
            progress_callback(message, step, total)

    return report


MAX_PUBLISH_TITLE_CHARS = DEFAULT_MAX_PUBLISH_TITLE_CHARS

TITLE_STYLE_PROFILE_PATH = str(
    current_streamer_profile().title_style_profile or ""
)

TITLE_STYLE_EXAMPLE_LIMIT = 8

_LEADING_ACCOUNT_PREFIX_RE = re.compile(
    r'^\s*[【\[][^】\]\r\n]{1,32}[】\]]\s*',
    re.IGNORECASE,
)

_PUBLISH_TITLE_META_KEYWORDS = (
    "publish_title", "投稿标题建议", "标题建议如下", "根据要求", "按照要求",
    "只输出", "最终JSON", "最终 JSON", "can_slice", "points", "作为模型",
    "作为AI", "无法生成", "信息不足", "仅供参考",
)

_GENERIC_PUBLISH_TITLES = {
    "直播精彩片段", "精彩直播片段", "直播高光", "精彩片段",
    "日常聊天", "日常闲聊", "游戏过程", "互动片段",
}

_TITLE_STYLE_TAG_KEYWORDS = {
    "SC": ("sc", "醒目留言", "付费留言", "红sc", "留言"),
    "观众互动": ("观众", "弹幕", "音悦生", "舰长", "礼物", "感谢", "互动"),
    "游戏": ("游戏", "关卡", "过关", "失败", "挑战", "节奏天国", "躲猫猫"),
    "看视频": ("看视频", "视频", "二创", "连线", "回放"),
    "唱歌": ("唱歌", "点歌", "演唱", "歌曲", "舞台"),
    "温情": ("晚安", "陪伴", "鼓励", "谢谢大家", "温柔", "感动"),
    "日常": ("出差", "下飞机", "打车", "外卖", "妈妈", "音妈", "线下", "日常"),
    "新衣": ("新衣", "衣服", "造型", "黑丝", "丝袜", "袜子", "皮裙", "裤子", "发型", "头发", "蓝框", "光环"),
    "AI": ("ai音", "ai", "人工智能", "紫色", "应援色", "女王音"),
    "视觉细节": ("虾线", "鼓包", "划破", "破了", "挂钩", "反光", "中间", "蓝框", "双层", "纹身"),
    "目标反差": ("目标", "万粉", "粉丝", "游戏高手", "做不到", "更难", "难度", "百大"),
    "整蛊": ("整蛊", "恶心", "坑朋友", "送朋友", "叫爸爸", "cdk", "激活", "游戏库", "steam", "社死", "天塌了"),
    "反差": ("反差", "居然", "却", "没想到", "不一样", "完全不同", "对不起", "不能"),
}

def _profile_formal_names(profile):
    """返回报告中应替换为粉丝称呼的正式名，不改写其它粉丝昵称。"""
    names = {profile.canonical_name, *profile.path_keywords}
    short_name = re.sub(
        r'[A-Za-z][A-Za-z0-9_. -]*$',
        '',
        profile.canonical_name,
    ).strip()
    if len(short_name) >= 2:
        names.add(short_name)
    return tuple(
        sorted((name for name in names if name), key=len, reverse=True)
    )

def _active_streamer_aliases():
    """返回当前任务可用于 SC 原话的常用称呼。"""
    profile = current_streamer_profile()
    aliases = tuple(dict.fromkeys((*profile.aliases, profile.report_name)))
    return aliases or (profile.report_name,)

def _publish_title_prefix():
    return current_streamer_profile().title_prefix

def _publish_title_instruction(*, quoted=True):
    prefix = _publish_title_prefix()
    if prefix:
        rendered = f'“{prefix}”' if quoted else prefix
        return f"固定以{rendered}开头"
    return "不要添加账号专属方括号前缀"

def _publish_title_example(text):
    return f"{_publish_title_prefix()}{text}"

def _title_style_profile_path():
    active = active_streamer_profile()
    if active is not None:
        return str(active.title_style_profile or "")
    return TITLE_STYLE_PROFILE_PATH

def _prompt_streamer_name(streamer_name=None):
    display_name = _streamer_report_name(
        streamer_name or current_streamer_profile().report_name
    )
    return "所选主播" if display_name == "主播" else display_name

def _streamer_role_pattern(*extra_names):
    """生成只包含当前主播称呼的安全正则片段。"""
    profile = current_streamer_profile()
    names = {
        profile.report_name,
        *profile.aliases,
        *extra_names,
    }
    return "(?:" + "|".join(
        re.escape(name)
        for name in sorted((item for item in names if item), key=len, reverse=True)
    ) + ")"

def _streamer_report_name(streamer_name):
    """报告展示用粉丝称呼，避免正式名太生硬。"""
    profile = current_streamer_profile()
    name = str(streamer_name or "").strip()
    if _profile_matches_streamer(profile, name):
        return profile.report_name
    return name or profile.report_name

def _manual_title_from_text(text):
    """从人工时间轴一句话生成短标题。"""
    clean = re.sub(r'[“”"（）()\[\]【】]', '', text or "")
    clean = re.sub(r'《(.+?)》', r'\1', clean)
    parts = [part.strip() for part in re.split(r'[，。；;：:、]', clean) if part.strip()]
    clean = parts[0] if parts else clean.strip()
    if len(clean) < 5 and len(parts) > 1:
        clean = f"{parts[0]}{parts[1]}"
    if len(clean) < 4:
        clean = re.sub(r'\s+', '', text or "")[:MAX_TOPIC_TITLE_CHARS]
    return (clean[:MAX_TOPIC_TITLE_CHARS] or "人工时间轴重点").strip()

def load_title_style_profile(profile_path=None):
    """读取历史投稿标题风格配置；配置损坏时安全降级为空配置。"""
    path = profile_path if profile_path is not None else _title_style_profile_path()
    empty = {"source": {}, "rules": [], "examples": []}
    if not path:
        return empty
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError, TypeError):
        return empty
    if not isinstance(payload, dict):
        return empty

    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    is_template = bool(source.get("template"))

    rules = []
    for rule in payload.get("rules") or []:
        text = re.sub(r'\s+', ' ', str(rule)).strip()
        if text and text not in rules:
            rules.append(text)

    examples = []
    seen_titles = set()
    title_prefix = _publish_title_prefix()
    for item in payload.get("examples") or []:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue
        title = re.sub(r'\s+', ' ', str(item.get("title", ""))).strip()
        if is_template:
            # 公开模板使用【主播】占位符，运行时必须跟随当前主播配置；
            # 私有历史样本则仍严格隔离前缀，防止串号。
            title = re.sub(
                r'^\u3010[^\u3011]{1,80}\u3011',
                title_prefix,
                title,
                count=1,
            )
        if (
            (title_prefix and not title.startswith(title_prefix))
            or "直播回放" in title
            or title in seen_titles
            or len(title) > MAX_PUBLISH_TITLE_CHARS
        ):
            continue
        tags = [
            re.sub(r'\s+', ' ', str(tag)).strip()
            for tag in item.get("tags") or []
            if str(tag).strip()
        ]
        examples.append({
            "title": title,
            "tags": tags,
            "source": str(item.get("source", "history")).strip() or "history",
        })
        seen_titles.add(title)
    return {
        "source": source,
        "rules": rules,
        "examples": examples,
    }

def _select_title_style_examples(context_text, profile=None, limit=TITLE_STYLE_EXAMPLE_LIMIT):
    """按当前话题语义选择少量同类标题样本，避免把全部历史标题塞进提示词。"""
    profile = profile or load_title_style_profile()
    examples = profile.get("examples") or []
    if not examples or limit <= 0:
        return []
    context = str(context_text or "").lower()
    active_tags = {
        tag
        for tag, keywords in _TITLE_STYLE_TAG_KEYWORDS.items()
        if any(keyword.lower() in context for keyword in keywords)
    }
    scored = []
    for index, item in enumerate(examples):
        tags = set(item.get("tags") or [])
        score = len(active_tags & tags) * 10
        score += {
            "user_approved": 4,
            "recent": 2,
            "high_play": 1,
        }.get(item.get("source"), 0)
        scored.append((-score, index, item))
    scored.sort(key=lambda row: (row[0], row[1]))
    return [item for _, _, item in scored[:limit]]

def _build_title_style_prompt(context_text="", compact=False):
    """把账号历史标题规律压缩成可复用的提示词片段。"""
    profile = load_title_style_profile()
    rule_limit = 4 if compact else 8
    example_limit = 4 if compact else TITLE_STYLE_EXAMPLE_LIMIT
    rules = (profile.get("rules") or [])[:rule_limit]
    examples = _select_title_style_examples(context_text, profile=profile, limit=example_limit)
    return _render_title_style_prompt(
        _TitleStylePromptEvidence(
            source=dict(profile.get("source") or {}),
            rules=tuple(rules),
            examples=tuple(examples),
        )
    )

def _prompt_context(
        streamer_name=None, *, context_text="", compact=False,
        publish_title_example_text="具体事件钩子👀结果或反差"):
    """把运行态配置解析为 prompt 模块唯一接受的显式上下文。"""
    profile = current_streamer_profile()
    streamer_display_name = _streamer_report_name(
        streamer_name or profile.report_name
    )
    prompt_streamer_name = _prompt_streamer_name(streamer_display_name)
    editor_subject = (
        profile.canonical_name
        if profile.canonical_name != "主播"
        else "所选主播"
    )
    return _PromptContext(
        streamer_display_name=streamer_display_name,
        prompt_streamer_name=prompt_streamer_name,
        editor_subject=editor_subject,
        title_prefix_rule=_publish_title_instruction(quoted=False),
        title_prefix_rule_quoted=_publish_title_instruction(),
        publish_title_example=_publish_title_example(
            publish_title_example_text,
        ),
        title_style=_build_title_style_prompt(
            context_text,
            compact=compact,
        ),
        streamer_aliases=tuple(_active_streamer_aliases()),
    )

_PLACEHOLDER_TITLES = (
    "无明显话题", "话题标题", "下一个话题", "未命名片段", "从字幕看",
    "其他话题", "下一段", "可能的切分", "根据要求",
)

_GENERIC_TOPIC_TITLES = ("日常聊天互动", "感谢礼物互动", "视频评论讨论", "游戏关卡挑战")

MAX_TOPIC_TITLE_CHARS = 24

_META_TITLE_KEYWORDS = (
    "考虑分成", "考虑输出", "更好的方式", "更合理", "我们仔细", "仔细分析",
    "每个时间段", "提取可理解", "然后紧接着", "第二个话题", "第一个话题",
    "可能的划分", "话题划分", "标题：", "标题:", "基于时间顺序",
    "建议这样", "字幕原文", "让我们详细解析", "我们还需要", "先理解字幕",
    "所以整体", "大致内容", "可能的整理", "从字幕看", "主要内容",
    "第一part", "第二part", "第三个短", "话题一", "话题二",
    "最佳方式", "我们仔细看", "时间线变化", "我们分析", "连续讲话",
    "规划话题结构", "输出时不要写Part", "一个合理的方法", "合理的方法",
    "观察事件",
    "中文（问候等）", "现在规划", "可能的最佳划分", "最佳划分",
    "具体分段", "梳理字幕", "连续意思", "输出最终条目", "读懂字幕",
    "具体要点", "比如",
    "这部分明显", "继续讨论这个视频", "继续这段剧情", "總結話題",
    "总结话题", "根據字幕", "可以劃分", "劃分為", "输出内容",
    "严格按照格式", "标题加emoji", "最终输出", "感谢一个礼物",
    "礼物、弹幕爆点", "确保时间戳", "让我们仔细构建", "最终输出示例",
    "注意称呼",
    "points:", "points：", "title:", "title：", "要点：", "要点:",
    "重新考虑分块内容", "我们先把内容分几个话题", "那么我们定义",
    "整体时间段", "让我们尝试提取话题", "我们确保每个话题",
    "这段讨论", "这段继续", "内容有些混乱",
    "约2分", "划分建议", "先构思", "topic1", "topic2",
    "观察内容", "更仔细看",
    "我们规划话题", "先考虑can", "建议分成两个话题",
    "先整理出具体的时间段", "根据人工时间轴", "最终JSON", "最终 JSON",
    "我们尝试解读字幕",
    "人工时间轴参考", "观察时间戳", "需要写点", "我们看内容",
    "我们来看内容", "根据内容推断边界",
    "其他话题", "这些人工时间轴", "与上一段有重叠", "下一段",
    "可能的切分", "我们来确定话题", "根据要求", "可能主播",
    "观众可能", "一个合理的划分",
)

def _normalise_obvious_report_terms(text):
    """修正无需猜测语义的报告残留，不改写源字幕。"""
    clean = str(text or "")
    report_name = current_streamer_profile().report_name
    if report_name and len(set(report_name)) == 1:
        clean = re.sub(
            rf'{re.escape(report_name[0])}{{{len(report_name) + 1},}}',
            report_name,
            clean,
        )
    if "自热" in clean:
        clean = clean.replace("发热刀", "发热包")
    clean = re.sub(
        r'商家自己没放清楚(?:没看清楚)?',
        '商家自己没看清订单',
        clean,
    )
    return clean


def _is_generic_topic_title(title):
    compact = re.sub(r'\s+', '', str(title or ""))
    if compact in _GENERIC_TOPIC_TITLES:
        return True
    if re.match(r'^(?:有|一位|某位)?观众(?:留言|提问|询问|分享|投稿|说)', compact):
        return True
    streamer_pattern = _streamer_role_pattern("主播", "她")
    return bool(re.fullmatch(
        rf'(?:{streamer_pattern})?(?:正在|在)?'
        r'(?:外卖|美团|大众点评|游戏|直播)?'
        r'(?:评审|点评|评论|互动|聊天|讨论|游戏)(?:中|过程)?',
        compact,
    ))


def _clean_topic_title(raw_title):
    """清理标题里的切片标记和模型推理说明，保留可读标题。"""
    title = _normalise_obvious_report_terms(raw_title)
    title = title.replace("✂️", "").replace("✂", "")
    title = _strip_title_meta(title)
    title = re.sub(r'\s+', ' ', title)
    return title.strip(' -—：:？?。；;，,') or "未命名片段"

def _is_bad_topic_title(title):
    """识别模型把整段字幕当标题的情况。"""
    clean = re.sub(r'\s+', '', title or "")
    if not clean:
        return True
    if clean in {
        "内容", "等", "根据人工时间轴", "划分建议", "先考虑can", "我们规划话题",
        "人工时间轴参考", "观察时间戳", "其他话题", "下一段", "可能的切分",
        "根据要求",
    }:
        return True
    if any(keyword in title for keyword in _META_TITLE_KEYWORDS):
        return True
    if re.match(r'^(所以|其实|可能|先|大致|关于|这部分|继续).*?(整体|字幕|话题|整理|内容|弹幕|留言|剧情|礼物)', title):
        return True
    if len(clean) > MAX_TOPIC_TITLE_CHARS:
        return True
    if any(keyword in clean for keyword in ("感谢我有十八岁的音乐", "感个CH的声音好", "但是下一次我不确定", "从字幕看", "然后从3", "嗯好了会了会了")):
        return True
    if re.fullmatch(r'[A-Za-z]{12,}', clean):
        return True
    return False

def _compact_topic_phrase(text, max_chars=MAX_TOPIC_TITLE_CHARS):
    """从正文提取一个短标题片段。"""
    clean = _strip_body_prefix(text)
    role_pattern = _streamer_role_pattern("主播", "她", "他")
    clean = re.sub(
        rf'^(这段|这里|{role_pattern}|继续)?(在)?(说|提到|聊到|表示|分析|吐槽|感谢|读弹幕|回应)',
        '',
        clean,
    )
    clean = re.sub(r'[“”"`]', '', clean)
    clean = re.split(r'[，。；;：:、（）()\s]', clean, maxsplit=1)[0]
    clean = clean.strip(' -—：:？?。；;，,、')
    return clean[:max_chars] if clean else ""

def _specific_topic_phrase(text, max_chars=MAX_TOPIC_TITLE_CHARS):
    """从泛化叙述中抽取事件冲突，避免标题只剩“正在评审中”。"""
    clean = _strip_body_prefix(text)
    role_pattern = _streamer_role_pattern("主播", "她")
    clean = re.sub(
        rf'^{role_pattern}(?:正在|在)?[^，,。]{{0,18}}(?:中|时)?[，,]',
        '',
        clean,
    )
    clean = re.sub(
        r'^(?:有|一位|某位)?观众(?:留言|提问|询问|分享|投稿|说)(?:称|说)?',
        '',
        clean,
    )
    clean = re.sub(rf'^{role_pattern}(?:正在|在)?', '', clean)
    clean = re.sub(
        r'^(?:发现|指出|看到|读到|认为|表示|回应|吐槽|提到|直呼)',
        '',
        clean,
    )
    clean = clean.replace("商家提供的证据照片", "商家证据照片")
    clean = re.sub(r'[“”"`]', '', clean)
    clean = re.split(r'[，。；;：:（）()\s]', clean, maxsplit=1)[0]
    clean = clean.strip(' -—：:？?。；;，,、')
    return clean[:max_chars] if len(clean) >= 5 else ""

def _derive_topic_title(title, body_lines):
    """长标题兜底：优先从正文关键词/第一条要点生成短标题。"""
    body_text = " ".join(_strip_body_prefix(line) for line in body_lines)
    title_needs_rebuild = _is_bad_topic_title(title) or _is_generic_topic_title(title)
    if title_needs_rebuild:
        manual_match = re.search(r'人工时间轴[⭐★]*[:：]\s*(?:\d{1,2}:\d{2}(?::\d{2})?\s*)?(.+?)(?:\s+人工时间轴|$)', body_text)
        if manual_match:
            manual_title = _manual_title_from_text(manual_match.group(1))
            if manual_title and not _is_bad_topic_title(manual_title):
                return manual_title
    keyword_titles = (
        (("300万", "石头"), "翡翠切石与包装"),
        (("柳师傅", "包装"), "翡翠切石与包装"),
        (("石头", "包装"), "翡翠切石与包装"),
        (("眼睛", "鲁鲁修"), "角色画风与番剧回忆"),
        (("英兰", "男公关"), "樱兰高校番剧回忆"),
        (("字母A",), "字母A关卡挑战"),
        (("a特别难",), "字母A关卡挑战"),
        (("闭着眼", "这一关"), "闭眼关卡挑战"),
        (("前女友", "回礼"), "前女友回礼吐槽"),
        (("出轨",), "出轨玩笑互动"),
        (("期末", "晚安"), "期末成绩与晚安互动"),
        (("十年前", "手机"), "十年前视频感慨"),
        (("千万", "播放"), "千万播放视频评论"),
        (("像素风",), "像素风古早感"),
        (("朱鹮", "新闻"), "读新闻吐槽朱鹮"),
        (("妈妈", "奶茶"), "奶茶晚安互动"),
        (("晚安", "音乐生"), "晚安收尾互动"),
        (("哼唱练习", "拍子"), "唱歌练习找拍子"),
        (("武士", "关卡"), "武士关卡挑战"),
        (("店铺", "亏损"), "连麦分析店铺亏损"),
        (("咖啡", "加盟"), "咖啡加盟经营分析"),
        (("银宝生日快乐",), "生日祝福与视频回顾"),
        (("生日祝福", "视频"), "生日祝福与视频回顾"),
        (("永远", "生日快乐"), "生日祝福与感悟"),
        (("礼物",), "感谢礼物互动"),
        (("评论",), "读评论与感想"),
    )
    for keywords, fallback_title in keyword_titles:
        if all(keyword in body_text for keyword in keywords):
            if title_needs_rebuild:
                return fallback_title
    if not title_needs_rebuild:
        return title
    for line in body_lines:
        phrase = _specific_topic_phrase(line)
        if phrase:
            return phrase
    for line in body_lines:
        phrase = _compact_topic_phrase(line)
        if phrase and len(phrase) >= 4:
            return phrase
    if _is_bad_topic_title(title):
        return ""
    return "日常聊天互动"

def _strip_title_meta(title):
    """去掉模型写进标题里的自我判断尾巴，避免污染报告和文件名。"""
    title = re.sub(r'\s+', ' ', title).strip()
    # 常见污染："标题 ？但时间太短。最好合并。"、"标题，但..."、"标题。例如..."
    title = re.split(r'\s*[？?。；;，,]\s*(?:但|不过|最好|可能|例如|所以|因为|由于|是否|应该|可以)', title, maxsplit=1)[0]
    title = re.split(r'\s+(?:但|不过|最好|可能|例如|所以|因为|由于|是否|应该|可以)', title, maxsplit=1)[0]
    title = re.sub(r'[（(]\s*(?:但|因为|由于|弹幕|时间|不切|不加标记|不建议切|不要切).*?[）)]', '', title)
    title = re.sub(r'[？?。；;，,：:]+$', '', title)
    return title.strip(' -—：:？?。；;，,')

def _strip_body_prefix(line):
    """去掉正文要点符号，便于判断是否是模型自我说明。"""
    stripped = line.strip()
    while stripped.startswith(("·", "●", "-", "*", "•")):
        stripped = stripped[1:].strip()
    return stripped

def _is_placeholder_title(title):
    """过滤模型占位标题和“无明显话题”。"""
    clean = _strip_body_prefix(title)
    if not clean:
        return True
    if any(placeholder in clean for placeholder in _PLACEHOLDER_TITLES):
        return True
    return clean in ("标题", "（标题）", "(标题)")

def _fallback_publish_title(topic_title):
    """模型标题缺失或受污染时，生成不会泄漏推理文字的安全投稿标题。"""
    clean_title = _clean_topic_title(str(topic_title or ""))
    if not clean_title or _is_bad_topic_title(clean_title):
        clean_title = "值得留意的直播片段"
    return _publish_title_example(clean_title)[:MAX_PUBLISH_TITLE_CHARS]

def _normalise_publish_title(raw_title, topic_title):
    """清理投稿标题并统一账号前缀；不合格时回退到话题短标题。"""
    raw_text = "" if raw_title is None else str(raw_title)
    title = _normalise_obvious_report_terms(raw_text)
    title = title.replace("**", "").replace("`", "")
    title = re.sub(r'^\s*(?:publish_title|投稿标题(?:建议)?)\s*[：:]\s*', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s+', ' ', title).strip(' \t\r\n-—')
    title = _LEADING_ACCOUNT_PREFIX_RE.sub('', title, count=1).strip()
    title_prefix = _publish_title_prefix()
    if (
        not title
        or len(title) + len(title_prefix) > MAX_PUBLISH_TITLE_CHARS
        or len(re.sub(r'\s+', '', title)) < 4
        or title in _GENERIC_PUBLISH_TITLES
        or any(keyword.lower() in title.lower() for keyword in _PUBLISH_TITLE_META_KEYWORDS)
        or any(token in title for token in ('{"topics"', '```', '\\n'))
    ):
        return _fallback_publish_title(topic_title)
    return f"{title_prefix}{title}"

def _normalise_title_hook(raw_hook):
    """保存模型提炼的标题爆点摘要，供审计使用，不把推理过程写入报告。"""
    if not isinstance(raw_hook, dict):
        return None
    hook_type = re.sub(r'\s+', ' ', str(
        raw_hook.get("type", raw_hook.get("kind", ""))
    )).strip()
    fact = re.sub(r'\s+', ' ', str(
        raw_hook.get("fact", raw_hook.get("peak_event", ""))
    )).strip()
    contrast = re.sub(r'\s+', ' ', str(
        raw_hook.get("contrast", raw_hook.get("why_clickable", ""))
    )).strip()
    if not fact:
        return None
    if len(fact) > 120:
        fact = fact[:119].rstrip() + "…"
    if len(contrast) > 120:
        contrast = contrast[:119].rstrip() + "…"
    if len(hook_type) > 30:
        hook_type = hook_type[:30]
    result = {"fact": fact}
    if hook_type:
        result["type"] = hook_type
    if contrast:
        result["contrast"] = contrast
    return result

_SUCCESSFUL_RAIL_EVIDENCE_RE = re.compile(
    r'(?:抢到|买到|订到|拿到).{0,16}(?:高铁|车)?票|'
    r'(?:还好|幸好|庆幸).{0,24}(?:高铁|车|票)|'
    r'(?<!没)赶上(?:了)?(?:高铁|车)|顺利.{0,12}(?:到家|回来)'
)

def _sanitize_transport_claims(title, evidence_lines):
    """用字幕中的确定事实清理投稿标题里的误车/误机反写。"""
    value = str(title or "").strip()
    evidence = re.sub(
        r'\s+', '', " ".join(_strip_body_prefix(line) for line in evidence_lines or [])
    )
    if not value:
        return value

    if re.search(r'闹钟(?:在)?半夜(?:十二|12)点响', value) and re.search(
            r'闹钟(?:没响|未响|没有响|.{0,12}误设.{0,8}半夜)', evidence):
        value = re.sub(
            r'闹钟(?:在)?半夜(?:十二|12)点响',
            '闹钟误设成半夜12点',
            value,
        )
    if not _SUCCESSFUL_RAIL_EVIDENCE_RE.search(evidence):
        return value

    replacements = (
        (r'痛失高铁票', '差点错过最后一班高铁'),
        (r'(?<!差点)(?:错过|误了)(?:最后一班)?高铁(?:票|车次)?', '差点错过最后一班高铁'),
        (r'(?<!差点)没赶上(?:最后一班)?高铁', '差点没赶上最后一班高铁'),
        (r'误机', '赶高铁惊魂'),
    )
    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value)
    return value

def _is_incomplete_ai_title(value):
    """识别模型截在时间连接词上的半句标题。"""
    compact = re.sub(r'\s+', '', str(value or "")).strip('，。！？!?：:；;')
    if not compact.endswith("时"):
        return False
    complete_time_words = ("小时", "当时", "平时", "有时", "临时", "及时", "准时", "顿时")
    return len(compact) >= 6 and not compact.endswith(complete_time_words)

def _fallback_title_from_text(text):
    """LLM 漏分块时，根据字幕关键词生成保底话题标题。"""
    if not text:
        return "日常聊天互动"
    rules = (
        (("人体比例",), "人体比例讨论"),
        (("痔疮",), "奇怪广告吐槽"),
        (("猫咪",), "猫咪内容互动"),
        (("像素风",), "像素风古早感"),
        (("节奏", "天国"), "节奏天国游戏"),
        (("手感", "火热"), "节奏天国游戏"),
        (("武士",), "武士关卡游戏"),
        (("关卡",), "游戏关卡挑战"),
        (("游戏", "关卡"), "游戏过程互动"),
        (("咖啡", "店"), "咖啡店经营讨论"),
        (("加盟",), "加盟经营讨论"),
        (("礼物",), "感谢礼物互动"),
        (("生日",), "生日相关聊天"),
        (("晚安",), "晚安收尾互动"),
        (("弹幕",), "读弹幕互动"),
        (("视频", "评论"), "视频评论讨论"),
        (("新闻",), "新闻内容吐槽"),
    )
    for keywords, title in rules:
        if all(keyword in text for keyword in keywords):
            return title
    return "日常聊天互动"

def _replace_streamer_role(text, streamer_name):
    """报告展示时把“主播/正式名”替换为更自然的粉丝称呼。"""
    display_name = _streamer_report_name(streamer_name)
    if not display_name or display_name == "主播":
        return text
    result = text or ""
    profile = current_streamer_profile()
    for formal_name in _profile_formal_names(profile):
        result = result.replace(formal_name, display_name)
    result = result.replace("主播", display_name)
    return _normalise_streamer_terms(result, streamer_name=display_name)

def _clip_candidate_danmaku_prompt_evidence(candidate):
    """把候选上已计算的弹幕特征转成 Terra 可核查的受限证据。"""
    peak_start = int(candidate.get("danmaku_peak_start") or max(
        0,
        int(candidate.get("slice_anchor", candidate.get("start", 0)))
        - DANMAKU_WINDOW // 2,
    ))
    content_evidence = candidate.get("danmaku_content_evidence")
    has_metrics = any(
        candidate.get(key) is not None
        for key in (
            "peak_density", "density_ratio", "danmaku_local_surge_ratio",
            "danmaku_selection_score", "danmaku_content_quality",
        )
    )
    if not content_evidence and not has_metrics:
        return None
    features = {
        "peak_start": peak_start,
        "density": candidate.get("peak_density"),
        "global_ratio": candidate.get("density_ratio"),
        "local_surge_ratio": candidate.get("danmaku_local_surge_ratio"),
        "density_percentile": candidate.get("danmaku_density_percentile"),
        "selection_score": candidate.get("danmaku_selection_score"),
        "interaction_signal": candidate.get("danmaku_interaction_signal"),
        "content_evidence": content_evidence,
    }
    title_context = candidate.get("title_cue_context") or " ".join([
        str(candidate.get("title", "")),
        *(str(line) for line in candidate.get("body") or []),
    ])
    payload = _danmaku_prompt_evidence(
        features,
        title_context=title_context,
    )
    if payload is not None:
        payload["topic_alignment"] = candidate.get("danmaku_topic_alignment")
    return payload

def _clip_candidate_reference_publish_titles(candidate, limit=4):
    """收集前序阶段已生成的标题，供最终复核防止具体钩子被改丢。"""
    values = [
        candidate.get("publish_title"),
        candidate.get("reference_publish_title"),
    ]
    for entry in candidate.get("manual_timeline") or []:
        if not isinstance(entry, dict):
            continue
        values.extend((
            entry.get("publish_title"),
            entry.get("reference_publish_title"),
        ))

    titles = []
    for value in values:
        title = re.sub(r'\s+', ' ', str(value or ""))
        title = title.replace("**", "").replace("`", "").strip()
        if (
            len(title) < 6
            or len(title) > MAX_PUBLISH_TITLE_CHARS
            or any(keyword.lower() in title.lower() for keyword in _PUBLISH_TITLE_META_KEYWORDS)
            or title in titles
        ):
            continue
        titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def _final_title_review_payload(topics, compact=False):
    """为独立标题阶段整理受限证据，不让标题生成继续承担切片判断。"""
    payload = []
    for index, topic in enumerate(topics, 1):
        body_limit = 8 if compact else 18
        subtitle_limit = 4 if compact else 10
        body = [
            _strip_body_prefix(line)
            for line in (topic.get("body") or [])[:body_limit]
            if _strip_body_prefix(line)
        ]
        subtitle_evidence = list(topic.get("core_subtitle_evidence") or [])
        if not subtitle_evidence:
            subtitle_evidence = [
                line for line in body if line.startswith("字幕核查：")
            ]
        manual_references = []
        for entry in topic.get("manual_timeline") or []:
            if not isinstance(entry, dict):
                continue
            reference = {
                "text": entry.get("text"),
                "summary": (entry.get("summary") or [])[:3],
                "publish_title": entry.get("publish_title"),
                "stars": entry.get("stars", 0),
            }
            if any(reference.values()):
                manual_references.append(reference)
        payload.append({
            "id": index,
            "time": f"{format_elapsed_time(topic.get('start', 0))}-{format_elapsed_time(topic.get('end', 0))}",
            "short_title": topic.get("title"),
            "current_publish_title": topic.get("publish_title"),
            "reference_publish_titles": _clip_candidate_reference_publish_titles(topic),
            "title_hook": topic.get("title_hook"),
            "subtitle_evidence": subtitle_evidence[:subtitle_limit],
            "verified_points": body,
            "manual_references": manual_references[:2 if compact else 4],
            "danmaku_evidence": _clip_candidate_danmaku_prompt_evidence(topic),
        })
    return payload

def _build_final_title_generation_prompt(topics, streamer_name=None, compact=False):
    """只负责发散标题方案；切片价值和边界已经在上一阶段确定。"""
    payload = _final_title_review_payload(topics, compact=compact)
    context = _prompt_context(
        streamer_name,
        context_text=json.dumps(payload, ensure_ascii=False),
        compact=compact,
    )
    return _render_final_title_generation_prompt(
        _FinalTitlePromptEvidence(
            context=context,
            topics=tuple(payload),
        )
    )

def _normalise_final_title_option(value, topic):
    if isinstance(value, dict):
        value = value.get("title")
    raw = re.sub(r'\s+', ' ', str(value or ""))
    raw = raw.replace("**", "").replace("`", "").strip()
    if (
        len(raw) < 6
        or len(raw) > MAX_PUBLISH_TITLE_CHARS
        or any(keyword.lower() in raw.lower() for keyword in _PUBLISH_TITLE_META_KEYWORDS)
    ):
        return None
    evidence_lines = [
        *(topic.get("body") or []),
        *(topic.get("core_subtitle_evidence") or []),
    ]
    return _sanitize_transport_claims(
        _normalise_publish_title(raw, topic.get("title", "未命名片段")),
        evidence_lines,
    )

def _parse_final_title_candidates(response, topics):
    payload = _extract_json_payload(response)
    raw_items = payload.get("topics", []) if isinstance(payload, dict) else []
    items_by_id = {}
    for item in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            item_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if 1 <= item_id <= len(topics) and item_id not in items_by_id:
            items_by_id[item_id] = item

    result = {}
    for item_id, topic in enumerate(topics, 1):
        generated = (items_by_id.get(item_id) or {}).get("candidates") or []
        values = [
            topic.get("publish_title"),
            *_clip_candidate_reference_publish_titles(topic),
            *generated,
        ]
        options = []
        for value in values:
            title = _normalise_final_title_option(value, topic)
            if title and title not in options:
                options.append(title)
        if not options:
            raise LLMStructuredOutputError(f"标题生成缺少 id={item_id} 的有效方案")
        result[item_id] = options[:6]
    return result

def _build_final_title_judge_prompt(
        topics, candidates_by_id, streamer_name=None, compact=False):
    """由独立终审比较原题和新方案，不默认偏爱任何一方。"""
    payload = _final_title_review_payload(topics, compact=compact)
    for item in payload:
        item["title_options"] = candidates_by_id.get(item["id"], [])
    context = _prompt_context(
        streamer_name,
        context_text=json.dumps(payload, ensure_ascii=False),
        compact=compact,
    )
    return _render_final_title_judge_prompt(
        _FinalTitlePromptEvidence(
            context=context,
            topics=tuple(payload),
        )
    )

def _parse_final_title_judgement(response, topics):
    payload = _extract_json_payload(response)
    raw_items = payload.get("topics", []) if isinstance(payload, dict) else []
    items_by_id = {}
    for item in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            item_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if 1 <= item_id <= len(topics) and item_id not in items_by_id:
            items_by_id[item_id] = item
    result = {}
    for item_id, topic in enumerate(topics, 1):
        item = items_by_id.get(item_id)
        title = _normalise_final_title_option(
            item.get("publish_title") if item else None,
            topic,
        )
        if not title:
            raise LLMStructuredOutputError(f"标题终审缺少 id={item_id} 的有效标题")
        reason = re.sub(r'\s+', ' ', str(item.get("reason", ""))).strip()[:240]
        result[item_id] = {"title": title, "reason": reason}
    return result

def review_selected_publish_titles(
        topics, streamer_name=None, progress_callback=None,
        checkpoint_callback=None):
    """对最终入选片段执行标题生成与独立终审，人工锁定项不参与改写。"""
    selected = [
        topic for topic in topics or []
        if (
            topic.get("can_slice")
            and topic.get("clip_review_validated") is True
            and not topic.get("publish_title_locked")
            and not topic.get("title_review_validated")
        )
    ]
    for topic in topics or []:
        if topic.get("publish_title_locked"):
            topic["title_review_validated"] = True
            topic["title_review_reason"] = "人工复审标题已锁定"
    if not selected:
        return None

    batches = [
        selected[offset:offset + TITLE_REVIEW_BATCH_SIZE]
        for offset in range(0, len(selected), TITLE_REVIEW_BATCH_SIZE)
    ]
    report_progress = _serialized_title_review_progress_callback(progress_callback)
    retry_coordinator = llm_gateway.LLMProviderRetryCoordinator()
    profile_snapshot = current_streamer_profile()

    def review_batch(batch_index, batch):
        with streamer_profile_context(profile_snapshot):
            if report_progress:
                report_progress(
                    f"投稿标题候选生成 ({batch_index}/{len(batches)})...",
                    96,
                    100,
                )
            generation_prompt = _build_final_title_generation_prompt(
                batch,
                streamer_name=streamer_name,
            )
            generation_response = llm_gateway.call_llm_with_retry(
                generation_prompt,
                compact_prompt=_build_final_title_generation_prompt(
                    batch,
                    streamer_name=streamer_name,
                    compact=True,
                ),
                require_json=True,
                progress_callback=report_progress,
                progress_label="投稿标题候选生成",
                progress_step=96,
                retry_coordinator=retry_coordinator,
                reasoning_stage="review",
            )
            candidates = _parse_final_title_candidates(generation_response, batch)
            if report_progress:
                report_progress(
                    f"投稿标题独立终审 ({batch_index}/{len(batches)})...",
                    96,
                    100,
                )
            judge_prompt = _build_final_title_judge_prompt(
                batch,
                candidates,
                streamer_name=streamer_name,
            )
            judge_response = llm_gateway.call_llm_with_retry(
                judge_prompt,
                compact_prompt=_build_final_title_judge_prompt(
                    batch,
                    candidates,
                    streamer_name=streamer_name,
                    compact=True,
                ),
                require_json=True,
                progress_callback=report_progress,
                progress_label="投稿标题独立终审",
                progress_step=96,
                retry_coordinator=retry_coordinator,
                reasoning_stage="review",
            )
            return candidates, _parse_final_title_judgement(judge_response, batch)

    warnings = []
    concurrency = min(_configured_title_review_concurrency(), len(batches))
    with ThreadPoolExecutor(
            max_workers=max(1, concurrency),
            thread_name_prefix="autoslice-title-review") as executor:
        jobs = [
            (index, batch, executor.submit(review_batch, index, batch))
            for index, batch in enumerate(batches, 1)
        ]
        for batch_index, batch, future in jobs:
            for topic in batch:
                topic["title_review_attempts"] = int(
                    topic.get("title_review_attempts", 0)
                ) + 1
            try:
                candidates, judgements = future.result()
            except Exception as exc:
                warnings.append(
                    f"第{batch_index}批标题终审失败：{_short_llm_error(exc)}"
                )
            else:
                for item_id, topic in enumerate(batch, 1):
                    judgement = judgements[item_id]
                    topic["publish_title"] = judgement["title"]
                    topic["title_review_candidates"] = candidates[item_id]
                    topic["title_review_validated"] = True
                    topic["title_review_reason"] = judgement["reason"]
                    topic["publish_title_source"] = "ai_title_judge"
            if checkpoint_callback:
                checkpoint_callback(topics, batch_index, len(batches))

    if not warnings:
        return None
    return "投稿标题终审未全部完成，失败项保留候选复核标题：" + "；".join(warnings)
