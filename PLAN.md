# AutoSlice 智能话题分析 + 切片 v2 — 开发计划

## 目标

整合 video-topic-analyzer 的核心思路，在 AutoSlice 中新建完整的"话题分析 + 智能切片"功能。不影响现有弹幕切片和时间轴切片。

## 用户要求

1. 移植 skill 核心逻辑，适配 RTX 2060 6GB
2. 无 SRT 时自动用 FunASR 生成，有 SRT 直接读
3. 独立新功能，不修改现有的弹幕切片 / docx 时间轴切片
4. 弹幕密度 + LLM 话题分析结合，AI 标记哪些段该切，自动切片按报告执行
5. LLM 用 DeepSeek V4 Pro

## 架构

```
视频 .flv
    │
    ├─→ [1] FunASR → 生成 SRT（没有的话）
    ├─→ [2] 弹幕密度分析 → 标记高密度峰值
    ├─→ [3] SRT 分块 + 弹幕密度数据 → 送 DeepSeek Flash
    │        │
    │        └─→ 话题报告.md + clip_marks.json
    │                    │
    └─→ [4] 自动切片 ← 按 clip_marks.json 切
                    │
                    └─→ F:\Videos\自动切片\话题切片_xxx\
```

## 新文件 / 改动

| 文件 | 操作 | 说明 |
|------|------|------|
| `topic_engine.py` | 新建 | 核心引擎：FunASR → 弹幕分析 → LLM → 报告 |
| `templates/topic.html` | 重写 | 话题分析 + 切片一体的 Web 页面 |
| `app.py` | 新增路由 | `/api/start-topic-pipeline` 完整流水线 |
| `core.py` | 不改 | 保留现有弹幕切片和时间轴切片 |

## 实施步骤

### Phase 1: topic_engine.py 核心引擎
- [ ] FunASR 自动转录（复用 core.py generate_srt，CPU 模式兼容 2060）
- [ ] 弹幕密度峰值提取（复用 core.py find_dense_periods）
- [ ] SRT 分块（移植 skill 的 5 分钟分块策略）
- [ ] LLM prompt（结合弹幕密度数据，标记可切片段）
- [ ] DeepSeek V4 Pro API 调用
- [ ] 输出：话题报告.md + clip_marks.json

### Phase 2: Web 集成
- [ ] 新页面 `/topic-v2` 或重写 topic.html
- [ ] 流水线 API：一步完成全部
- [ ] 进度实时显示（SSE）
- [ ] 预览报告 + 一键切片按钮

### Phase 3: 自动切片
- [ ] 读取 clip_marks.json
- [ ] 调用现有 ffmpeg 切片逻辑
- [ ] 输出到子文件夹

### Phase 4: 测试
- [ ] 用实际录播测试完整流水线
- [ ] 验证切片质量
- [ ] 与弹幕密度切片对比

---

## Phase 5: 话题分析报告去重与解析修复 [已完成]

> 状态: 已完成
> 说明: 修复 LLM 复读提示词示例导致的重复切片、报告正文缺失和旧 JSON 重复切片问题。

### Action 5.1: 修复话题分析报告去重 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | 无 |
| 描述 | 收紧话题分析提示词，解析 LLM 输出时按当前分块时间范围过滤，保留报告正文，并对报告段落与 clip_marks 去重。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有单元测试通过，覆盖复读示例过滤、报告正文保留、clip_marks 去重。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-06 04:58 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |




### Action 5.2: 调整话题报告为逐话题时间轴格式 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.1 |
| 描述 | 将话题分析报告输出改为“逐话题时间轴”样式，按 Part 分组，话题条目下保留 ·/● 详细要点，同时继续支持切片标记解析。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过，报告格式包含逐话题时间轴、Part 分组、正文要点和去重后的切片标记。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-06 05:08 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |



### Action 5.3: 过滤模型思考内容和占位话题 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.2 |
| 描述 | 收紧 LLM 响应解析，过滤“但注意/我们应该/输出格式”等模型思考说明、无明显话题和占位话题，避免污染最终话题时间轴。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过，报告正文不再包含模型自我说明、占位标题或无明显话题段。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-06 05:34 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |



### Action 5.4: 修正话题切片时间基准和上下文 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.3 |
| 描述 | 明确话题切片时间为视频内时间，并在写入 clip_marks 和实际切片前按 SRT 上下文扩展，避免只切弹幕爆点导致前因后果缺失。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过，短话题标记会扩展为包含前后文的切片区间，JSON 标明 video_elapsed_seconds 时间基准。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-06 05:44 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |



### Action 5.5: 清理标题和正文残留推理说明 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.4 |
| 描述 | 强化话题标题与正文过滤，移除“但时间太短/例如/由于弹幕密度/所以输出”等模型判断过程，避免污染报告和切片文件名。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过，标题和正文不再包含模型自我推理、弹幕密度判定说明或占位内容。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-06 06:22 |
| 备注 | 参考 video-topic-analyzer 的分块与后处理思路；验证通过：python -B -m unittest test_topic_engine.py -v |



### Action 5.6: 增强 LLM API 500 重试与降级 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.5 |
| 描述 | 参考 video-topic-analyzer 的稳定分块思路，增强 LLM 分块调用的 500/429/超时重试、指数退避、紧凑 prompt 降级，并记录失败块。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；临时 500 会自动重试，连续失败会记录 failed_chunks 而不是静默丢失。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-06 06:36 |
| 备注 | 参考 video-topic-analyzer 的稳定分块思路；验证通过：python -B -m unittest test_topic_engine.py -v |



### Action 5.7: 修正 API 预检 500 的任务状态 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.6 |
| 描述 | API 预检遇到 5xx/429/超时时不再误报“完成 0 个”，而是继续尝试正式分块；若正式分块连续失败则抛出错误，让 Web 显示失败状态。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；预检临时失败会写入报告/API 警告，连续分块失败会中止而不是静默完成。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-06 06:50 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |



### Action 5.8: 清理新报告残留说明和半句 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.7 |
| 描述 | 继续强化话题报告后处理，过滤“内容要点/我们还需要考虑/根据格式/如果有礼物”等说明、弹幕密度解释和被截断的半句。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；新报告样例中的残留说明、密度解释和半句不会进入最终报告。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-07 04:49 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |



### Action 5.9: 报告主播名替换与 SC 前文包含 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.8 |
| 描述 | 从录播路径推断主播名，最终话题报告中将“主播”替换为具体名字；切片上下文扩展时识别 SC/礼物/付费留言等触发语句，把可切话题前面的观众 SC 或礼物文本一起纳入切片。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；报告可按指定主播名替换“主播”，短话题扩展会回溯包含 SC/礼物触发字幕。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-07 05:01 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |


### Action 5.10: 改为全程时间轴而非只挑爆点 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.9 |
| 描述 | 调整 LLM 提示词，从“只输出明显话题/爆点”改为“每个分块都要输出连续时间轴话题，普通聊天和游戏过程也要整理；仅对值得切的段加 ✂️”。报告展示名改用音音等粉丝称呼，并提示遇到 SC/观众留言时保留音姐/麻麻/音音等开头称呼；同时继续过滤“只能写基于字幕/标题可以更简洁/现在写/我决定输出/注意起始时间/弹幕高密度”等模型说明。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；prompt 明确全程时间轴要求，不再诱导 LLM 对普通分块输出“无明显话题”；报告使用粉丝称呼；新残留说明被过滤。 |
| 重试次数 | 1/3 |
| 完成时间 | 2026-07-08 00:54 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |



### Action 5.11: 改为 10 分钟处理块和自然话题拆分 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.10 |
| 描述 | 将 LLM 处理块从 5 分钟调整为 10 分钟，减少调用次数和话题割裂；提高非紧凑 prompt 的字幕输入上限与输出 token，并要求每块输出 2-5 个自然话题（内容少可 1 个），最终报告继续按 Part 聚合。 |
| 涉及文件 | 	opic_engine.py 	est_topic_engine.py |
| 验证命令 | python -B -m unittest test_topic_engine.py -v |
| 验证预期 | 所有测试通过；chunk_srt 默认按 10 分钟分块，prompt 明确 2-5 个自然话题且保留完整时间轴。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-08 01:26 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |


### Action 5.12: 修复异常 SRT 时间戳和报告污染残留 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.11 |
| 描述 | SRT 解析阶段去除连续重复字幕，识别长文本压缩到极短时间的 ASR 异常并按文本长度估算结束时间；分块 prompt 输出时间范围而不只是起点；继续过滤“要点用/现在写最终答案/规则要求/不符合常识/弹幕互动平淡”等模型说明和密度废话。 |
| 涉及文件 | 	opic_engine.py 	est_topic_engine.py |
| 验证命令 | python -B -m unittest test_topic_engine.py -v |
| 验证预期 | 所有测试通过；异常长字幕不会形成 2 秒话题提示，旧报告样例中的新污染不会进入最终报告。 |
| 重试次数 | 1/3 |
| 完成时间 | 2026-07-08 03:38 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v；真实 SRT 抽查 3:54-3:59 段已从重复 0.x 秒字幕修正为分钟级范围。 |


### Action 5.13: 增加分块兜底时间轴和长标题清洗 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.12 |
| 描述 | 当 LLM 某个 10 分钟分块没有解析出有效话题时，按分块字幕生成保底时间轴条目，避免整场直播大面积空缺；对超长原文标题自动降级为短标题；继续过滤代码块、段落编号、时间重叠分析和“我们按时间顺序梳理”等残留说明。 |
| 涉及文件 | 	opic_engine.py 	est_topic_engine.py |
| 验证命令 | python -B -m unittest test_topic_engine.py -v |
| 验证预期 | 所有测试通过；空分块会生成非切片兜底话题，超长标题不会进入报告，当前报告样例中的代码块/段落分析残留被过滤。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-08 14:47 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |


### Action 5.14: 清理兜底输出和推理残留 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.13 |
| 描述 | 兜底时间轴不再使用 ASR 原文生成标题和正文预览，避免 imistion/痔疮等垃圾标题；进一步过滤“根据字幕/话题1/可能的划分/通常做法/我们尽量”等模型推理残留，禁止代码块和编号提纲进入报告。 |
| 涉及文件 | 	opic_engine.py 	est_topic_engine.py |
| 验证命令 | python -B -m unittest test_topic_engine.py -v |
| 验证预期 | 所有测试通过；当前报告样例中的兜底垃圾标题、ASR 原文预览和推理提纲不会进入最终报告。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-08 17:47 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |


### Action 5.15: 改成每小时重点与弹幕密度切片筛选 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.14 |
| 描述 | 参考 video-topic-analyzer 成熟流程：每个处理块只提 1-2 个核心话题，最终报告按小时聚合为“每小时重点”；切片标记不再只依赖 LLM 的 ✂️，而是对重点话题结合弹幕峰值/平均密度进行筛选，只有弹幕密度高且非兜底的话题才进入 clip_marks。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；报告支持按小时分组，话题可切标记由弹幕密度决定，高密度重点会生成 clip_marks，低密度或兜底话题不切。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-08 21:41 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |


### Action 5.16: 过滤模型草稿并禁止无主播内容误切 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.15 |
| 描述 | 针对最新实跑报告中的“考虑分成以下话题/我们仔细分析/更合理的是/标题：”等模型草稿残留继续清洗；同时在弹幕密度切片筛选前增加内容可切性门槛，禁止只有游戏背景语音、无主播发言、字幕不清晰或兜底说明的高密度片段进入 clip_marks。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；当前报告样例中的模型草稿行被过滤，游戏开头动画/背景语音即使弹幕高也不会生成切片标记。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-08 22:33 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |


### Action 5.17: 清理剩余草稿句并去重重叠切片 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.16 |
| 描述 | 针对最新实跑报告继续过滤“首先覆盖/最好基于时间顺序/建议这样划分/字幕原文/让我们详细解析”等模型草稿和原文转述；同时对同标题高度重叠的切片标记去重，避免晚安/奶茶等连续尾段重复切片。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；模型草稿不会进入报告，同标题重叠切片只保留一个。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-09 00:14 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |


### Action 5.18: 结构化过滤模型分析过程并修正切片标题 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.17 |
| 描述 | 针对最新实跑报告中的“先理解字幕/所以整体/大致内容/可能的整理/从字幕看/第一part”等分析过程做结构化过滤；当标题明显是模型草稿时从有效正文重新推导短标题，避免 clip_marks 出现“所以整体是主播...”这类文件名。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；最新报告样例中的分析过程被过滤，翡翠石头讨论切片标题被清洗为短标题。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-09 01:13 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |


### Action 5.19: 强过滤分析过程标题和错误切片 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.18 |
| 描述 | 针对最新实跑报告中的“最佳方式/我们仔细看时间线变化/我们分析有哪些连续讲话/输出时不要写Part行/一个合理的方法”等分析过程标题做强过滤；分析过程标题不得进入报告和 clip_marks，无法从正文推导出真实内容标题时整条丢弃。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；最新 JSON 中这三类分析过程标题不会再生成切片，报告中不再出现这些草稿话题。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-09 02:33 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |


### Action 5.20: 过滤规划划分类残留并阻止误切 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.19 |
| 描述 | 针对最新实跑报告中的“现在规划/可能的最佳划分/具体分段/梳理字幕的连续意思/输出最终条目/具体要点/比如”等规划划分类残留继续结构化过滤；正文过滤后为空的话题直接丢弃，避免“中文（问候等）”等非内容标题进入 clip_marks。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；最新报告中的规划划分类残留不再进入报告和切片 JSON。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-09 02:54 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |


### Action 5.21: 切换话题分析模型为 DeepSeek V4 Pro [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.20 |
| 描述 | 将话题分析默认 LLM 从 deepseek-v4-flash 切换为 deepseek-v4-pro，并同步本地 api_config.json 的 model 字段；报告展示和配置兜底均使用 Pro。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` `PLAN.md` `api_config.json(本地不提交)` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；默认 LLM_MODEL 和 load_api_config 兜底模型均为 deepseek-v4-pro，本地 api_config.json 的 model 字段已切换。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-09 03:01 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v；api_config.json 已本地切换为 deepseek-v4-pro，未提交 token 文件。 |

### Action 5.22: 过滤 Pro 输出的总结和格式说明残留 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.21 |
| 描述 | 针对 DeepSeek V4 Pro 最新报告中的“这部分明显/继续这段剧情/總結話題/输出内容要严格按照格式/标题加emoji/感谢一个礼物”等总结和格式说明残留继续过滤；泛标题无法推导真实内容时丢弃，避免进入 clip_marks。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；最新 Pro 报告中的总结/格式说明残留不进入报告和切片 JSON。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-09 03:24 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |

### Action 5.23: 改用结构化 JSON 输出降低报告污染 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.22 |
| 描述 | 将 LLM 分块提示从自由 Markdown 条目改为严格 JSON 输出，新增 JSON topics 解析器并保留旧 Markdown 解析兜底；程序只接收 start/end/title/can_slice/points 字段，降低“总结/规划/格式说明”混入报告和 clip_marks 的概率。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；JSON 输出可解析为时间轴话题和切片标记，prompt 明确只输出 JSON。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-09 10:14 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |

### Action 5.24: 过滤 JSON 字段残片和兜底解析残留 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.23 |
| 描述 | 针对最新实跑报告中的 `points:`、`title:`、`要点：`、`重新考虑分块内容`、`我们先把内容分几个话题`、`那么我们定义`、`整体时间段` 等 JSON 字段残片和 Markdown 兜底解析残留继续过滤；过滤后无有效正文的话题直接丢弃，避免进入报告和 clip_marks。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；最新报告样例中的 JSON 字段残片和兜底解析残留不再进入报告或切片 JSON。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-09 11:35 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |

### Action 5.25: 接入人工切片时间轴 docx 辅助分析 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.24 |
| 描述 | 自动按录播日期读取 `F:\切片时间轴\YYYYMMDD.docx`/切片文档，解析墙钟时间并换算为视频内时间；将人工时间轴尤其 ⭐ 片段加入 LLM 分块提示、最终报告和切片筛选，提高话题可信度；同时修复 CLI 在 GBK 控制台打印 emoji 崩溃。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` `PLAN.md` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；可解析 20260708.docx 样式时间轴，⭐ 片段能附加到匹配话题并提高可切优先级，CLI UTF-8 输出不因 emoji 崩溃。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-09 12:45 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |

### Action 5.26: 修复人工重点被兜底话题吞掉和新草稿残留 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.25 |
| 描述 | 人工 ⭐ 片段不再被“日常聊天/字幕识别较碎”兜底话题吞掉，而是补成独立人工重点话题并可参与切片；继续过滤 `我们规划话题`、`先考虑can`、`建议分成两个话题`、`最终 JSON`、`根据人工时间轴` 等真实报告残留。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` `PLAN.md` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；人工 ⭐ 重点可生成独立话题并进入 clip_marks，最新报告草稿残留被过滤。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-09 13:25 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |

### Action 5.27: 收紧人工时间轴合并和提示残留过滤 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.26 |
| 描述 | 人工 ⭐ 只合并到严格覆盖该时间点的真实话题，避免和相邻但不相关的 LLM 话题错配；继续过滤 `需要写点`、`人工时间轴参考`、`观察时间戳`、`我们看内容`、`我们来看内容` 等真实报告残留。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` `PLAN.md` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；人工重点不再错配到相邻话题，最新报告草稿/提示残留被过滤。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-09 14:05 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |

### Action 5.28: 最终报告前二次清洗坏标题和坏切片 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.27 |
| 描述 | 在生成报告和 clip_marks 前追加最终清洗：坏标题/提示语标题不得进入报告和切片，带人工 ⭐ 的坏标题优先改用人工时间轴标题；继续过滤 `其他话题`、`这些人工时间轴可帮助...`、`与上一段有重叠`、`下一段`、`可能的切分` 等真实报告残留。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` `PLAN.md` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；最新真实报告中的坏标题不会进入报告或 clip_marks。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-09 14:50 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |

### Action 5.29: 真实流水线禁用 Markdown 兜底解析 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.28 |
| 描述 | 真实 `run_pipeline` 只接受结构化 JSON 话题输出；LLM 未返回可解析 JSON 时不再走 Markdown 兜底，直接使用兜底时间轴/人工时间轴，避免模型草稿进入报告和 clip_marks；OpenAI 兼容响应不再读取 `reasoning_content`。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` `PLAN.md` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；严格 JSON 模式会忽略 Markdown 草稿，真实报告不再出现模型推理过程。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-09 15:35 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |

### Action 5.30: 适配 DeepSeek Pro reasoning 输出但不污染报告 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.29 |
| 描述 | 提高 DeepSeek V4 Pro 输出 token，避免 content 被 reasoning 挤空；预检使用更高 token；仅当 `reasoning_content` 含完整 JSON 时用于结构化解析，禁止把非 JSON 推理文本当报告内容。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` `PLAN.md` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；API 预检不会因 reasoning_content 挤占 100 token 失败，真实流水线仍不解析非 JSON 推理文本。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-09 16:10 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |

### Action 5.31: 人工时间轴快路径避免 Pro 全量超时 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.30 |
| 描述 | 当录播日期存在 `F:\切片时间轴` docx 时，优先使用人工时间轴聚合生成干净话题并结合弹幕筛切片，跳过 39 个 LLM 分块，避免 DeepSeek Pro 全量推理超时；无人工时间轴时仍走 LLM。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` `PLAN.md` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；人工时间轴可聚合为干净话题，真实分析不会因 Pro 全量分块超时。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-09 16:35 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |

### Action 5.32: 修正人工快路径报告编号和普通时间轴保留 [✅]

| 字段 | 值 |
|------|-----|
| 状态 | 已验证 |
| 依赖 | Action 5.31 |
| 描述 | 修正按小时分组报告的 Part 编号跳号问题；人工时间轴快路径中普通记录改为 `·时间轴：...`，避免被时间戳过滤规则删除，从而保留切片前后上下文。 |
| 涉及文件 | `topic_engine.py` `test_topic_engine.py` `PLAN.md` |
| 验证命令 | `python -B -m unittest test_topic_engine.py -v` |
| 验证预期 | 所有测试通过；真实报告 Part 顺序连续，人工时间轴普通行保留在报告正文中。 |
| 重试次数 | 0/3 |
| 完成时间 | 2026-07-09 16:50 |
| 备注 | 验证通过：python -B -m unittest test_topic_engine.py -v |
