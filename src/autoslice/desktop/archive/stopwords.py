"""候选术语的“太普通了”过滤：口语里反复出现但不是主播专有说法的词。

这是个起步的小表，宁可漏过滤也别误杀专名；真正的“这个词算不算梗”之后可以交给大模型判断，
也可以往这个表里补词。以这些词开头、结尾或整词相同的候选会被丢掉。
"""

from __future__ import annotations

GENERIC_PARTS = frozenset("""
什么 怎么 这个 那个 一个 一下 一些 一样 没有 可以 不是 不要 不会 不能 不想 不知道 知道 我们 你们 他们 她们 它们 咱们
因为 所以 但是 然后 如果 还是 或者 而且 虽然 就是 就是说 已经 现在 时候 出来 起来 下来 上去 过来 过去 东西 这样 那样
自己 好像 应该 可能 其实 感觉 觉得 这种 那种 为什么 是不是 有没有 对不对 什么意思 真的 一直 一定 一起 开始 结果 比如
这里 那里 这边 那边 这么 那么 怎么样 怎么办 多少 几个 有点 有些 非常 特别 比较 还有 还要 只是 只有 就算 反正 本来
对吧 是吧 好吧 对啊 是啊 对对 哈哈 哈哈哈 嘿嘿 呵呵 啊啊 呃呃 嗯嗯 那就 这就 我就 你就 他就 然后呢 所以说 的话 的时候
""".split())


def is_generic(term: str) -> bool:
    """整词是普通词，或以普通词开头、结尾。"""

    if term in GENERIC_PARTS or len(set(term)) == 1:
        return True
    return any(term.startswith(part) or term.endswith(part) for part in GENERIC_PARTS if len(part) >= 2 and len(part) < len(term))
