# Desktop-07 AI 建议契约

`autoslice.desktop.ai_review.Suggestion` 是 Qt 唯一消费的建议结构。字段包括稳定的 `suggestion_id`、源 cue 序号 `cue_id`、`original_text`、`suggested_text`、`reason`、可选 `confidence`、`status`（`pending` / `accepted` / `skipped`）、`source`、`model` 和 `prompt_version`。`cue_id` 是源 SRT 序号，不是当前列表行号；采纳时再次核对当前 cue 存在且正文与 `original_text` 完全一致。文本改变或 cue 删除即冲突，禁止直接覆盖。

`%LOCALAPPDATA%\AutoSlice\cache\ai` 保存两层数据：按内容与配置哈希分隔的旧检查器输入 SRT/原始结果缓存，以及按源 SRT 路径标识的桌面建议会话。桌面会话为 schema v1，记录项目标题、配置哈希、源文件指纹、当前文档哈希、缓存键和建议状态。读取时逐项校验；不匹配即标记过期，不自动调用 AI。用户正式保存校对字幕后，文档内容一致时仍可恢复处理进度。密钥不进入缓存键、会话或日志。

检查仅由“AI 检查”或确认后的“重新检查”调用。旧检查器在用户私有目录中复用自己的缓存；重新检查传入 `use_cache=False`。结果从不自动应用到字幕。采纳调用 `SubtitleDocument.edit_text`，跳过只改建议状态；两个动作立即持久化会话。字幕草稿仍由既有节流保存负责，正式 SRT 仍须用户主动保存。
