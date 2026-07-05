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


