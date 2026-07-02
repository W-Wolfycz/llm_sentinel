# Changelog

## v1.3.0

### 默认提示词优化

- **性暗示检测**：调整为「宽松优先、累积触发」——可检可不检一律不触发；单次软暗示（谐音/错字/拆字/emoji/拼音缩写等）需在 `<history>` 中反复累积成明显色情引导才判定；正常亲密互动（亲吻、深吻、拥抱、暧昧表达等）不再被误伤。理由：每轮对话都会过 sentinel，孤立擦边会在后续轮次被累积识别，无需单次过度敏感。
- **角色状态覆写检测**：精简并严格化——核心判定改为「主语 + 语气」双重规则。新增「嵌入式描写」判定（复合句中前半为疑问/假设/命令，后半嵌入 AI 状态描写仍算覆写，如「你是不是害羞了，低下头？」）；连带描写、任意符号包裹（括号/星号/方括号/书名号/引号等）一律 flagged。篇幅较原版精简约 40%。
- **时间线覆写检测**：仅精简格式与表达（标题统一为【】风格、删冗余「如」字等），判定边界完全不变。

### Bug 修复

- 修复 `user_text` 为空时（用户发图片/语音/表情）整条 sentinel 被跳过的 bug——此前 `ai_only` 规则即使在仅依赖历史的情况下也会被误挡，现在 `text_val.strip() or hist_block.strip()` 过滤层能正确放行
- 修复 `_parse_result` 未捕获 XML `ParseError` 的 bug——模型输出畸形 XML（未闭合标签、未转义字符等）时整条规则被记为「检测失败」跳过；现在包 `try-except ET.ParseError` 返回 `(False, "")`
- 修复历史配对循环假设严格 user-assistant 交替的 bug——用户连发消息（`user-user-assistant`）时配对错位，哨兵提示词会显示「用户/用户」混乱配对；现在按时间序以 `pending_user → assistant` 配对，连发取最后一条，孤立 assistant 丢弃
- 修复 `chat_memory.query_history` 在 `user_only` / `ai_only` 单类型模式下 limit 不足的 bug——拉 `N*2` 条混合记录后过滤会丢消息，改为单类型 mode 拉 `N*4`
- `chat_memory` 未找到时的回退日志由 `warning` 降为 `debug`，避免每轮对话刷屏
- 其它：`from datetime import datetime` / `import sys` 提至顶部；`rule.setdefault("name", rule.get("rule_name", ""))` 冗余求值清理；`_OUTPUT_FORMAT` 中文引号统一为「无」

## v1.2.0

- 警示注入通道由 `req.system_prompt` 改为 `req.extra_user_content_parts`：警示是每轮条件触发的动态内容，原写法会破坏模型服务端的提示词缓存，显著增加请求成本和首 token 延迟
- 标记 `mark_as_temp()`，警示只参与本轮 LLM 请求，不持久化到会话历史；对旧版 AstrBot（< v4.24.0）自动退化为普通内容块
- 重写「角色状态覆写」默认检测提示词：引入"主语判定"规则——以"我"为主语（含省略"我"的祈使/动作句，如`我抱你`、`帮你xx`）属于用户对 AI 的主动动作，不算覆写；只有以"你"为主语替 AI 描写状态/动作/反应才是覆写。修复原先对用户主动亲密行为的误判
- 新增规则配置 `history_source`（both / user_only / ai_only）：决定 `{history}` 占位符的内容构成。`both` = 用户+AI 配对（默认），`user_only` = 仅用户历史发言，`ai_only` = 仅 AI 历史回复
- `ai_only` 模式下 `{text}` 自动置空——规则只分析 AI 历史回复，不读当前用户输入。配合 `rounds=1-3` 可审计 AI 上一轮回复（如称谓一致性、括号规范等）
- 修复 `min_check_length` 在 `ai_only` 模式下错误跳过规则的 bug：该过滤原本作用于 `user_text`，但 ai_only 模式下 `user_text` 为空导致规则永远被跳过。现在 ai_only 模式跳过此过滤

## v1.1.0

- 新增 `whitelist` 白名单配置：填入 QQ 号列表，名单内用户的消息跳过所有哨兵检测

## v1.0.0

初始版本。

- 性暗示/色情检测、角色状态覆写检测、时间线覆写检测三套内置模板
- 可自定义检测提示词与警示模板，支持独立启用/禁用各规则
- 哨兵模型可单独配置
- 并行检测，共享延迟
- 每条规则独立配置 `min_check_length`（最小检测长度，0=不启用）与 `history_rounds`（参考对话轮数）
- 对话历史参考：可配置近 N 轮对话（含 BOT 回复），用于识别渐进式越界与连续覆写模式
- 历史数据源双路径：`chat_memory` 插件（按用户 ID 隔离）/ AstrBot 自带上下文（群聊共享）
- 检测提示词支持 `{history}` 占位符，自动注入 `<history>` 块
- 日志前缀统一为 `[LLMSentinel]`，`log_config` 组可附加机器人 ID 与 debug 提级
