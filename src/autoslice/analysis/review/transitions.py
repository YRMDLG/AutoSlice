"""边界转场识别与报告话题安全边界的唯一实现。"""

import math
import re

from autoslice.analysis.review import context_evidence
from autoslice.analysis.review import policy as clip_policy

TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE = clip_policy.TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE


FACADE_EXPORTS = {
    "_NEXT_CASE_ASR_TRIGGER_RE": "_NEXT_CASE_ASR_TRIGGER_RE",
    "_TOPIC_CONCLUSION_RE": "_TOPIC_CONCLUSION_RE",
    "_TOPIC_DECISION_EVIDENCE_RE": "_TOPIC_DECISION_EVIDENCE_RE",
    "_TOPIC_DISCOURSE_CONTINUATION_RE": "_TOPIC_DISCOURSE_CONTINUATION_RE",
    "_TOPIC_REFUND_RE": "_TOPIC_REFUND_RE",
    "_VISUAL_CASE_SHIFT_RE": "_VISUAL_CASE_SHIFT_RE",
    "_looks_like_delayed_topic_conclusion": "_looks_like_delayed_topic_conclusion",
    "_looks_like_discourse_continuation": "_looks_like_discourse_continuation",
    "_looks_like_low_score_visual_case_shift": "_looks_like_low_score_visual_case_shift",
    "_looks_like_next_case_transition": "_looks_like_next_case_transition",
    "_next_report_topic_safe_boundary": "_next_report_topic_safe_boundary",
}


_NEXT_CASE_ASR_TRIGGER_RE = re.compile(
    r'(?:再(?:看|能|给你)|来看|出给|给你|接着看|继续看)下(?:一(?:个)?|个)|'
    r'下一个(?:视频|话题|游戏|评价|差评|案例|商家|商品)?|'
    r'(?:看看|看一下)(?:他|她|商家|顾客|用户).{0,6}(?:说|写)(?:了)?什么|'
    r'(?:谁|有谁)记得(?:上次|之前).{0,20}(?:吗|嘛)?'
)


_TOPIC_DECISION_EVIDENCE_RE = re.compile(
    r'(?:判断|如何|是否|怎么办|怎么处理|结论|退款|退钱|退回|赔偿|补偿|换货)'
)


_TOPIC_CONCLUSION_RE = re.compile(
    r'(?:我觉得|所以|那就|这样(?:的话)?|应该|最终|最后|结论|总之|看来|结果|决定)'
    r'.{0,40}(?:可以|不可以|不行|不用|展示|通过|驳回|解决|处理|算了|'
    r'退款|退钱|退回|退掉|退了|赔偿|补偿|换货|保留|删除)|'
    r'(?:把|给).{0,20}(?:钱|款).{0,8}退(?:回|掉|了)|'
    r'(?:退款|退钱|退回|退掉|返钱)'
)


_TOPIC_REFUND_RE = re.compile(r'(?:退款|退钱|退回|退掉|退了|返钱|把.{0,20}钱.{0,8}退)')


_TOPIC_DISCOURSE_CONTINUATION_RE = re.compile(
    r'^(?:主要是|而且|然后|所以|但是|不过|就是|对(?:啊|呀|的)|确实|其实|'
    r'我想说|可怜|恭喜)|^.{0,16}(?:还(?:要|会|真|拿|点|给|说|有|在|数|是)|再补充)'
)


_VISUAL_CASE_SHIFT_RE = re.compile(
    r'(?:左边|右边).{0,30}(?:赠品|原厂|非原装|遥控器|商品|图片)|'
    r'(?:原厂|非原装).{0,20}(?:遥控器|商品)|'
    r'这两个.{0,12}(?:遥控器|商品|图片)'
)


def _looks_like_next_case_transition(text):
    """识别“再看下一个”及常见 ASR 误识别，避免吞入下一案例。"""
    compact = re.sub(r'\s+', '', text or "")
    return bool(_NEXT_CASE_ASR_TRIGGER_RE.search(compact))


def _looks_like_delayed_topic_conclusion(mark, text, term_counts):
    """识别与案由相符、但在短暂停顿后才说出的判断或退款结论。"""
    compact = re.sub(r'\s+', '', text or "")
    if not compact or not _TOPIC_CONCLUSION_RE.search(compact):
        return False
    evidence = re.sub(r'\s+', '', " ".join([
        str(mark.get("title", "")),
        str(mark.get("publish_title", "")),
        *[str(item) for item in mark.get("boundary_evidence") or []],
    ]))
    if _TOPIC_REFUND_RE.search(evidence) and _TOPIC_REFUND_RE.search(compact):
        return True
    if _TOPIC_DECISION_EVIDENCE_RE.search(evidence):
        return True
    return context_evidence._score_boundary_evidence_text(compact, term_counts) > 0


def _looks_like_discourse_continuation(text):
    """识别短暂停顿后以“主要是/还……”承接上一话题的补充句。"""
    return bool(_TOPIC_DISCOURSE_CONTINUATION_RE.search(
        re.sub(r'\s+', '', text or "")
    ))


def _looks_like_low_score_visual_case_shift(text, term_counts):
    """识别未说“下一个”、但画面和对象已明显切换的新案例。"""
    compact = re.sub(r'\s+', '', text or "")
    return bool(
        _VISUAL_CASE_SHIFT_RE.search(compact)
        and context_evidence._score_boundary_evidence_text(compact, term_counts)
        < TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE
    )


def _next_report_topic_safe_boundary(next_topic_start, topic_end, srt_segments):
    """下一话题时间落在字幕句内时，允许当前片段保留该句到句末。"""
    next_topic_start = int(next_topic_start)
    for seg_start, seg_end, _ in srt_segments or []:
        if seg_end <= topic_end:
            continue
        if seg_start >= next_topic_start:
            break
        if seg_start < next_topic_start < seg_end and seg_end - next_topic_start <= 10:
            return int(math.ceil(seg_end)), float(seg_start)
    return next_topic_start, None
