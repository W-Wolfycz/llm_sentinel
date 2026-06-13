"""LLM Sentinel — 上下文安全哨兵，在 LLM 请求前检测不当内容并注入警示。"""

import re
import json
import asyncio
import xml.etree.ElementTree as ET

from astrbot.api import logger
from astrbot.api.star import Star, Context
from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.message_components import Plain

# chat_memory 可选依赖：启用 use_chat_memory 时按用户 ID 隔离读取历史
try:
    from chat_memory.main import query_history as _chat_memory_query
except ImportError:
    _chat_memory_query = None

_TEMPLATE_NAMES = {
    "sexual": "性暗示/色情检测",
    "state_overwrite": "角色状态覆写检测",
    "time_overwrite": "时间线覆写检测",
}

# 清洗历史中残留的注入标签（thought 块、情感面板等），避免污染哨兵判断
_CTX_CLEAN_PATTERN = re.compile(
    r"<thought>.*?</thought>|"
    r"<情感好感>.*?</情感好感>",
    re.DOTALL,
)


class LLMSentinelPlugin(Star):
    # 由插件统一追加的 XML 输出说明——结果格式与解析都收在代码边界内
    _OUTPUT_FORMAT = (
        '\n\n只输出XML：'
        '<result><flagged>true或false</flagged>'
        '<detail>引用检测到的具体内容，未检测到则输出"无"</detail></result>'
    )

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        self.guard_provider = config.get("guard_provider", "")
        self.use_chat_memory = config.get("use_chat_memory", False)
        if self.use_chat_memory and _chat_memory_query is None:
            logger.warning("[LLMSentinel] 未找到 chat_memory 插件，回退到 AstrBot 自带上下文")
            self.use_chat_memory = False

        log_conf = config.get("log_config", {})
        self.log_with_bot_id = log_conf.get("log_with_bot_id", False)
        self.debug_to_info = log_conf.get("debug_to_info", False)

        self.rules = self._load_rules(config.get("guard_rules", []))

        enabled = [r["name"] for r in self.rules if r.get("enabled", True)]
        if enabled:
            logger.info(f"[LLMSentinel] 已启用: {', '.join(enabled)}")
        else:
            logger.info("[LLMSentinel] 未配置任何检测规则")

    # ── 日志 ────────────────────────────────────────────

    def _tag(self, event=None) -> str:
        if self.log_with_bot_id and event is not None:
            try:
                return f"[LLMSentinel:{event.get_platform_id()}]"
            except Exception:
                pass
        return "[LLMSentinel]"

    def _log(self, event: AstrMessageEvent, msg: str):
        if self.debug_to_info:
            logger.info(f"{self._tag(event)} {msg}")
        else:
            logger.debug(f"{self._tag(event)} {msg}")

    # ── 规则加载 ────────────────────────────────────────

    @staticmethod
    def _validate_rule(rule: dict) -> bool:
        return isinstance(rule, dict) and bool(rule.get("check_prompt"))

    @staticmethod
    def _load_rules(rules_raw) -> list[dict]:
        if not isinstance(rules_raw, list) or not rules_raw:
            return []
        normalized = []
        for r in rules_raw:
            if not isinstance(r, dict):
                continue
            rule = {k: v for k, v in r.items() if not k.startswith("__")}
            tk = r.get("__template_key", "")
            if tk and tk != "custom":
                rule.setdefault("id", tk)
                rule.setdefault("name", _TEMPLATE_NAMES.get(tk, tk))
            else:
                rule.setdefault("id", rule.get("rule_name", ""))
                rule.setdefault("name", rule.get("rule_name", ""))
            if LLMSentinelPlugin._validate_rule(rule):
                normalized.append(rule)
        return normalized

    # ── 用户文本提取 ────────────────────────────────────

    @staticmethod
    def _extract_user_text(event: AstrMessageEvent) -> str:
        chain = getattr(event, "message_chain", None)
        if chain:
            parts = [comp.text for comp in chain if isinstance(comp, Plain)]
            text = "".join(parts).strip()
            if text:
                return text
        return getattr(event, "message_str", "") or ""

    # ── XML 解析 ───────────────────────────────────────

    @staticmethod
    def _parse_result(xml_str: str) -> tuple[bool, str]:
        raw = re.sub(r"```(?:xml)?\s*", "", xml_str).strip()
        match = re.search(r"<result>.*?</result>", raw, re.DOTALL)
        if not match:
            return False, ""
        root = ET.fromstring(match.group(0))
        flagged = (root.findtext("flagged") or "false").strip().lower() == "true"
        detail = (root.findtext("detail") or "").strip()
        return flagged, detail

    # ── 对话历史 ────────────────────────────────────────

    async def _get_recent_history(
        self, umo: str, user_id: str, conversation_id: str,
        current_user_text: str = "", rounds: int = 0,
    ) -> str:
        """获取最近 N 轮对话历史（user + assistant 配对），返回纯文本（无 <history> 包裹）。"""
        if rounds <= 0 or not conversation_id:
            return ""

        if self.use_chat_memory and _chat_memory_query is not None:
            try:
                records = await _chat_memory_query(
                    umo, conversation_id, user_id, limit=rounds * 2
                )
            except Exception as e:
                logger.warning(f"{self._tag()} chat_memory 读取失败: {e}")
                records = []
        else:
            records = await self._read_astrbot_history(umo, conversation_id)

        if not records:
            return ""

        # 去重：若末尾 user 等于当前输入（AstrBot 上下文可能已写入），丢弃
        if current_user_text and records and records[-1].get("role") == "user":
            if records[-1].get("content", "").strip() == current_user_text.strip():
                records = records[:-1]

        # 只保留最近 N 轮（每轮 = user + assistant）
        records = records[-(rounds * 2):]

        # 配对格式化
        lines = []
        for i in range(0, len(records) - 1, 2):
            idx = i // 2 + 1
            user_msg = records[i].get("content", "")
            bot_msg = records[i + 1].get("content", "")
            lines.append(f"[{idx}] 用户: {user_msg}")
            lines.append(f"[{idx}] 角色: {bot_msg}")
        return "\n".join(lines)

    async def _read_astrbot_history(self, umo: str, conversation_id: str) -> list[dict]:
        """从 AstrBot 自带上下文读取历史，返回 [{role, content}, ...]。"""
        try:
            conv = await self.context.conversation_manager.get_conversation(umo, conversation_id)
            if not conv or not conv.history:
                return []
            raw = json.loads(conv.history) if isinstance(conv.history, str) else conv.history
            result = []
            for msg in raw:
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                content = str(msg.get("content", ""))
                content = _CTX_CLEAN_PATTERN.sub("", content).strip()
                if content:
                    result.append({"role": role, "content": content})
            return result
        except Exception as e:
            logger.warning(f"{self._tag()} 读取 AstrBot 上下文失败: {e}")
            return []

    # ── 单条检测 ────────────────────────────────────────

    async def _check(
        self, event: AstrMessageEvent, user_text: str, rule: dict,
        provider_id: str, history_block: str,
    ) -> tuple[bool, str]:
        # 每条规则独立的 min_check_length 过滤（0=不启用）
        min_len = max(0, rule.get("min_check_length", 0))
        if min_len > 0 and len(user_text) < min_len:
            return False, ""

        prompt = (rule["check_prompt"]
                  .replace("{history}", history_block)
                  .replace("{text}", user_text)) + self._OUTPUT_FORMAT
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id, prompt=prompt
            )
            raw = (resp.completion_text or "").strip()
            flagged, detail = self._parse_result(raw)
            if not flagged and not detail:
                self._log(event, f"'{rule['name']}': 未命中或未解析: {raw[:200]}")
            return flagged, detail
        except Exception as e:
            logger.warning(f"{self._tag(event)} '{rule['name']}' 检测失败: {e}")
            return False, ""

    # ── 主钩子 ──────────────────────────────────────────

    @filter.on_llm_request()
    async def guard_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM 请求前哨兵检测：分析用户输入（可参考历史），命中规则时向主模型注入警示。"""
        if event.get_extra("sentinel_checked"):
            return
        event.set_extra("sentinel_checked", True)

        enabled_rules = [r for r in self.rules if r.get("enabled", True)]
        if not enabled_rules:
            return

        user_text = self._extract_user_text(event)
        if not user_text:
            return

        umo = getattr(event, "unified_msg_origin", "")
        if self.guard_provider:
            provider_id = self.guard_provider.strip()
        else:
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            except Exception:
                return
        if not provider_id:
            return

        # 预取历史块：按每条规则的 history_rounds 分组，每个唯一 rounds 值取一次
        user_id = event.get_sender_id() or ""
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo) or ""
        except Exception:
            cid = ""

        history_blocks: dict[int, str] = {}
        for rule in enabled_rules:
            rounds = max(0, min(10, rule.get("history_rounds", 0)))
            if rounds in history_blocks:
                continue
            if rounds > 0 and cid:
                history_text = await self._get_recent_history(
                    umo, user_id, cid, user_text, rounds
                )
                history_blocks[rounds] = (
                    f"<history>\n{history_text}\n</history>" if history_text else ""
                )
            else:
                history_blocks[rounds] = ""

        tasks = [
            self._check(
                event, user_text, r, provider_id,
                history_blocks[max(0, min(10, r.get("history_rounds", 0)))]
            )
            for r in enabled_rules
        ]
        results = await asyncio.gather(*tasks)

        warnings = []
        for rule, (flagged, detail) in zip(enabled_rules, results):
            if flagged:
                tpl = rule.get("warning_template", "")
                warnings.append(
                    tpl.replace("{detail}", detail) if tpl
                    else f"⚠️ 规则 '{rule['name']}' 检测到异常（{detail}）"
                )
                logger.info(f"{self._tag(event)} '{rule['name']}' 触发: {detail}")

        if warnings:
            req.system_prompt = (req.system_prompt or "") + "\n\n" + "\n\n".join(warnings)
            self._log(event, f"已注入 {len(warnings)} 条警示")
