"""LLM Sentinel — 上下文安全哨兵，在 LLM 请求前检测不当内容并注入警示。"""

import re
import json
import sys
import asyncio
from datetime import datetime
import xml.etree.ElementTree as ET

from astrbot.api import logger
from astrbot.api.star import Star, Context
from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.message_components import Plain
from astrbot.core.agent.message import TextPart

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
        '<detail>引用检测到的具体原文片段（仅复制用户输入中的关键句段，不要改写不要补充，未检测到则输出「无」）；'
        '若片段含 < > & 等 XML 特殊字符，必须转义为 &lt; &gt; &amp;</detail></result>'
    )

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        self.guard_provider = config.get("guard_provider", "")
        self.use_chat_memory = config.get("use_chat_memory", False)
        self._chat_memory = None  # 成功解析后缓存，失败不缓存以便下次重试
        self.whitelist = self._load_whitelist(config.get("whitelist", []))

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

    def _resolve_chat_memory(self):
        """定位 chat_memory 插件，返回带 query_history 方法的对象。
        成功后缓存到 self._chat_memory；失败不缓存以便下次重试。
        优先 AstrBot 插件注册表；失败则直接从 sys.modules 查找，绕过包导入路径问题。
        """
        if self._chat_memory is not None:
            return self._chat_memory
        try:
            star = self.context.get_registered_star("chat_memory")
            if star is not None:
                for candidate in (star, getattr(star, "star", None), getattr(star, "star_cls", None)):
                    if candidate is not None and hasattr(candidate, "query_history"):
                        self._chat_memory = candidate
                        return candidate
        except Exception:
            pass
        mod = sys.modules.get("chat_memory.main") or sys.modules.get("chat_memory")
        if mod is not None and hasattr(mod, "query_history"):
            self._chat_memory = mod
            return mod
        return None

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
                rule_name = rule.get("rule_name", "")
                rule.setdefault("id", rule_name)
                rule.setdefault("name", rule_name)
            if LLMSentinelPlugin._validate_rule(rule):
                normalized.append(rule)
        return normalized

    # ── 用户文本提取 ────────────────────────────────────

    @staticmethod
    def _load_whitelist(raw) -> set[str]:
        """严格 list 解析：非 list 或元素非字符串直接丢弃。"""
        if not isinstance(raw, list):
            return set()
        return {str(x).strip() for x in raw if str(x).strip()}

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
        try:
            root = ET.fromstring(match.group(0))
        except ET.ParseError:
            return False, ""
        flagged = (root.findtext("flagged") or "false").strip().lower() == "true"
        detail = (root.findtext("detail") or "").strip()
        return flagged, detail

    # ── 对话历史 ────────────────────────────────────────

    def _ts_tag(self, record: dict) -> str:
        """从记录中提取时间戳标签 (MM-DD HH:MM)；无 created_at 或解析失败时返回空串。"""
        created = record.get("created_at")
        if not created:
            return ""
        try:
            dt = datetime.strptime(str(created)[:19], "%Y-%m-%d %H:%M:%S")
            return dt.strftime("(%m-%d %H:%M)")
        except (ValueError, TypeError):
            return ""

    async def _get_recent_history(
        self, umo: str, user_id: str, conversation_id: str,
        current_user_text: str = "", rounds: int = 0,
        mode: str = "all",
    ) -> str:
        """获取最近 N 轮对话历史，返回纯文本（无 <history> 包裹）。

        mode:
          - "all": user + assistant 配对（默认）
          - "user_only": 仅用户发言，按时间顺序列出
          - "ai_only": 仅 AI 回复，按时间顺序列出
        """
        if rounds <= 0 or not conversation_id:
            return ""

        chat_memory = self._resolve_chat_memory() if self.use_chat_memory else None
        if self.use_chat_memory and chat_memory is None:
            logger.debug(
                f"{self._tag()} 未找到 chat_memory 插件，本次回退到 AstrBot 自带上下文"
            )

        if chat_memory is not None:
            try:
                # 单类型 mode 时拉更多条补偿过滤损失（每 2 条混合记录约含 1 条目标类型）
                fetch_limit = rounds * 2 if mode == "all" else rounds * 4
                records = await chat_memory.query_history(
                    umo, conversation_id, user_id, limit=fetch_limit
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

        if mode == "user_only":
            wanted = "user"
        elif mode == "ai_only":
            wanted = "assistant"
        else:
            wanted = None

        if wanted:
            filtered = [r for r in records if r.get("role") == wanted]
            # 只保留最近 N 条（每轮 = 1 条）
            filtered = filtered[-rounds:]
            label = "用户" if wanted == "user" else "角色"
            lines = []
            for idx, r in enumerate(filtered, 1):
                ts = self._ts_tag(r)
                prefix = f"[{idx}] {ts} {label}" if ts else f"[{idx}] {label}"
                lines.append(f"{prefix}: {r.get('content', '')}")
            return "\n".join(lines)

        # 默认：配对格式（user + assistant）——重新配对以应对 user-user-assistant 等非严格交替
        pairs = []
        pending_user = None
        for r in records[-(rounds * 2):]:
            role = r.get("role")
            if role == "user":
                pending_user = r
            elif role == "assistant" and pending_user is not None:
                pairs.append((pending_user, r))
                pending_user = None
        pairs = pairs[-rounds:]
        lines = []
        for idx, (u, a) in enumerate(pairs, 1):
            user_ts = self._ts_tag(u)
            bot_ts = self._ts_tag(a)
            user_prefix = f"[{idx}] {user_ts} 用户" if user_ts else f"[{idx}] 用户"
            bot_prefix = f"[{idx}] {bot_ts} 角色" if bot_ts else f"[{idx}] 角色"
            lines.append(f"{user_prefix}: {u.get('content', '')}")
            lines.append(f"{bot_prefix}: {a.get('content', '')}")
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
        # min_check_length 仅对分析用户输入的规则生效；ai_only 模式 user_text 为空时跳过此过滤
        min_len = max(0, rule.get("min_check_length", 0))
        if min_len > 0 and user_text and len(user_text) < min_len:
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

        if self.whitelist and event.get_sender_id() in self.whitelist:
            self._log(event, f"用户 {event.get_sender_id()} 在白名单中，跳过检测")
            return

        enabled_rules = [r for r in self.rules if r.get("enabled", True)]
        if not enabled_rules:
            return

        user_text = self._extract_user_text(event)
        # 不在此处因 user_text 空就 return：ai_only 规则可能只依赖历史，不分析当前输入
        # 后续 (text_val.strip() or hist_block.strip()) 过滤会处理全空情况

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

        # history_source 取值：both / user_only / ai_only
        # both → 内部 mode "all"（user+assistant 配对）
        # user_only → 仅用户历史
        # ai_only → 仅 AI 历史，且 {text}（用户当前输入）会被置空
        _HISTORY_MODE_MAP = {
            "both": "all",
            "user_only": "user_only",
            "ai_only": "ai_only",
        }

        history_cache: dict[tuple[int, str], str] = {}

        async def get_history_text(rounds: int, mode: str) -> str:
            if rounds <= 0 or not cid:
                return ""
            key = (rounds, mode)
            if key in history_cache:
                return history_cache[key]
            # ai_only 模式下不需要在去重时引用 current_user_text
            dedup_text = user_text if mode != "ai_only" else ""
            text = await self._get_recent_history(
                umo, user_id, cid, dedup_text, rounds, mode=mode
            )
            history_cache[key] = text
            return text

        # 按规则构造 (text_value, history_block) 二元组
        rule_inputs: list[tuple[dict, str, str]] = []  # (rule, text_value, history_block)
        for r in enabled_rules:
            source = r.get("history_source", "both")
            mode = _HISTORY_MODE_MAP.get(source, "all")
            rounds = max(0, min(10, r.get("history_rounds", 0)))
            # 拉历史
            if rounds > 0 and cid:
                hist_text = await get_history_text(rounds, mode)
                hist_block = f"<history>\n{hist_text}\n</history>" if hist_text else ""
            else:
                hist_block = ""
            # 决定 {text}：ai_only 模式下置空（不分析当前用户输入）
            text_val = "" if source == "ai_only" else user_text
            rule_inputs.append((r, text_val, hist_block))

        # 过滤：text 与 history 全空则跳过（无内容可分析）
        tasks = [
            self._check(event, text_val, r, provider_id, hist_block)
            for (r, text_val, hist_block) in rule_inputs
            if text_val.strip() or hist_block.strip()
        ]
        if not tasks:
            return
        results = await asyncio.gather(*tasks)
        checked_rules = [
            r for (r, text_val, hist_block) in rule_inputs
            if text_val.strip() or hist_block.strip()
        ]

        warnings = []
        for rule, (flagged, detail) in zip(checked_rules, results):
            if flagged:
                tpl = rule.get("warning_template", "")
                warnings.append(
                    tpl.replace("{detail}", detail) if tpl
                    else f"⚠️ 规则 '{rule['name']}' 检测到异常（{detail}）"
                )
                logger.info(f"{self._tag(event)} '{rule['name']}' 触发: {detail}")

        if warnings:
            part = TextPart(text="\n\n".join(warnings))
            mark_as_temp = getattr(part, "mark_as_temp", None)
            if callable(mark_as_temp):
                part = mark_as_temp()
            req.extra_user_content_parts.append(part)
            self._log(event, f"已注入 {len(warnings)} 条警示")
