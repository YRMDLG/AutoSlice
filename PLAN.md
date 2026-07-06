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

