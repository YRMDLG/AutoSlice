"""LLM 话题正文清洗、points 规范化与证据约束。"""

import re

from autoslice.analysis.topic import titles as title_analysis

FACADE_EXPORTS = {
    "_DANMAKU_META_KEYWORDS": "DANMAKU_META_KEYWORDS",
    "_FRAGMENT_BODY_LINES": "FRAGMENT_BODY_LINES",
    "_META_BODY_KEYWORDS": "META_BODY_KEYWORDS",
    "_UNSUPPORTED_AI_AUDIENCE_REACTION_RE": "UNSUPPORTED_AI_AUDIENCE_REACTION_RE",
    "_clean_body_content": "clean_body_content",
    "_filter_unsupported_ai_points": "filter_unsupported_ai_points",
    "_is_meta_body_line": "is_meta_body_line",
    "_json_points_to_body": "json_points_to_body",
    "_normalise_body_line": "normalise_body_line",
}


META_BODY_KEYWORDS = (
    "但注意", "注意：", "注意:", "我们需要", "我们应该", "我应该", "我倾向", "是否应该",
    "输出格式", "输出如下", "不要输出", "程序会自动", "允许时间范围", "当前分块",
    "时间范围", "时间戳必须", "格式：", "格式`", "根据原则", "指令说", "题目说", "不能假设",
    "只需输出", "只需要输出", "最后，检查", "Markdown代码块", "Markdown 代码块",
    "这里有一段字幕", "后面没有字幕", "所以我们", "因此，输出", "考虑一下",
    "例如：", "例如:", "由于弹幕密度", "因为弹幕密度", "弹幕密度", "全场平均", "本段无峰值",
    "所以输出", "现在写输出", "要点要写", "所以整理信息", "所以话题标题", "目标风格",
    "但具体", "具体有哪些点", "从字幕中提取", "再看弹幕信息", "弹幕反应？", "没有具体弹幕内容",
    "不加✂️", "加✂️", "可能是", "似乎", "或许", "我们可以", "最好合并", "时间太短",
    "内容要点", "我们还需要考虑", "其他可能性", "可以提一句", "由于字幕", "所以只有一个话题",
    "根据格式", "如果有礼物", "才用●", "不用写", "如果无明显话题", "没有弹幕爆点",
    "弹幕爆点信息", "无爆点", "弹幕高能", "密度达", "峰值", "弹幕信息", "低于平均",
    "高于平均", "不活跃", "这里有明显话题", "最后，如果", "尽量简洁",
    "只能写基于字幕", "基于字幕", "标题可以", "标题更简洁", "优先简洁",
    "现在写", "我决定", "决定输出", "注意起始时间", "弹幕高密度",
    "要点用", "没有特别弹幕爆点", "这里没有明显", "需要确保", "有依据",
    "很好地覆盖", "再检查", "写要点", "最终答案", "规则要求", "按照示例",
    "很难分开", "检查要求", "要点2", "可以考虑更具体", "也可以分", "也许可以写",
    "更符合实际", "原字幕没有说完", "忠实于数据", "不符合常识", "每条对应真实时间",
    "字幕未显示", "我们谨慎", "我们写", "可以不用●",
    "看第二段", "第一段", "第二段", "第三段", "第四段", "同样，", "同样，1:",
    "我们说", "这显然", "时间重叠", "重新组织", "按时间顺序梳理", "接着在",
    "从“", "开始到", "我们取到", "最好重新", "约4:", "约3:", "约2:", "约1:",
    "根据字幕", "话题可以", "话题划分", "可能的划分", "通常做法", "首先，决定",
    "分析字幕内容", "从内容看", "关键词", "不能输出", "建议3个话题", "建议3个",
    "考虑时间顺序", "考虑实际讲话内容", "第五段", "时间轴整合", "自然分段",
    "注意1:", "允许时间", "超出范围", "我们尽量", "有很多讲话",
    "考虑分成", "考虑输出", "输出两个话题", "更好的方式", "更合理", "更合理的是", "更合理地", "然后紧接着",
    "我们仔细分析", "仔细分析每个时间段", "提取可理解", "这里明显", "我可以这样",
    "可以这样", "整体上，这是", "第二个话题", "第一个话题", "标题：", "标题:",
    "整个分块", "前部分", "我们只能", "不能用", "超过", "最后一段开始",
    "首先，覆盖", "覆盖从", "要注意", "直接输出最终条目", "最好基于时间顺序",
    "基于时间顺序整理", "建议这样划分", "子部分：", "子部分:", "字幕原文",
    "但内容不确定", "写具体", "从语义看", "可以作为一个整体话题", "为了简洁",
    "注意，我们", "分话题", "建议分成以下", "字幕分析", "总体来说",
    "比较好的做法", "我建议", "我考虑", "我们也可以", "但中间有间隔",
    "我们还需要写出具体要点", "让我们详细解析", "提取关键点", "可能游戏相关",
    "先理解字幕", "基于此", "要点要具体", "要点内容要具体", "思考如何写",
    "输出中不要", "更精确", "我们可用", "话题一", "话题二", "可能的话题",
    "大致内容", "评论文本", "原文：", "原文:", "整体来看", "注意时间戳",
    "可能的整理", "不合要求", "第三个短", "主要内容:", "主要内容：",
    "第一part", "第二part", "部分:", "部分：",
    "最佳方式", "我们仔细看", "时间线变化", "我们分析", "有哪些连续讲话",
    "规划话题结构", "输出时不要写Part", "现在我们来组织", "字幕内容:",
    "字幕内容：", "一个合理的方法", "合理的方法", "实际上，看字幕文本",
    "观察事件",
    "现在规划", "可能的最佳划分", "最佳划分", "这样就", "具体分段",
    "梳理字幕", "连续意思", "输出最终条目", "让我们仔细整理", "读懂字幕",
    "具体要点", "比如：", "比如:", "我认为合理的划分", "我们可能还需要涵盖",
    "然后要点", "话题A", "话题B",
    "这部分明显", "继续讨论这个视频", "继续这段剧情", "總結話題",
    "总结话题", "根據字幕", "根据字幕", "我認為", "我认为", "可以劃分",
    "可以划分", "劃分為", "划分为", "输出内容要严格按照格式", "严格按照格式",
    "标题加emoji", "最终输出", "礼物、弹幕爆点", "确保时间戳",
    "让我们仔细构建", "最终输出示例", "注意称呼", "如果有）",
    "points:", "points：", "title:", "title：", "重新考虑分块内容",
    "我们先把内容分几个话题", "那么我们定义", "整体时间段",
    "让我们尝试提取话题", "我们确保每个话题",
    "我们仔细阅读字幕", "整体看", "我们试着划分", "可能乱码",
    "后面还有", "这些时间段有重叠", "观察内容", "更仔细看",
    "划分建议", "我们还须注意", "先构思", "topic1", "topic2",
    "我们规划话题", "仔细看字幕", "先考虑can", "建议分成两个话题",
    "最终JSON", "最终 JSON", "先整理出具体的时间段", "查看字幕时间戳",
    "注意时间有重叠", "根据人工时间轴", "再分析字幕", "我们尝试解读字幕",
    "can_slice", "points", "\"topics\"", "\"start\"", "\"end\"", "\"title\"",
    "人工时间轴参考", "观察时间戳", "需要写点", "我们看内容",
    "我们来看内容", "对于话题", "根据内容推断边界", "我们看字幕的时间戳",
    "这些人工时间轴", "与上一段有重叠", "其他话题", "另一个思路",
    "我计划", "虽然弹幕低", "必须整理", "考虑话题", "提示说",
    "不需要特别重视", "可以作为参考", "所以生成JSON", "我们整理一下",
    "根据要求", "我们考虑", "先仔细解析字幕", "一个合理的划分",
    "我们来做分析", "我们来确定话题", "从人工时间轴和字幕",
    "输出JSON模板", "可能的切分", "或者：",
)


FRAGMENT_BODY_LINES = {
    "要点", "补充细节", "具体要点", "另一个事件", "例如", "例如：", "例如:", "等等。", "等等",
    "内容要点", "内容要点：", "内容要点:", "输出", "主播", "加盟商", "店主", "连麦者",
    "但", "但是", "然后", "因为", "所以", "因此", "不过", "最后", "另外", "同时", "继续",
    "现在规划", "具体要点", "具体要点：", "具体要点:", "比如", "比如：", "比如:",
    "points", "points:", "points：", "title", "title:", "title：", "要点", "要点：", "要点:",
    "更好的划分", "更好的划分：", "那么我们定义", "那么我们定义：", "整体时间段", "整体时间段：",
    "观察内容", "观察内容：", "更仔细看", "更仔细看：", "划分建议", "划分建议：",
    "整体看", "整体看，内容涉及：", "我们试着划分", "我们试着划分：",
    "我们规划话题", "我们规划话题：", "仔细看字幕", "仔细看字幕：",
    "先考虑can", "先考虑can：", "最终JSON", "最终 JSON", "最终 JSON：",
    "根据人工时间轴", "根据人工时间轴：", "再分析字幕详细内容", "再分析字幕详细内容：",
    "人工时间轴参考", "人工时间轴参考：", "观察时间戳", "观察时间戳：",
    "需要写点", "需要写点：", "我们看内容", "我们看内容：",
    "我们来看内容", "我们来看内容：", "我们看字幕的时间戳", "我们看字幕的时间戳：",
    "其他话题", "其他话题：", "另一个思路", "另一个思路：",
    "我计划", "我计划：", "所以生成JSON", "所以生成JSON：",
    "根据要求", "根据要求，", "我们考虑", "我们考虑：",
    "先仔细解析字幕", "先仔细解析字幕：", "一个合理的划分", "一个合理的划分：",
    "我们来做分析", "我们来做分析：", "我们来确定话题", "我们来确定话题。",
    "输出JSON模板", "输出JSON模板：", "可能的切分", "可能的切分：",
    "或者", "或者：",
    "弹幕/礼物高光", "弹幕礼物高光", "…", "...", "……",
}


DANMAKU_META_KEYWORDS = (
    "弹幕反应平静", "无爆点", "弹幕高能", "密度达", "峰值", "全场平均", "低于平均", "高于平均",
    "弹幕倍数", "弹幕信息", "弹幕爆点信息", "没有弹幕爆点", "不活跃", "反应不活跃",
    "弹幕高密度", "反应活跃", "可能弹幕", "字幕未显示", "我们谨慎",
    "弹幕互动平淡", "观众反应较少", "弹幕较少", "观众活跃度不高",
)


UNSUPPORTED_AI_AUDIENCE_REACTION_RE = re.compile(
    r'(?:'
    r'(?:观众|弹幕).{0,18}(?:刷屏|刷|直呼|调侃|笑称|齐刷|赞叹|赞|起哄|反应活跃|疯狂|热闹|沸腾|炸锅)'
    r'|(?:现场|全场|气氛).{0,10}(?:热烈|活跃|沸腾|炸锅|爆笑|高涨)'
    r')'
)


def is_meta_body_line(line):
    """过滤模型思考过程、规则复述、弹幕密度解释和占位半句。"""
    raw = line.strip()
    clean = title_analysis._strip_body_prefix(line)
    if not clean:
        return True
    if "```" in clean:
        return True

    normalized = clean.strip(' （）()[]【】「」『』：:。；;，,、.!！?？')
    if re.fullmatch(r'\[?\d{1,2}:\d{2}(?::\d{2})?\s*', clean):
        return True
    if clean.startswith(("字幕核查：", "字幕核查:", "弹幕依据：", "弹幕依据:", "切片核心：", "切片核心:")):
        return False
    if clean.startswith(("“", "”", "\"", "‘", "'")) and ("– 说" in clean or "- 说" in clean or len(clean) > 80):
        return True
    if "->" in clean:
        return True
    if clean in FRAGMENT_BODY_LINES or normalized in FRAGMENT_BODY_LINES:
        return True
    if clean.startswith((
        "标题：", "标题:", "第一个话题", "第二个话题", "第三个话题", "字幕原文",
        "话题一", "话题二", "话题三", "話題1", "話題2", "話題3",
        "第一part", "第二part", "第三个短", "{", "}", '"topics"',
        '"start"', '"end"', '"title"', '"can_slice"', '"points"',
    )):
        return True
    if re.match(r'^(points|title)\s*[:：]', clean, re.IGNORECASE):
        return True
    if clean.startswith((
        "首先，覆盖", "覆盖从", "要注意", "注意字幕", "然后从", "另外，前部分", "整个分块",
        "注意最后一段", "更好的方式", "更合理", "其实我们最好", "建议这样",
        "子部分", "从语义看", "为了简洁", "注意，我们", "字幕分析", "总体来说",
        "比较好的做法", "我建议", "我考虑", "我们也可以", "但中间有间隔",
        "让我们详细解析", "先理解字幕", "基于此", "要点要具体", "要点内容",
        "思考如何写", "输出中不要", "更精确", "我们可用", "可能的话题",
        "大致内容", "从字幕看", "整体来看", "注意时间戳", "可能的整理",
        "主要内容", "部分:", "最佳方式", "我们仔细看", "我们分析", "输出时不要写Part",
        "现在我们来组织", "字幕内容", "一个合理的方法", "实际上，看字幕文本",
        "观察事件", "现在规划", "可能的最佳划分", "具体分段", "梳理字幕",
        "输出最终条目", "让我们仔细整理", "读懂字幕", "具体要点", "比如",
        "我认为合理的划分", "我们可能还需要涵盖", "然后要点",
        "这部分明显", "继续讨论这个视频", "继续这段剧情", "總結話題",
        "根據字幕", "根据字幕", "输出内容要严格按照格式", "标题加emoji",
        "最终输出", "礼物、弹幕爆点", "确保时间戳", "让我们仔细构建",
        "最终输出示例", "注意称呼", "由于是主播自言自语",
        "重新考虑分块内容", "我们先把内容分几个话题", "那么我们定义",
        "整体时间段", "让我们尝试提取话题", "我们确保每个话题",
        "我们仔细阅读字幕", "整体看", "我们试着划分", "这些时间段有重叠",
        "观察内容", "更仔细看", "划分建议", "我们还须注意", "先构思",
        "我们规划话题", "仔细看字幕", "先考虑can", "建议分成两个话题",
        "或者可以合并", "最终 JSON", "最终JSON", "先整理出具体的时间段",
        "查看字幕时间戳", "注意时间有重叠", "根据人工时间轴", "再分析字幕",
        "我们尝试解读字幕",
        "人工时间轴参考", "观察时间戳", "需要写点", "我们看内容",
        "我们来看内容", "对于话题", "根据内容推断边界", "我们看字幕的时间戳",
        "这些人工时间轴", "与上一段有重叠", "其他话题", "另一个思路",
        "我计划", "虽然弹幕低", "必须整理", "考虑话题", "提示说",
        "不需要特别重视", "可以作为参考", "所以生成JSON", "我们整理一下",
        "根据要求", "我们考虑", "先仔细解析字幕", "一个合理的划分",
        "我们来做分析", "我们来确定话题", "从人工时间轴和字幕",
        "输出JSON模板", "可能的切分", "或者",
    )):
        return True
    if re.match(r'^topic\d+\s*[:：]', clean, re.IGNORECASE):
        return True
    if re.match(r'^\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*["“”]', clean):
        return True
    if re.match(r'^["“”]?(start|end|title|can_slice|points|topics)["“”]?\s*[:：]', clean, re.IGNORECASE):
        return True
    if clean in {"{", "}", "[", "]", "},", "],", "{"}:
        return True
    if re.match(r'^\[\d{1,2}:\d{2}(?::\d{2})?\s*/\s*\d{4}-\d{2}-\d{2}', clean):
        return True
    if re.match(r'^\d+[.)、]\s*(聊|讨论|观看|感谢|游戏|生日)', clean):
        return True
    if re.match(r'^\d+\.\s*\d{1,2}:\d{2}', clean):
        return True
    if re.match(r'^\d{1,2}:\d{2}(?::\d{2})?\s*[-－]\s*\d{1,2}:\d{2}', clean):
        return True
    if re.match(r'^\d{1,2}:\d{2}(?::\d{2})?\s*[（(]', clean):
        return True
    if re.match(r'^\d{1,2}:\d{2}(?::\d{2})?\s*[：:]', clean):
        return True
    if re.match(r'^\d+[.)、]\s*', clean) and re.search(r'(表演|评论|讨论|话题|話題|游戏|感谢|朗读|观看|吐槽|礼物)', clean):
        return True
    if clean.startswith(("然后", "从")) and re.search(r'\d{1,2}:\d{2}(?::\d{2})?', clean):
        return True
    if "##" in clean or "规划话题结构" in clean:
        return True
    if clean.startswith("[开始") or clean.startswith("开始－结束") or clean.startswith("开始-结束"):
        return True
    if re.match(r'^\d+[.、]\s*', clean) and re.search(r'\d{1,2}:\d{2}(?::\d{2})?\s*[-－]\s*\d{1,2}:\d{2}', clean):
        return True
    if re.match(r'^\d+[.、]\s*', clean) and re.search(
            rf'(话题|話題|关于|讨论|{title_analysis._streamer_role_pattern("主播")}|弹幕|感谢|游戏|时间|内容)',
            clean):
        return True
    if re.match(r'^(话题|話題|第[一二三四五六七八九十]+段|第\d+段)\s*\d*[:：]', clean):
        return True
    if re.search(r'\d{1,2}:\d{2}(?::\d{2})?\s*[-－]\s*\d{1,2}:\d{2}', clean) and re.search(r'(话题|时间|开始|结束|取到|部分|阶段)', clean):
        return True
    if re.search(r'\d{1,2}:\d{2}(?::\d{2})?\s*[-－]\s*\d{1,2}:\d{2}', clean) and re.search(r'(注意|但是|但|我们|考虑|更好|更合理|然后|这里|标题|划分|输出|合并)', clean):
        return True
    if re.match(r'^\d{1,2}:\d{2}(?::\d{2})?\s*(?:开始|继续)', clean) and re.search(r'(评论文本|讨论|抱怨|感谢|开始)', clean):
        return True
    # 被 max_tokens 截断时常出现“·主播”“·加盟商”“·但”这类无法独立理解的半句。
    if len(normalized) <= 3 and normalized in {"主播", "观众", "弹幕", "店主", "对方", "加盟商", "但", "输出"}:
        return True
    # ● 只保留礼物、观众金句等具体事件；泛泛的弹幕强弱/密度判断不进报告。
    if raw.startswith("●") and any(keyword in clean for keyword in DANMAKU_META_KEYWORDS):
        return True
    if any(keyword in clean for keyword in META_BODY_KEYWORDS):
        return True
    if re.search(r'(应该|不应该|可以只输出|是否|格式|指令|原则|分块|代码块|我们可以|所以输出|要点要写|具体有哪些点)', clean) and (
        clean.startswith(("但", "另外", "所以", "因此", "这里", "如果", "最后", "检查", "考虑"))
        or "我们" in clean
    ):
        return True
    if clean.startswith(("但", "但是", "不过", "所以", "因此", "此外", "按照", "检查", "现在", "这里", "因为", "另外", "也许", "也可以", "为了")) and re.search(
        r'(规则|要求|字幕|依据|输出|话题|标题|要点|检查|示例|时间|数据|写|分成|可以|覆盖|常识)',
        clean,
    ):
        return True
    if clean.startswith(("所以", "另外", "因此", "现在", "再看")) and re.search(r'(输出|整理|标题|弹幕|要点|具体|密度)', clean):
        return True
    if re.match(r'^(弹幕|密度|由于弹幕|因为弹幕)[:：]', clean):
        return True
    return False


def clean_body_content(line):
    """保留有效信息，同时去掉模型常见的总结式开头。"""
    clean = title_analysis._strip_body_prefix(line)
    clean = re.sub(r'^(?:所以整体是|大致内容[:：]?|主要内容[:：]?|首先[，,]\s*)', '', clean).strip()
    clean = re.sub(r'^[\"“”](.*?)[\"”]?\s*,?$', r'\1', clean).strip()
    clean = re.sub(r'^内容有些混乱[，,。；;：:但是\s]*', '', clean).strip()
    clean = re.sub(r'^但是可以归纳出话题[:：]?', '', clean).strip()
    clean = re.sub(r'^可以归纳出话题[:：]?', '', clean).strip()
    clean = re.sub(r'^要点\s*[:：]\s*', '', clean).strip()
    clean = re.sub(r'^这段(?:讨论|继续解释|继续)?', '', clean).strip()
    return title_analysis._normalise_obvious_report_terms(clean)


def normalise_body_line(line):
    """规范正文要点前缀，让报告接近人工时间轴。"""
    raw = line.strip()
    line = clean_body_content(raw)
    if not line or is_meta_body_line(line):
        return ""
    if raw.startswith("●"):
        return "●" + line
    return "·" + line


def json_points_to_body(points):
    """把 JSON points/body 字段转换成报告正文要点。"""
    if points is None:
        return []
    if isinstance(points, str):
        raw_items = [line for line in re.split(r'[\r\n]+', points) if line.strip()]
    elif isinstance(points, (list, tuple)):
        raw_items = []
        for item in points:
            if isinstance(item, (list, tuple)):
                raw_items.extend(str(sub) for sub in item)
            else:
                raw_items.append(str(item))
    else:
        raw_items = [str(points)]
    body_lines = [normalise_body_line(item) for item in raw_items]
    return [line for line in body_lines if line]


def filter_unsupported_ai_points(points):
    """弹幕密度不能证明具体弹幕内容，过滤模型自行补写的观众反应。"""
    return [
        line for line in points or []
        if not UNSUPPORTED_AI_AUDIENCE_REACTION_RE.search(title_analysis._strip_body_prefix(line))
    ]
