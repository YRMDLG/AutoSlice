"""识别结果的文本处理：标点挂回字、整句拆成均匀字、字错率。纯函数，没有外部依赖。"""

from __future__ import annotations

import re
import unicodedata

from .types import Piece, Word

_PUNCT = frozenset("，。！？、；：,.!?;:…—-“”\"'‘’（）()[]【】《》<>~·")
_SENTENCE_END = frozenset("。！？!?；;")


def is_punct(char: str) -> bool:
    return char in _PUNCT or unicodedata.category(char).startswith(("P", "S"))


def attach_punctuation(text: str, words: list[Word]) -> list[Word] | None:
    """把整句文本里的标点挂到前一个字上。字对不上（引擎改写了内容）返回 None，由调用方退回均匀分配。"""

    if not words:
        return []
    plain_text = "".join(char for char in text if not char.isspace() and not is_punct(char))
    plain_words = "".join(
        "".join(char for char in word.text if not char.isspace() and not is_punct(char))
        for word in words
    )
    if plain_text.casefold() != plain_words.casefold():
        return None
    # 按“已消耗的非标点字数”记标点，再挂到对应位置的字上
    marks: dict[int, str] = {}
    consumed = 0
    for char in text:
        if char.isspace():
            continue
        if is_punct(char):
            marks[consumed] = marks.get(consumed, "") + char
        else:
            consumed += 1
    result = []
    position = 0
    for word in words:
        size = len("".join(char for char in word.text if not char.isspace() and not is_punct(char)))
        position += size
        suffix = marks.get(position, "") if size else ""
        base = "".join(char for char in word.text if not char.isspace() and not is_punct(char)) or word.text
        result.append(Word(word.start, word.end, base + suffix))
    return result


def spread_words(piece: Piece) -> list[Word]:
    """引擎没给字级时间：把整句按字数均匀分到起止时间里。标点挂在前一个字上。"""

    tokens: list[str] = []
    # 中文一字一个单位，英文/数字连着算一个词（分段拼接时才不会被拆成“i n s”）
    for token in re.findall(r"[A-Za-z0-9]+|[^\sA-Za-z0-9]", piece.text):
        punctuation = len(token) == 1 and is_punct(token)
        if punctuation and tokens:
            tokens[-1] += token
        elif not punctuation:
            tokens.append(token)
    if not tokens:
        return []
    weights = [max(1, len("".join(char for char in token if not is_punct(char)))) for token in tokens]
    span = max(piece.end - piece.start, 0.01)
    unit = span / sum(weights)
    words, cursor = [], piece.start
    for token, weight in zip(tokens, weights):
        words.append(Word(cursor, cursor + weight * unit, token))
        cursor += weight * unit
    return words


def piece_words(piece: Piece) -> list[Word]:
    """一句话的字级时间：引擎给了就用（并把标点挂回去），否则均匀分配。"""

    if piece.words:
        attached = attach_punctuation(piece.text, list(piece.words))
        return attached if attached is not None else list(piece.words)
    return spread_words(piece)


def normalise_for_cer(text: str) -> str:
    """比较用：全角转半角、去标点空白、英文统一小写。字幕本来就没有标点，比内容不比格式。"""

    folded = unicodedata.normalize("NFKC", str(text)).casefold()
    return "".join(char for char in folded if not char.isspace() and not is_punct(char))


def edit_distance(left: str, right: str) -> int:
    """逐字的编辑距离（替换/插入/删除各算 1）。"""

    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, 1):
        current = [row]
        for column, right_char in enumerate(right, 1):
            current.append(min(
                previous[column] + 1,
                current[column - 1] + 1,
                previous[column - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]


def cer(reference: str, hypothesis: str) -> tuple[int, int]:
    """(错字数, 参考字数)：参考是你校对后的字幕，错字数就是你得手动改的字数。"""

    ref, hyp = normalise_for_cer(reference), normalise_for_cer(hypothesis)
    return edit_distance(ref, hyp), len(ref)
