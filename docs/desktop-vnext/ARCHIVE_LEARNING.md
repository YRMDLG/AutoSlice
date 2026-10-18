# 学习旧切片：框架、数据约定与接手待办

> 作用：让工具从用户本机保留的**全部历史切片**里学习（校对习惯、术语、标题口吻、封面风格），并且让后续接手的人（GPT / 别的开发者）能在不碰用户原始数据的前提下继续优化。
> 代码在 `src/autoslice/desktop/archive/`，设置页面板在 `qt_app/archive_panel.py`，决策见 [D-031](DECISIONS.md)。

## 1. 边界（不能破）

1. **只读**：不修改、不移动、不删除档案文件夹里的任何文件，也不往里面写任何东西。产物全在 `%LOCALAPPDATA%\AutoSlice\learning\archive\`。测试里有“扫描前后整棵档案树逐文件对比”的用例守着。
2. **只在本机**：不联网、不上传。需要交给 GPT 时，只交 `digest.json`（统计 + 少量样本，有体积上限，不含视频、字幕全文和完整路径；纠错样例里带每条 ≤80 字的字幕片段，标题是原样标题）。
3. **学到的东西要用户确认才生效**：扫描只产生“建议”，用户在设置页勾选后才写进词库 / 术语表 / 标题范例 / 字幕阈值。
4. **不打开视频**：扫描只看文件名、大小、修改时间；读字幕（小文件）和旧封面的小缩略图。
5. **随时可清除**：设置页“清除学到的内容”或 `forget --yes` 删掉整个 `learning/archive/`（不动档案，也不撤销已写进覆盖词库的固定纠错）。

## 2. 怎么用

界面：设置 → “学习旧切片”→ 添加存放旧切片的文件夹（可多个）→ 开始扫描（可停、可续）→ 查看报告 / 学到的建议。

命令行（在有旧切片的那台机器上，建议先 `inspect`）：

```
python -m autoslice.desktop.archive inspect D:\切片        # 只读：看文件夹怎么被认出来、没被认出来的里面是什么后缀
python -m autoslice.desktop.archive scan D:\切片 --limit 200 # 先试跑 200 个
python -m autoslice.desktop.archive scan D:\切片             # 全量；可 Ctrl+C，下次接着扫
python -m autoslice.desktop.archive proposals                # 看学到了什么
python -m autoslice.desktop.archive apply --kinds replacement,titles,subtitle_limits
python -m autoslice.desktop.archive report                   # 用库里已有的数据重新生成报告
```

**交接给 GPT 的方式**：跑完 `scan` 后把 `reports/digest.json`（几十 KB）贴给 GPT，并让它读本文件第 9 节的待办。GPT 看不到用户磁盘，所以它能做的是：基于 digest 判断规律、改抽取器/报告/建议的代码、写测试；改完由用户在本机重跑 `scan --force` 看效果。

## 3. 数据布局假设（`layout.py` / `model.py` 常量）

| 内容 | 识别方式 |
|---|---|
| 投稿文件夹 | 含成片视频或字幕的文件夹；标题 = 文件夹名；递归，最深 10 层，跳过回收站、`.git`、隐藏目录和符号链接 |
| 成片 | `.mp4 .mkv .mov .flv .avi .webm .ts .m4v .wmv`，且主名不以生成后缀结尾 |
| 字幕初稿 | 与成片同名的 `.srt`；只有一个成片和一份字幕时名字对不上也配对 |
| 校对稿 | `<初稿主名>_校对.srt`（也认 `_校对字幕`） |
| 字幕版成片 | `<成片主名>_字幕版.<视频后缀>` |
| 封面候选 | 同目录的图片；名字带 封面/cover/autocover/thumb/缩略图 的优先，其余按大小；最多 3 张 |
| 笔记 | 同目录 `.txt/.md`（目前只记录，不读取） |
| 日期 | 标题里的 `2024年10月3日 / 20241003 / 10月3日`；否则成片修改时间 |
| 主播 | 主播档案（标题前缀、称呼、路径关键词）；没有档案的按标题开头的【名字】归为 `ext:名字` |
| 只有字幕没有成片的老文件夹 | 用字幕当“视频”记下来，字幕里的信息照样学 |

**旧数据命名不一样怎么办**：先 `inspect` 看“没被认成投稿文件夹里的后缀”和样例；然后只改 `model.py` 的常量（后缀、生成标记、封面关键词）或 `layout.classify_folder`，它是纯函数，`tests/unit/test_desktop_archive.py::LayoutTests` 用合成文件列表测，不需要真实磁盘。

## 4. 模块职责

| 模块 | 职责 |
|---|---|
| `model.py` | 数据类、常量、`EXTRACT_VERSION`、`ScanOptions` |
| `srt.py` | 宽容的 SRT 读取（UTF-8/16、GB18030、乱序号、格式标签），读不出来返回空 |
| `layout.py` | 文件夹 → `ArchiveEntry`（纯函数）；日期、主播推断；指纹 |
| `store.py` | SQLite（WAL），按文件夹存事实，汇总全靠 SQL 现算 |
| `extractors/` | 可插拔抽取器：`corrections` / `subtitles` / `terms` / `covers` |
| `scanner.py` | 遍历、增量（指纹+版本）、取消、续跑、容错、定期提交 |
| `report.py` | `report.json/.md`、`digest.json` |
| `proposals.py` | 建议的生成、状态（pending/accepted/rejected）、应用 |
| `knowledge.py` | 运行时取用：相关术语检索、相似标题检索、字幕阈值 |
| `service.py` | 界面和命令行共用的入口 |

## 5. 证据库（`archive.db`）

每个事实表的第一列都是 `entry_id`；某个文件夹变了，先删它在各表的旧行再写新行。

- `entries(id, root, folder, title, streamer, date, year, video_count, video_bytes, srt_count, corrected_count, burned_count, cover_count, signature, version, scanned, error)`
- `corrections(entry_id, wrong, right, n, kept, context)`：一个文件夹里某个（错词→正确词）改了几处（`n`）、兼容列 `kept`（写 0，不再读取）、一条原文例子
- `final_text(entry_id, text)`：每份有效校对稿的最终文本，用于按主播统计全部原样保留次数
- `edits(entry_id, kind, n)`：改动类型 same / replace / punctuation / delete_filler / delete_text / insert_text / linebreak / split / merge / other
- `terms(entry_id, term, n)`：候选术语（每个文件夹最多 80 个）
- `subtitle_stats(entry_id, source, cues, chars, seconds, cps_p50/p90/max, chars_p50/p90, line_p90, multiline, trailing_punct, short, long, overlap)`
- `covers(entry_id, path, width, height, bytes, brightness, saturation, palette, edge_top/mid/bottom, named)`
- `runs`、`roots`、`meta`

**加一个抽取器**（`extractors/base.py` 有说明）：继承 `Extractor`，写 `schema`（建表语句）、`tables`（表 → 列名，不含 entry_id）、`extract(entry, context)`；在 `extractors/__init__.py` 的 `DEFAULT_EXTRACTORS` 登记；输出含义变了就把 `model.EXTRACT_VERSION` 加一，旧条目下次扫描自动重抽。抽取器抛异常只记在该文件夹的 `error` 里，不会中断扫描。

## 6. 建议如何变成效果

2026-10-18 修复：精度按该主播全部有效校对稿统计，包含只剩校对稿的条目；仅有初稿不算认可保留。`precision = 此替换次数 /（同一错词所有替换次数 + 全部校对稿中原样保留次数）`，少数替换去向也进入分母。数据库版本与抽取版本均升级为 2，新增 `final_text(entry_id, text)` 保存本机最终字幕事实；旧 `corrections.kept` 列仅为兼容保留，不再作为统计依据。

升级只新增表并保留原数据，下次扫描会重抽旧条目。某主播仍有旧版本或读取失败的条目时，报告会标明语料尚未补齐，并暂停该主播的固定纠错建议；完成重扫后恢复。报告和 digest 的版本也升级为 2，增加证据完整状态。首次升级重扫后，未改动条目仍会增量跳过。

| 建议 | 生成条件 | 确认后写到哪 | 运行时谁用 |
|---|---|---|---|
| 固定纠错 | ≥3 个视频、语料升级完整、**精度 ≥0.8**（此替换次数 /（所有替换次数 + 全部原样保留））、不在词库里 | 本机覆盖词库（受“每个主播 ≤100 条”约束） | AI 字幕检查的“固定纠错映射” |
| 术语 | ≥5 个视频、不是更长词的碎片、不是普通词、跨主播扩散 ≤2、不在术语表里 | `knowledge/glossary.json`（无 200 条上限） | AI 字幕检查：只带和当前字幕有关的几十个 |
| 标题范例 | ≥10 个该主播标题 | `knowledge/titles.json` | 投稿文案：挑和当前视频最像的几个，再补当前目录里的 |
| 字幕阈值 | ≥20 个视频 | `knowledge/habits.json`（读速、一条字数、单行字数，取各视频 90 分位的 90 分位 ×1.1） | 字幕质量提醒（学过才启用） |

精度这条来自真实数据：用户把一处“意思”改成了“ins”，如果把“意思→ins”当固定纠错，之后每个“意思”都会被改坏。**现有的 `correction_memory`（保存校对时学错词）没有这道检查**，见待办 P1。

## 7. 性能预算与实测

合成 4000 个文件夹 × 60 条字幕（24 万条）：首扫 17 秒，复扫 0.8 秒（全部跳过），出报告 0.5 秒，生成建议 0.4 秒，库 21 MB。真实磁盘（尤其机械盘、网络盘）IO 会慢些；封面分析每张约几十毫秒。扫描在后台线程，随时可停；每 100 个变化的文件夹提交一次，崩溃最多丢一小批。

## 8. 已知局限（诚实清单）

1. **候选术语噪声大**：没有语料可对照“什么是普通词”，只靠 n-gram + 碎片过滤 + 小的普通词表（`stopwords.py`）。所以默认不勾选术语建议，要人挑；真正的过滤应交给大模型（P1）。
2. **单字替换的上下文碎片**：`correction_pairs` 对“只改一个字”会把前后各一个字带上（学到“聊非月→聊绯月”而不是“非月祭→绯月祭”），同一个词在不同句子里变成不同的对，凑不够“≥3 个视频”。需要词级对齐（P1）。
3. 字幕条数不同（拆分/合并）时只比较一对一的条目；时间被大幅挪动又条数相同时按位置配对。
4. 日期靠标题和成片修改时间，成片被复制过则不准；主播只认主播档案里配置的，其余按【名字】。
5. 一个文件夹多个成片时，字幕统计、术语是合并算的。
6. 封面特征很粗（主色、明暗、文字落点），只够聚类；看不懂人物位置、字的内容、版式类型。
7. 只处理 `.srt`；`.ass/.lrc/.json` 等没读。笔记文件只记录不读取。
8. 没有“监视新投稿自动入库”，需要手动点扫描。

## 9. 给 GPT 的待办（按优先级）

**P0 先在真实数据上跑通**
- `inspect` 看布局，修正命名不符（常量或 `classify_folder`），补对应的合成用例；`scan --limit 200` 试跑，把 `digest.json` 里“数据质量”那段的异常（没字幕、读取出错、认不出日期/主播）逐项解释并修。

**P1 学得更准**
- 词级对齐的纠错学习：用右侧（校对后）高频词当词边界，把单字替换扩展成“包含该字的整词”，替代现在的固定向两边各扩一个字；验收：同一个词在不同句子里合并成同一对。
- 把 `kept`/精度这道检查也加进 `correction_memory`（保存校对时学错词）：目前只要两个视频都改过就写进词库。
- 候选术语的大模型过滤：输入 digest 里的 terms（带 df、spread、示例），输出分类（人名 / 梗 / 外来词 / 普通词），只留前三类；写成一个可重复运行的脚本，结果回写成 `knowledge/glossary.json` 的排序依据。
- 外来词/英文热词（ins、emoji、get到、AI、QQ）：真实数据里这类词最容易被听成中文（“意思上面改”=“ins上面改”）；单独统计“被改成含英文的词”，作为 AI 检查的专项提示。
- 同音检测（本地、零成本的建议来源）：用 pypinyin 找“拼音相同/相近但字不同”的片段对照术语表。**需要用户同意安装 pypinyin**（纯 Python，约 1 MB）；做成可选依赖，没装时整段跳过。

**P2 标题与封面**
- 标题：识别系列（标题只差期数）→ 系列模板；统计“标题公式”（【主播】+ 起因 + 结果 + emoji）写进投稿文案提示词；用 embedding 或 BM25 替换现在的 n-gram 重叠检索（`Knowledge.title_examples`）。
- 封面：把 `covers` 特征聚类成“你的常用版式”，再对抽样（如 300 张）用看图大模型标注（人物位置、文字颜色/位置、版式类型），映射到 `cover_style.STYLE_PRESETS` 和主播档案的 `cover_rules.emphasis_terms`；新增 `extractors/cover_labels.py`，输出含义变了记得加 `EXTRACT_VERSION`。
- AI 建议反馈回灌：把应用数据里 `cache/ai/session-*.json` 的采纳/跳过记进库，统计“总被跳过的建议类型”，让 AI 检查少提、让建议队列按采纳概率排序。

**P3**
- 导入创作中心导出的播放量/点击率 CSV（用户自己导出，不登录任何站点），按标题对上，分析哪种标题/封面风格表现更好。
- 监视新投稿自动入库（文件夹变化 → 增量扫描）。

## 10. 契约稳定性

- `digest.json` 有 `kind`、`version`；字段变了就加版本。
- `knowledge/*.json` 有 `schema_version`，按主播 id 分桶。
- 数据库表结构变了加 `SCHEMA_VERSION` 并写迁移；v1 → v2 为新增最终文本表的幂等升级，保留已有事实并等待重新抽取。
- 新抽取器、新建议类型都要有合成档案的测试；不能依赖用户真实数据，也不能读写用户真实配置（测试里用 `AUTOSLICE_STREAMER_PROFILE_OVERRIDES` 指向临时文件）。
