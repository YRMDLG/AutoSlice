"""AutoCover vNext 本地基础文案提取回归。"""

from __future__ import annotations

from autoslice.desktop.cover_copy import generate_basic_copy_variants


def test_packaging_suspense_does_not_beat_real_event():
    title = "〖泽音〗音姐直播间突入神秘人物👀气的音姐大骂音悦生要他退钱！😰究竟发生了什么🤔"
    variants = generate_basic_copy_variants(title)

    assert variants
    assert "退钱" in variants[0].headline
    assert "究竟发生了什么" not in variants[0].text


def test_warning_can_be_a_single_strong_headline():
    title = "〖泽音〗警告：本视频请勿外放！音姐打《啊。啊。啊。》太隐晦了🥵"
    best = generate_basic_copy_variants(title)[0]

    assert "请勿外放" in best.headline
    assert not best.context


def test_final_reaction_is_kept_for_dialogue_title():
    title = (
        "〖泽音〗音悦生直播点鸭子😰"
        "“点鸭子你去菜场啊来我们这🤔”"
        "“原来是菜场啊 我以为是超市呢🤭”"
        "“我恨我秒懂🤬”"
    )
    variants = generate_basic_copy_variants(title)

    assert "秒懂" in variants[0].headline


def test_basic_candidates_never_exceed_two_semantic_blocks():
    title = (
        "〖泽音〗看大家给音姐弄大屏😮"
        "“感觉去线下得要更低调了😰”"
        "“穿洞洞鞋拿purax的是音音！🤩”"
        "“嘘嘘嘘！不要再说了”"
    )

    for candidate in generate_basic_copy_variants(title):
        assert candidate.headline
        assert candidate.text.count("\n") <= 1


def test_basic_mode_only_extracts_substrings_from_original_title():
    title = "〖糖&牛〗BW糖牛要打一架了👀音姐要狠狠爆牛哥口嗨之仇🤬结果可恶牛哥居然要下架切片员视频销毁证据😡坏事做尽！"

    for candidate in generate_basic_copy_variants(title):
        if candidate.context:
            assert candidate.context in title
        assert candidate.headline in title


def test_prefix_is_not_left_in_generated_copy():
    title = "〖泽音〗音悦生屁股这么翘，一定能顶瓶汽水吧"
    variants = generate_basic_copy_variants(title)

    assert variants
    assert all("〖泽音〗" not in candidate.text for candidate in variants)


def test_default_candidate_prefers_short_original_burst_over_suspense_suffix():
    title = "〖泽音〗直播间突入神秘人物气的音姐大骂音悦生要他退钱！究竟发生了什么"
    best = generate_basic_copy_variants(title)[0]

    assert "究竟发生了什么" not in best.text
    assert len(best.headline) <= 18
    assert "退钱" in best.text


def test_long_context_is_dropped_when_it_would_recreate_a_giant_text_box():
    title = "〖泽音〗苦主小音幻想音悦生以后对别人说天衣无缝情话😰音音心里就一阵心酸😭"
    best = generate_basic_copy_variants(title)[0]

    assert best.headline
    assert len(best.context) <= 20
    assert best.text.count("\n") <= 1


def test_slash_separated_failure_sample_stays_two_original_blocks():
    title = "音姐今天要给沐霂点男模 / 哎呀我还没见过男模啥样呢"
    best = generate_basic_copy_variants(title)[0]

    assert best.context == "音姐今天要给沐霂点男模"
    assert best.headline == "哎呀我还没见过男模啥样呢"
    assert best.context in title
    assert best.headline in title
