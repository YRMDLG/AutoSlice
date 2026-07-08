# AutoSlice 智能话题分析 + 切片 v2 — 开发计划

## 目标

整合 video-topic-analyzer 的核心思路，在 AutoSlice 中新建完整的"话题分析 + 智能切片"功能。不影响现有弹幕切片和时间轴切片。

## 用户要求

1. 移植 skill 核心逻辑，适配 RTX 2060 6GB
2. 无 SRT 时自动用 FunASR 生成，有 SRT 直接读
3. 独立新功能，不修改现有的弹幕切片 / docx 时间轴切片
4. 弹幕密度 + LLM 话题分析结合，AI 标记哪些段该切，自动切片按报告执行
5. LLM 用 DeepSeek v4 Flash（不用 Pro）

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
- [ ] DeepSeek v4 Flash API 调用
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

