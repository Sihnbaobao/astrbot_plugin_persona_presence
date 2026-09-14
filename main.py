"""
Persona Presence - 人格自主参与插件
基于AI参与判断的群聊与私聊增强插件，让bot更懂氛围

重构核心原则：
- 插件只决定是否进入正式回复，以及传递最小参与依据；不决定回复正文
- 回复内容完全交给 AstrBot 原始链路（用户设定的人格 + 平台默认 prompt）
- 不再注入任何系统行为指令/情绪/注意力/主动对话等文本到 system_prompt / prompt

保留功能：
1. AI参与判断 - 智能决定是否回复消息（DecisionAI，输出结构化参与结果）
3. 关键词触发 - 特定词触发（可配智能模式）
4. @消息与普通消息同样由参与判断
5. 图片识别（转文字/多模态直传）、表情包标记、媒体路径内联
6. 转发消息解析、新成员入群解析
7. 黑名单（用户/关键词）
8. 时间戳/发送者标注（群聊里 AI 只比私聊多知道"谁在说话"）
9. 记忆注入（livingmemory 集成）
10. AstrBot 插件页管理控制台（Dashboard 内嵌，卡片式可视化）
11. 戳一戳（回复后戳/反戳/戳过追踪）
12. Smart 并发合并
13. 指令过滤、@全体成员/@他人过滤、重复回复过滤、内容过滤
14. 官方历史同步（用户消息/AI回复/缓存转正）

删除功能（详见 docs/REFACTOR_DESIGN.md）：
情绪系统、注意力机制、主动对话、等待窗口、对话疲劳、
错字生成、打字模拟、拟人模式、消息质量评分、回复密度、频率调整、
动态时间段概率、工具提醒文本注入、SystemPromptRewriter 差分重写

作者/维护: Sihnbaobao
版本: 1.1.2（Persona Presence 参与判断重构）
"""

import asyncio
import copy
import hashlib
import json
import math
import os
import re
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import filter
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.provider.entities import ProviderRequest

# 导入保留的工具模块
from .utils import (
    EMOJI_MARKER,
    CommandMixin,
    ContextManager,
    DecisionAI,
    EmojiDetector,
    ImageHandler,
    KeywordChecker,
    MemoryInjector,
    MentionMixin,
    MessageCleaner,
    MessageProcessor,
    PlatformLTMHelper,
    PokeMixin,
    ProbabilityManager,
    ReplyHandler,
    SaveMixin,
    SmartConcurrentManager,
)
from .utils.image_description_cache import ImageDescriptionCache
from .utils.message_cache_manager import MessageCacheManager
from .utils.participation import ParticipationDecision, ParticipationThrottle
from .utils.private_conversation_state import PrivateConversationState


@register(
    "astrbot_plugin_persona_presence",
    "Sihnbaobao",
    "让当前 Persona 按兴趣、关系和当下意愿选择是否参与对话的增强插件",
    "1.1.2",
    "https://github.com/Sihnbaobao/astrbot_plugin_persona_presence",
)
class PersonaPresence(PokeMixin, MentionMixin, CommandMixin, SaveMixin, Star):
    """
    Persona Presence 插件主类

    采用事件监听而非消息拦截，确保与其他插件兼容
    """

    # 重复回复缓存大小硬上限
    _DUPLICATE_CACHE_SIZE_LIMIT = 50

    # Keep lazy relevance checks bounded when a Smart burst contains many images.
    _LAZY_IMAGE_CANDIDATE_LIMIT = 12
    _PRIVATE_LATE_NIGHT_START_HOUR = 1
    _PRIVATE_LATE_NIGHT_END_HOUR = 7
    _PRIVATE_LATE_NIGHT_ACTIVE_SECONDS = 45 * 60

    # ============================================================
    # 初始化
    # ============================================================

    def __init__(self, context: Context, config: AstrBotConfig):
        """
        初始化插件

        Args:
            context: AstrBot的Context对象，包含各种API
            config: 插件配置
        """
        super().__init__(context)
        self.context = context
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self._web_apis_registered = False

        # V2.2.0：旧版平铺配置 → 分组结构一次性迁移（必须先于所有配置读取）
        self._migrate_legacy_flat_config()

        # ========== 基础配置 ==========
        self.enable_group_chat = self._cfg_bool("enable_group_chat", True)
        self.enable_private_chat = self._cfg_bool("enable_private_chat", False)
        self.debug_mode = self._cfg_bool("enable_debug_log", False)
        self.enabled_groups = self._cfg_list("enabled_groups", [])
        self.enabled_private_users = self._cfg_list("enabled_private_users", [])
        self.takeover_private_reply = self._cfg_bool("takeover_private_reply", True)
        self.private_reply_mode = self._cfg_choice(
            "private_reply_mode", "direct", {"direct", "decide"}
        )

        # ========== Private chat media policy ==========
        media_modes = {"ignore", "decide", "always"}
        legacy_media_mode = self._cfg_choice(
            "private_media_mode", "decide", media_modes
        )
        self.private_image_mode = self._cfg_choice(
            "private_image_mode", legacy_media_mode, media_modes
        )
        self.private_emoji_mode = self._cfg_choice(
            "private_emoji_mode", "ignore", media_modes
        )
        self.private_collapse_duplicate_emoji = self._cfg_bool(
            "private_collapse_duplicate_emoji", True
        )
        self.private_duplicate_emoji_window_ms = self._cfg_int(
            "private_duplicate_emoji_window_ms", 1500, minimum=0, maximum=600_000
        )

        # ========== Compatibility settings ==========

        # ========== 决策AI（参与判断）配置 ==========
        self.decision_ai_provider_id = self._cfg_str("decision_ai_provider_id", "")
        self.decision_ai_include_persona = self._cfg_bool(
            "decision_ai_include_persona", True
        )
        self.decision_ai_persona_name = self._cfg_str("decision_ai_persona_name", "")
        self.decision_ai_extra_prompt = self._cfg_str("decision_ai_extra_prompt", "")
        self.decision_ai_timeout = self._cfg_int(
            "decision_ai_timeout", 30, minimum=1, maximum=600
        )
        self.decision_ai_prompt_mode = self._cfg_choice(
            "decision_ai_prompt_mode", "append", {"append", "override"}
        )
        self.private_decision_ai_extra_prompt = self._cfg_str(
            "private_decision_ai_extra_prompt", ""
        )
        self.private_decision_ai_prompt_mode = self._cfg_choice(
            "private_decision_ai_prompt_mode", "append", {"append", "override"}
        )
        self.private_decision_ai_reply_tendency = self._cfg_choice(
            "private_decision_ai_reply_tendency",
            "persona",
            {"persona", "reserved", "active"},
        )
        self.decision_ai_reply_tendency = self._cfg_choice(
            "decision_ai_reply_tendency",
            "persona",
            {"persona", "reserved", "active"},
        )
        self.enable_decision_ai_reasoning = self._cfg_bool(
            "enable_decision_ai_reasoning", False
        )
        self.decision_ai_reasoning_log = self._cfg_bool(
            "decision_ai_reasoning_log", False
        )
        self.decision_ai_reasoning_log_mode = self._cfg_choice(
            "decision_ai_reasoning_log_mode",
            "processed",
            {"raw", "processed"},
        )
        self.judgment_reasoning_start_marker = self._cfg_str(
            "judgment_reasoning_start_marker", "[[GCP_REASONING_START]]"
        )
        self.judgment_reasoning_end_marker = self._cfg_str(
            "judgment_reasoning_end_marker", "[[GCP_REASONING_END]]"
        )
        self.ambient_reply_min_interval_seconds = self._cfg_float(
            "ambient_reply_min_interval_seconds", 45.0, minimum=0.0, maximum=86_400.0
        )
        self.ambient_reply_window_seconds = self._cfg_float(
            "ambient_reply_window_seconds", 600.0, minimum=0.0, maximum=86_400.0
        )
        self.ambient_reply_max_per_window = self._cfg_int(
            "ambient_reply_max_per_window", 4, minimum=0, maximum=1000
        )

        # ========== 回复配置 ==========
        self.reply_ai_extra_prompt = self._cfg_str("reply_ai_extra_prompt", "")
        self.reply_ai_prompt_mode = self._cfg_choice(
            "reply_ai_prompt_mode", "append", {"append", "override"}
        )
        self.include_timestamp = self._cfg_bool("include_timestamp", True)
        self.include_sender_info = self._cfg_bool("include_sender_info", True)
        self.collapse_reply_newlines = self._cfg_bool("collapse_reply_newlines", False)

        # ========== 上下文配置 ==========
        self.max_context_messages = self._cfg_int(
            "max_context_messages", -1, minimum=-1, maximum=10_000
        )
        self.pending_cache_max_count = self._cfg_int(
            "pending_cache_max_count", 10, minimum=1, maximum=10_000
        )
        self.pending_cache_ttl_seconds = self._cfg_int(
            "pending_cache_ttl_seconds", 1800, minimum=60, maximum=86_400
        )

        # ========== 转发/入群解析配置 ==========
        # ========== 图片处理配置 ==========
        self.enable_image_processing = self._cfg_bool("enable_image_processing", False)
        self.image_read_mode = self._cfg_choice(
            "image_read_mode", "lazy", {"lazy", "eager"}
        )
        self.image_to_text_scope = self._cfg_choice(
            "image_to_text_scope",
            "all",
            {"all", "mention_only", "at_only", "keyword_only"},
        )
        self.image_to_text_provider_id = self._cfg_str("image_to_text_provider_id", "")
        self.image_to_text_prompt = "请详细描述这张图片的内容"
        self.image_to_text_timeout = 60
        self.max_images_per_message = self._cfg_int(
            "max_images_per_message", 10, minimum=1, maximum=100
        )
        self.enable_image_description_cache = self._cfg_bool(
            "enable_image_description_cache", False
        )
        self.image_description_cache_max_entries = self._cfg_int(
            "image_description_cache_max_entries", 500, minimum=10, maximum=10_000
        )
        self.gcp_clear_image_cache_allowed_user_ids = self._cfg_list(
            "gcp_clear_image_cache_allowed_user_ids", []
        )
        self.platform_image_caption_max_wait = self._cfg_float(
            "platform_image_caption_max_wait", 2.0, minimum=0.0, maximum=30.0
        )
        self.platform_image_caption_retry_interval = self._cfg_int(
            "platform_image_caption_retry_interval", 50, minimum=1, maximum=10_000
        )
        self.platform_image_caption_fast_check_count = self._cfg_int(
            "platform_image_caption_fast_check_count", 5, minimum=0, maximum=100
        )

        # ========== 表情包标记配置 ==========
        self.enable_emoji_filter = self._cfg_bool("enable_emoji_filter", False)
        self.emoji_probability_decay = 0.7
        self.emoji_decay_min_probability = 0.1

        # ========== 记忆注入配置（livingmemory） ==========
        self.enable_memory_injection = self._cfg_bool("enable_memory_injection", False)
        self.memory_plugin_mode = self._cfg_choice(
            "memory_plugin_mode", "auto", {"auto", "legacy", "livingmemory"}
        )
        self.memory_insertion_timing = "post_decision"
        self.livingmemory_top_k = self._cfg_int(
            "livingmemory_top_k", 5, minimum=1, maximum=100
        )
        self.livingmemory_version = self._cfg_choice(
            "livingmemory_version", "auto", {"auto", "v1", "v2"}
        )
        self.livingmemory_persona_compat_mode = self._cfg_choice(
            "livingmemory_persona_compat_mode",
            "auto",
            {"auto", "resolver_only", "legacy_only", "off"},
        )

        # ========== 关键词/黑名单配置 ==========
        self.trigger_keywords = self._cfg_list("trigger_keywords", [])
        self.blacklist_keywords = self._cfg_list("blacklist_keywords", [])
        self.keyword_smart_mode = self._cfg_bool(
            "keyword_smart_mode", True
        )  # 默认：关键词命中（含bot名字/被@）也交给参与判断
        self.takeover_group_reply = self._cfg_bool(
            "takeover_group_reply", True
        )  # 默认：接管群聊回复（stop_event 挡住主对话，避免 @/关键词被兜底必回）
        self.group_reply_scope = self._cfg_choice(
            "group_reply_scope", "ambient", {"addressed", "ambient"}
        )
        self.enable_user_blacklist = self._cfg_bool("enable_user_blacklist", False)
        self.blacklist_user_ids = self._cfg_list("blacklist_user_ids", [])

        # ========== 指令过滤配置 ==========
        self.enable_command_filter = self._cfg_bool("enable_command_filter", True)
        self.command_prefixes = self._cfg_list("command_prefixes", ["/", "!", "#"])
        self.enable_full_command_detection = self._cfg_bool(
            "enable_full_command_detection", False
        )
        self.full_command_list = self._cfg_list(
            "full_command_list", ["new", "help", "reset"]
        )
        self.enable_command_prefix_match = self._cfg_bool(
            "enable_command_prefix_match", False
        )
        self.command_prefix_match_list = self._cfg_list("command_prefix_match_list", [])
        # ========== @消息过滤配置 ==========
        self.enable_ignore_at_others = self._cfg_bool("enable_ignore_at_others", False)
        self.ignore_at_others_mode = self._cfg_choice(
            "ignore_at_others_mode", "strict", {"strict", "allow_with_bot"}
        )
        self.enable_ignore_at_all = self._cfg_bool("enable_ignore_at_all", False)
        self.ignore_at_all_enabled = self.enable_ignore_at_all
        self.at_all_message_mode = self._cfg_choice(
            "at_all_message_mode", "skip_probability", {"skip_probability", "skip_all"}
        )

        # ========== 戳一戳配置 ==========
        self.poke_message_mode = self._cfg_choice(
            "poke_message_mode", "bot_only", {"ignore", "bot_only", "all"}
        )
        self.poke_bot_skip_probability = True
        self.poke_after_reply_enabled = self._cfg_bool("enable_poke_after_reply", False)
        self.poke_after_reply_probability = self._cfg_float(
            "poke_after_reply_probability", 0.15, minimum=0.0, maximum=1.0
        )
        self.poke_after_reply_delay = self._cfg_float(
            "poke_after_reply_delay", 0.5, minimum=0.0, maximum=60.0
        )
        self.poke_trace_enabled = self._cfg_bool("enable_poke_trace_prompt", False)
        self.poke_trace_max_tracked_users = self._cfg_int(
            "poke_trace_max_tracked_users", 5, minimum=1, maximum=1000
        )
        self.poke_trace_ttl_seconds = self._cfg_int(
            "poke_trace_ttl_seconds", 300, minimum=1, maximum=86_400
        )
        self.poke_enabled_groups = []  # 精简后固定默认：全部群可用戳一戳

        # 反戳概率（0=禁用，1=必定反戳并丢弃本插件处理）
        self.poke_reverse_on_poke_probability = self._cfg_float(
            "poke_reverse_on_poke_probability", 0.0, minimum=0.0, maximum=1.0
        )

        # ========== 去重过滤配置 ==========
        self.enable_duplicate_filter = self._cfg_bool("enable_duplicate_filter", True)
        self.duplicate_filter_check_count = 5
        self.enable_duplicate_time_limit = True
        self.duplicate_filter_time_limit = 1800

        # ========== 并发/Smart配置 ==========
        self.concurrent_mode = self._cfg_choice(
            "concurrent_mode", "legacy", {"legacy", "smart"}
        )
        self.concurrent_wait_max_loops = self._cfg_int(
            "concurrent_wait_max_loops", 10, minimum=1, maximum=1000
        )
        self.concurrent_wait_interval = self._cfg_float(
            "concurrent_wait_interval", 1.0, minimum=0.1, maximum=60.0
        )
        self.enable_smart_batch_reply_hint = self._cfg_bool(
            "enable_smart_batch_reply_hint", True
        )
        self.smart_concurrent_merge_wait = self._cfg_float(
            "smart_concurrent_merge_wait", 30.0, minimum=0.1, maximum=600.0
        )
        self.smart_concurrent_max_batch_size = self._cfg_int(
            "smart_concurrent_max_batch_size", 20, minimum=1, maximum=1000
        )
        self.smart_concurrent_claim_delay = self._cfg_float(
            "smart_concurrent_claim_delay", 0.3, minimum=0.0, maximum=60.0
        )
        self.private_concurrent_mode = self._cfg_choice(
            "private_concurrent_mode", "smart", {"legacy", "smart"}
        )
        self.private_batch_wait_ms = self._cfg_int(
            "private_batch_wait_ms", 4500, minimum=0, maximum=600_000
        )
        self.private_batch_max_size = self._cfg_int(
            "private_batch_max_size", 10, minimum=1, maximum=1000
        )

        # ========== 性能警告阈值 ==========
        self.reply_timeout_warning_threshold = 120
        self.reply_generation_timeout_warning = 60

        # ========== 桌面端模式（AstrBot Desktop 兼容） ==========
        self.desktop_mode_setting = self._cfg_choice(
            "desktop_mode", "auto", {"auto", "force_desktop", "force_standard"}
        )

        # ========== 数据目录 ==========
        try:
            data_dir = Path(self.context.get_data_dir()) / "group_chat_plus"
        except Exception:
            data_dir = Path.cwd() / "data" / "group_chat_plus"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.plugin_data_dir = str(data_dir)

        # ========== 管理器初始化 ==========
        # 概率管理器
        ProbabilityManager.initialize({})

        # 上下文历史统一由 AstrBot 官方存储管理。

        # 消息缓存管理器（统一管理待决策消息的缓存）
        self.cache_manager = MessageCacheManager(
            cache_ttl_seconds=self.pending_cache_ttl_seconds,
            max_cache_count=self.pending_cache_max_count,
            debug_mode=self.debug_mode,
            include_timestamp=self.include_timestamp,
            include_sender_info=self.include_sender_info,
        )
        self.pending_messages_cache = self.cache_manager.pending_messages_cache

        # 图片描述缓存（省钱）
        self.image_description_cache = ImageDescriptionCache(
            data_dir=str(data_dir),
            max_entries=self.image_description_cache_max_entries,
            enabled=self.enable_image_description_cache,
        )

        # ========== 状态容器 ==========
        # 标记本插件正在处理的消息（用于 after_message_sent 筛选）
        self.processing_sessions: dict[str, str] = {}
        # 并发控制锁
        self.concurrent_lock = asyncio.Lock()
        # 群聊消息到达顺序计数器（Smart 排序）
        self._arrival_seq_counter: int = 0
        # Smart 批次快照 {processing_id: [cached_message_dict, ...]}
        self._smart_batch_snapshots: dict[str, list[dict[str, Any]]] = {}
        # 会话级流程 owner {chat_id: {"owner": str, "processing_id": str, "started_at": float}}
        self._chat_flow_owners: dict[str, dict[str, Any]] = {}
        # 消息缓存快照（供 after_message_sent 使用）{message_id: cached_message_dict}
        self._message_cache_snapshots: dict[str, dict[str, Any]] = {}
        # 指令消息标记 {message_id: timestamp}
        self.command_messages: dict[str, float] = {}
        # 最近回复缓存（去重）{chat_id: [{"content": str, "timestamp": float}]}
        self.recent_replies_cache: dict[str, list[dict[str, Any]]] = {}
        self.raw_reply_cache: dict[str, str] = {}
        # Recent private emoji signatures {chat_id: {signature: monotonic_time}}.
        self._private_recent_emoji: dict[str, dict[str, float]] = {}
        # 多轮工具调用累积AI回复文本 {message_id: [text, ...]}
        self._pending_bot_replies: dict[str, list[str]] = {}
        # 群聊消息序号 {chat_key: int}
        self._group_message_seq: dict[str, int] = {}
        # Last reply trigger {chat_key: {"sender_id": str, "group_seq": int}}.
        self._last_bot_reply_context: dict[str, dict[str, Any]] = {}
        # agent完成标志 set[message_id]
        self._agent_done_flags: set[str] = set()
        # 重复消息拦截标记 {message_id: True}
        self._duplicate_blocked_messages: dict[str, bool] = {}
        # 已保存消息标记 {message_id: timestamp}
        self._saved_messages: dict[str, float] = {}
        # 平台重复推送去重 {source_event_id: timestamp}
        self._seen_message_ids: dict[str, float] = {}
        # 戳一戳追踪记录 {chat_id: OrderedDict{user_id: expire_at}}
        self.poke_trace_records: dict[str, Any] = {}
        # AI错误消息标记 set[message_id]
        self._ai_error_message_ids: set[str] = set()
        self.participation_throttle = ParticipationThrottle(
            min_interval_seconds=self.ambient_reply_min_interval_seconds,
            window_seconds=self.ambient_reply_window_seconds,
            max_replies_per_window=self.ambient_reply_max_per_window,
        )
        self._private_conversation_state = PrivateConversationState()
        self._private_wake_tasks: dict[str, asyncio.Task] = {}
        self._private_late_night_active_until: dict[str, float] = {}

        # Smart 并发参数同步
        try:
            SmartConcurrentManager._EXPIRE_SECONDS = float(
                self.smart_concurrent_merge_wait
            )
        except (TypeError, ValueError):
            pass
        try:
            SmartConcurrentManager._MAX_BATCH_SIZE = max(
                1, int(self.smart_concurrent_max_batch_size)
            )
        except (TypeError, ValueError):
            pass

        # 日志输出
        logger.info("=" * 50)
        logger.info("Persona Presence 已加载 - 1.1.2（人格自主参与）")
        logger.info(
            f"🔘 群聊功能总开关: {'✓ 已启用' if self.enable_group_chat else '✗ 已禁用'}"
        )

        logger.info(f"启用的群组: {self.enabled_groups} (留空=全部)")
        logger.info(f"详细日志模式: {'开启' if self.debug_mode else '关闭'}")
        logger.info(f"Group reply scope: {self.group_reply_scope}")
        logger.info(f"Image read mode: {self.image_read_mode}")
        logger.info("=" * 50)

    # ============================================================
    # 生命周期
    # ============================================================

    async def initialize(self) -> None:
        """Initialize runtime resources and register plugin APIs."""
        session_created = False
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
            session_created = True

        try:
            SmartConcurrentManager._EXPIRE_SECONDS = self.smart_concurrent_merge_wait
            SmartConcurrentManager._MAX_BATCH_SIZE = (
                self.smart_concurrent_max_batch_size
            )
            self._register_web_apis()
        except Exception:
            if session_created:
                await self._close_http_session()
            raise

    async def terminate(self) -> None:
        """Release runtime resources when the plugin is disabled or reloaded."""
        await self._close_http_session()
        self._web_apis_registered = False

        wake_tasks = list(self._private_wake_tasks.values())
        for wake_task in wake_tasks:
            wake_task.cancel()
        if wake_tasks:
            await asyncio.gather(*wake_tasks, return_exceptions=True)
        self._private_wake_tasks.clear()

        try:
            await SmartConcurrentManager.reset()
        except Exception:
            logger.warning(
                "Failed to reset SmartConcurrentManager state", exc_info=True
            )

        try:
            await ProbabilityManager.reset()
        except Exception:
            logger.warning("Failed to reset ProbabilityManager state", exc_info=True)

        async with self.concurrent_lock:
            self.processing_sessions.clear()
            self._smart_batch_snapshots.clear()
            self._chat_flow_owners.clear()
            self._message_cache_snapshots.clear()
            self.command_messages.clear()
            self.recent_replies_cache.clear()
            self.raw_reply_cache.clear()
            self.pending_messages_cache.clear()
            self._group_message_seq.clear()
            self._last_bot_reply_context.clear()
            self._arrival_seq_counter = 0
            self._pending_bot_replies.clear()
            self._agent_done_flags.clear()
            self._duplicate_blocked_messages.clear()
            self._saved_messages.clear()
            self._seen_message_ids.clear()
            self._private_recent_emoji.clear()
            self.poke_trace_records.clear()
            self._ai_error_message_ids.clear()
            self.participation_throttle.reset()
            self._private_conversation_state.reset()
            self._private_late_night_active_until.clear()

    async def _close_http_session(self) -> None:
        """Close the owned HTTP session and make repeated cleanup harmless."""
        session = self.session
        self.session = None
        if session is None or session.closed:
            return
        try:
            await session.close()
        except Exception:
            logger.warning("Failed to close plugin HTTP session", exc_info=True)

    def _get_http_session(self) -> aiohttp.ClientSession:
        """Return the active HTTP session or raise a clear lifecycle error."""
        session = self.session
        if session is None or session.closed:
            raise RuntimeError("PersonaPresence HTTP session is not initialized")
        return session

    # ============================================================
    # 插件页 Web API（AstrBot Dashboard 插件页，见 pages/control/）
    # V2.1.0：独立 Web 面板已移除，管理界面改为 AstrBot 插件页
    # （Dashboard 内嵌 iframe），后端 API 由此注册。
    # ============================================================

    # ============================================================
    # 配置访问兼容层（V2.2.0 配置分组：_conf_schema.json 按功能分栏）
    # 旧版平铺配置会在首次加载时自动迁移到分组结构。
    # ============================================================

    def _migrate_legacy_flat_config(self) -> None:
        """Migrate legacy flat keys into grouped configuration storage.

        Existing grouped values win over legacy flat values. Each schema key is
        migrated independently so a partially edited grouped configuration does
        not cause unrelated legacy values to be lost.
        """
        try:
            schema = getattr(self.config, "schema", None) or {}
            groups = {
                group_name: metadata.get("items", {})
                for group_name, metadata in schema.items()
                if isinstance(metadata, dict)
                and metadata.get("type") == "object"
                and isinstance(metadata.get("items"), dict)
            }
            if not groups:
                return

            migrated = 0
            legacy_keys = set()
            for group_name, items in groups.items():
                current_group = self.config.get(group_name)
                grouped_values = (
                    dict(current_group) if isinstance(current_group, dict) else {}
                )
                for key in items:
                    legacy_keys.add(key)
                    if key in grouped_values or key not in self.config:
                        continue
                    grouped_values[key] = self.config[key]
                    migrated += 1
                if grouped_values and grouped_values != current_group:
                    self.config[group_name] = grouped_values

            for key in legacy_keys:
                if key in self.config:
                    del self.config[key]

            if migrated:
                self.config.save_config()
                logger.info(f"⚙️ 配置已迁移到分组结构（{migrated} 项）")
        except Exception as e:
            logger.warning(f"⚙️ 配置迁移失败（继续使用默认值）: {e}")

    def _cfg(self, key: str, default: Any = None) -> Any:
        """Read a configuration value from grouped or flat storage."""
        try:
            schema = getattr(self.config, "schema", None) or {}
            for gname, gmeta in schema.items():
                if (
                    isinstance(gmeta, dict)
                    and isinstance(gmeta.get("items"), dict)
                    and key in gmeta["items"]
                ):
                    gval = self.config.get(gname)
                    if isinstance(gval, dict) and key in gval:
                        return gval[key]
                    return default
        except Exception:
            pass
        return self.config.get(key, default)

    @staticmethod
    def _coerce_bool(value: Any, default: bool) -> bool:
        """Convert a configuration value to a boolean safely.

        Args:
            value: Raw configuration value.
            default: Value returned when the input is not recognized.

        Returns:
            A normalized boolean value.
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and math.isfinite(value):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on", "y"}:
                return True
            if normalized in {"0", "false", "no", "off", "n"}:
                return False
        return default

    @staticmethod
    def _coerce_int(
        value: Any,
        default: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        """Convert and clamp an integer configuration value.

        Args:
            value: Raw configuration value.
            default: Fallback integer.
            minimum: Optional inclusive lower bound.
            maximum: Optional inclusive upper bound.

        Returns:
            A validated integer within the requested bounds.
        """
        try:
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("non-finite number")
            result = int(value)
        except (TypeError, ValueError, OverflowError):
            result = default
        if minimum is not None:
            result = max(minimum, result)
        if maximum is not None:
            result = min(maximum, result)
        return result

    @staticmethod
    def _coerce_float(
        value: Any,
        default: float,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        """Convert and clamp a finite floating-point configuration value.

        Args:
            value: Raw configuration value.
            default: Fallback float.
            minimum: Optional inclusive lower bound.
            maximum: Optional inclusive upper bound.

        Returns:
            A validated finite float within the requested bounds.
        """
        try:
            result = float(value)
            if not math.isfinite(result):
                raise ValueError("non-finite number")
        except (TypeError, ValueError, OverflowError):
            result = default
        if minimum is not None:
            result = max(minimum, result)
        if maximum is not None:
            result = min(maximum, result)
        return result

    @staticmethod
    def _coerce_choice(value: Any, default: str, choices: set[str]) -> str:
        """Return a configured string only when it is an allowed choice.

        Args:
            value: Raw configuration value.
            default: Fallback choice.
            choices: Allowed values.

        Returns:
            A valid choice from choices or default.
        """
        candidate = value.strip() if isinstance(value, str) else value
        return candidate if candidate in choices else default

    @staticmethod
    def _coerce_list(value: Any, default: list[Any]) -> list[Any]:
        """Copy a list-like configuration value without sharing defaults.

        Args:
            value: Raw configuration value.
            default: Fallback list.

        Returns:
            A new list containing normalized string entries.
        """
        source = value if isinstance(value, (list, tuple, set)) else default
        return [
            item.strip() if isinstance(item, str) else str(item)
            for item in source
            if item is not None and str(item).strip()
        ]

    def _cfg_str(self, key: str, default: str) -> str:
        """Read and normalize a string configuration value."""
        value = self._cfg(key, default)
        return value.strip() if isinstance(value, str) else default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        """Read and normalize a boolean configuration value."""
        return self._coerce_bool(self._cfg(key, default), default)

    def _cfg_int(
        self,
        key: str,
        default: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        """Read and normalize an integer configuration value."""
        return self._coerce_int(
            self._cfg(key, default),
            default,
            minimum=minimum,
            maximum=maximum,
        )

    def _cfg_float(
        self,
        key: str,
        default: float,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        """Read and normalize a floating-point configuration value."""
        return self._coerce_float(
            self._cfg(key, default),
            default,
            minimum=minimum,
            maximum=maximum,
        )

    def _cfg_choice(self, key: str, default: str, choices: set[str]) -> str:
        """Read and normalize an enumerated string configuration value."""
        return self._coerce_choice(self._cfg(key, default), default, choices)

    def _cfg_list(self, key: str, default: list[Any]) -> list[Any]:
        """Read and normalize a list configuration value."""
        return self._coerce_list(self._cfg(key, default), default)

    def _set_cfg(self, key, value):
        """按分组结构写入配置；schema 外的键回退到平铺写入。"""
        try:
            schema = getattr(self.config, "schema", None) or {}
            for gname, gmeta in schema.items():
                if (
                    isinstance(gmeta, dict)
                    and isinstance(gmeta.get("items"), dict)
                    and key in gmeta["items"]
                ):
                    gval = self.config.get(gname)
                    if not isinstance(gval, dict):
                        gval = {}
                        self.config[gname] = gval
                    gval[key] = value
                    return
        except Exception:
            pass
        self.config[key] = value

    _PLUGIN_NAME = "astrbot_plugin_persona_presence"

    # ============================================================
    # 插件页配置源：100% 由 _conf_schema.json 驱动
    # _schema_groups() 从 _conf_schema.json 动态读取分组与字段定义，
    # 插件页渲染与 AstrBot 配置页完全一致；全部配置统一由 _conf_schema.json 驱动
    # 有读取但 schema 未展示的隐藏参数。
    # ============================================================

    # schema 键 → 实例属性名 的例外映射（多数键名与属性名相同）
    _ATTR_MAP = {
        "desktop_mode": "desktop_mode_setting",
        "enable_debug_log": "debug_mode",
        "enable_poke_trace_prompt": "poke_trace_enabled",
        "enable_poke_after_reply": "poke_after_reply_enabled",
    }

    def _schema_groups(self):
        """从 _conf_schema.json 动态提取分组与字段定义（与 AstrBot 配置页一致）。"""
        try:
            schema = getattr(self.config, "schema", None) or {}
            groups = []
            for gname, gmeta in schema.items():
                if isinstance(gmeta, dict) and isinstance(gmeta.get("items"), dict):
                    groups.append(
                        {
                            "id": gname,
                            "title": gmeta.get("description") or gname,
                            "hint": gmeta.get("hint", ""),
                            "items": gmeta["items"],
                        }
                    )
            return groups
        except Exception:
            return []

    def _all_editable_keys(self) -> dict:
        """全部可编辑键 → 属性名（全部来自 schema 分组）。"""
        mapping = {}
        for group in self._schema_groups():
            for key in group["items"]:
                mapping[key] = self._ATTR_MAP.get(key, key)
        return mapping

    def _register_web_apis(self) -> None:
        """Register plugin-page APIs once for this plugin instance."""
        if self._web_apis_registered:
            return
        try:
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/status",
                self._api_status,
                ["GET"],
                "插件运行状态总览",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/config/save",
                self._api_save_config,
                ["POST"],
                "保存插件页修改的配置",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/prompts",
                self._api_prompts,
                ["GET"],
                "提示词预览（参与判断/回复生成）",
            )
            self._web_apis_registered = True
            logger.info("✅ 插件页 Web API 已注册（Dashboard 插件页可用）")
        except Exception as e:
            logger.warning(f"插件页 Web API 注册失败（需要 AstrBot v4.25.3+）: {e}")

    async def _api_status(self):
        """返回插件状态总览（供插件页渲染卡片/胶囊）。"""
        from astrbot.api.web import json_response

        values = {}
        for key, attr in self._all_editable_keys().items():
            values[key] = self._cfg(key, getattr(self, attr, None))

        prob_status = getattr(ProbabilityManager, "_probability_status", {}) or {}
        runtime = {
            "probability_session_count": len(prob_status),
            "smart_batch_snapshot_count": len(
                getattr(self, "_smart_batch_snapshots", {})
            ),
            "processing_session_count": len(getattr(self, "processing_sessions", {})),
        }
        groups = self._schema_groups()
        return json_response(
            {
                "version": "1.1.2",
                "values": values,
                "groups": groups,
                "runtime": runtime,
            }
        )

    async def _api_save_config(self):
        """保存插件页提交的配置变更（仅允许白名单内的键）。"""
        from astrbot.api.web import error_response, json_response, request

        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        updates = payload.get("updates") if isinstance(payload, dict) else None
        if not isinstance(updates, dict) or not updates:
            return error_response("updates 必须是非空对象")

        applied = []
        skipped = []
        valid_keys = self._all_editable_keys()
        for key, value in updates.items():
            if key not in valid_keys:
                skipped.append(key)
                continue
            try:
                self._set_cfg(key, value)
            except Exception:
                skipped.append(key)
                continue
            setattr(self, valid_keys[key], value)
            applied.append(key)

        try:
            await asyncio.to_thread(self.config.save_config)
        except Exception as e:
            logger.warning(f"插件页保存配置落盘失败: {e}")

        # Smart 并发参数需同步到类级
        try:
            SmartConcurrentManager._EXPIRE_SECONDS = float(
                self.smart_concurrent_merge_wait
            )
        except (TypeError, ValueError):
            pass
        try:
            SmartConcurrentManager._MAX_BATCH_SIZE = max(
                1, int(self.smart_concurrent_max_batch_size)
            )
        except (TypeError, ValueError):
            pass

        return json_response({"applied": applied, "skipped": skipped})

    def _page_tendency_prompt(
        self, tendency: str | None = None, *, is_private: bool = False
    ) -> str:
        """Build the configured willingness preview for the plugin page."""
        tendency = tendency or self.decision_ai_reply_tendency
        if tendency == "reserved":
            return (
                "\n\n[persona_willingness preset: reserved]\n"
                "在人格意愿判断阶段提高开口门槛：更偏好安静、简短或不打扰；"
                + (
                    "不把问候、弱话题或非必要消息自动变成回复。\n"
                    if is_private
                    else "不改变 ownership / information / continuation / participation 的判定，也不能覆盖 unclear 的立即 no；对 other 也不能强行制造 side 入口。\n"
                )
            )
        if tendency == "active":
            return (
                "\n\n[persona_willingness preset: active]\n"
                "在人格意愿判断阶段降低开口门槛：更愿意接住有内容的提问、分享和有效续话；"
                + (
                    "仍需尊重当前边界和时间，不把每条消息都变成必须回复。\n"
                    if is_private
                    else "开放话题仍需要具体的个人切入点，不把“能回答”变成 yes；不把 reaction、noise 或不明归属变成 substantive，也不能覆盖 other 或 unclear 的立即 no。\n"
                )
            )
        return (
            "\n\n[persona_willingness preset: persona]\n"
            "完全依据当前人格的性格、兴趣、心情、关系和聊天氛围判断是否愿意开口；"
            + (
                "私聊中继续结合当前关系、话题和边界自然回应。\n"
                if is_private
                else "群聊开放消息默认保持安静，只有具体个人切入点才考虑发言。\n"
            )
        )

    async def _api_prompts(self):
        """返回参与判断/回复生成的提示词预览（与真实拼接逻辑保持一致）。"""
        from astrbot.api.web import json_response

        try:
            from .utils.decision_ai import DecisionAI
            from .utils.reply_handler import ReplyHandler
        except Exception:
            return json_response(
                {
                    "decision": {"text": "（无法加载提示词模块）"},
                    "reply": {"text": ""},
                }
            )

        def build_decision_preview(
            base_prompt: str,
            extra_prompt: str,
            prompt_mode: str,
            tendency: str,
            is_private: bool = False,
        ) -> tuple[str, bool]:
            custom = bool(str(extra_prompt or "").strip())
            if custom and prompt_mode == "override":
                text = str(extra_prompt).strip()
            else:
                text = base_prompt
                if custom:
                    text += f"\n\n用户补充说明:\n{str(extra_prompt).strip()}\n"
            text += self._page_tendency_prompt(tendency, is_private=is_private)
            text += DecisionAI.SYSTEM_DECISION_PROMPT_ENDING
            return text, custom

        decision_text, decision_custom = build_decision_preview(
            DecisionAI.SYSTEM_DECISION_PROMPT,
            self.decision_ai_extra_prompt,
            self.decision_ai_prompt_mode,
            self.decision_ai_reply_tendency,
        )
        private_custom_prompt = str(self.private_decision_ai_extra_prompt or "").strip()
        private_fallback = not private_custom_prompt
        private_extra_prompt = (
            self.decision_ai_extra_prompt if private_fallback else private_custom_prompt
        )
        private_prompt_mode = (
            self.decision_ai_prompt_mode
            if private_fallback
            else self.private_decision_ai_prompt_mode
        )
        private_text, private_custom_effective = build_decision_preview(
            DecisionAI.PRIVATE_SYSTEM_DECISION_PROMPT,
            private_extra_prompt,
            private_prompt_mode,
            self.private_decision_ai_reply_tendency,
            is_private=True,
        )
        if private_fallback and self.decision_ai_extra_prompt:
            private_text = (
                "[当前未单独配置私聊补充提示词；以下预览沿用通用参与判断补充说明。]\\n\\n"
                + private_text
            )

        reply_custom = bool(
            self.reply_ai_extra_prompt and str(self.reply_ai_extra_prompt).strip()
        )
        if reply_custom and self.reply_ai_prompt_mode == "override":
            reply_text = str(self.reply_ai_extra_prompt).strip()
        else:
            reply_text = "[发送者标注 + 历史上下文 + 当前消息]"
            if reply_custom:
                reply_text += "\n" + str(self.reply_ai_extra_prompt).strip()
        reply_text += ReplyHandler.PROMPT_ENDING

        root_config = self.context.get_config()
        provider_settings = (
            root_config.get("provider_settings", {})
            if hasattr(root_config, "get")
            else {}
        )
        if not isinstance(provider_settings, dict):
            provider_settings = {}
        global_time_awareness = bool(
            provider_settings.get(
                "datetime_system_prompt",
                root_config.get("datetime_system_prompt", True)
                if hasattr(root_config, "get")
                else True,
            )
        )

        return json_response(
            {
                "decision": {
                    "mode": self.decision_ai_prompt_mode,
                    "has_custom": decision_custom,
                    "extra": str(self.decision_ai_extra_prompt or ""),
                    "text": decision_text,
                },
                "private_decision": {
                    "mode": (
                        self.decision_ai_prompt_mode
                        if private_fallback
                        else self.private_decision_ai_prompt_mode
                    ),
                    "configured_mode": self.private_decision_ai_prompt_mode,
                    "has_custom": private_custom_effective,
                    "extra": str(self.private_decision_ai_extra_prompt or ""),
                    "fallback_to_shared": private_fallback,
                    "shared_has_custom": bool(
                        str(self.decision_ai_extra_prompt or "").strip()
                    ),
                    "time_awareness": global_time_awareness,
                    "time_awareness_source": "AstrBot global datetime_system_prompt",
                    "text": private_text,
                },
                "reply": {
                    "mode": self.reply_ai_prompt_mode,
                    "has_custom": reply_custom,
                    "extra": str(self.reply_ai_extra_prompt or ""),
                    "text": reply_text,
                },
            }
        )

    # ============================================================
    # 重启辅助
    # ============================================================

    async def _get_auth_token(self):
        """获取认证 token（JWT 签发，失败降级密码登录）。"""
        try:
            token = self._generate_jwt_token()
            logger.debug("通过 jwt_secret 生成认证 token 成功")
            return token
        except Exception as e:
            logger.warning(f"通过 jwt_secret 生成 token 失败: {e}，降级尝试密码登录...")

        login_url = f"http://{self.host}:{self.port}/api/auth/login"
        login_data = {
            "username": self.dbc["username"],
            "password": self.dbc["password"],
        }
        session = self._get_http_session()
        async with session.post(login_url, json=login_data) as response:
            if response.status == 200:
                data = await response.json()
                if data and data.get("status") == "ok" and "data" in data:
                    token = data.get("data", {}).get("token")
                    if token:
                        return token
                raise Exception(f"登录响应格式错误: {data}")
            else:
                text = await response.text()
                raise Exception(f"登录失败，状态码: {response.status}, 响应: {text}")

    def _generate_jwt_token(self, dbc_override: dict | None = None) -> str:
        """使用 dashboard 的 jwt_secret 直接签发 JWT，跳过密码登录。"""
        import jwt as _jwt

        dbc = dbc_override if dbc_override is not None else self.dbc
        jwt_secret = dbc.get("jwt_secret", "")
        if not jwt_secret:
            raise ValueError("jwt_secret 不在 dashboard 配置中")

        payload = {
            "username": dbc.get("username", "astrbot"),
            "exp": datetime.now(timezone.utc) + timedelta(days=7),
        }
        return _jwt.encode(payload, jwt_secret, algorithm="HS256")

    def _detect_desktop_mode(self, config) -> bool:
        """多重策略检测是否运行在 AstrBot 桌面端环境。"""
        mode = self.desktop_mode_setting
        if mode == "force_desktop":
            logger.info(
                "🖥️ [桌面端] 用户强制配置为桌面端模式（desktop_mode=force_desktop）"
            )
            return True
        if mode == "force_standard":
            logger.info(
                "🖥️ [桌面端] 用户强制配置为标准版模式（desktop_mode=force_standard）"
            )
            return False

        detected_reason = ""
        if os.environ.get("ASTRBOT_DESKTOP_CLIENT") == "1":
            detected_reason = "env:ASTRBOT_DESKTOP_CLIENT=1"
        if not detected_reason:
            astrbot_root = os.environ.get("ASTRBOT_ROOT", "")
            if astrbot_root:
                try:
                    home = Path.home()
                    root_path = Path(astrbot_root).resolve()
                    if root_path == (home / ".astrbot").resolve():
                        detected_reason = f"path:ASTRBOT_ROOT={astrbot_root}"
                except Exception:
                    pass
        if not detected_reason:
            webui_dir = os.environ.get("ASTRBOT_WEBUI_DIR", "")
            if webui_dir and "resources" in webui_dir.replace("\\", "/").lower():
                detected_reason = f"env:ASTRBOT_WEBUI_DIR={webui_dir}"
        if not detected_reason:
            if os.environ.get("PYTHONNOUSERSITE") == "1" and os.environ.get(
                "ASTRBOT_ROOT"
            ):
                detected_reason = "env:PYTHONNOUSERSITE=1+ASTRBOT_ROOT"

        is_desktop = bool(detected_reason)
        try:
            config["desktop_detected_env"] = detected_reason or "none"
            config.save_config()
        except Exception:
            pass
        return is_desktop

    async def restart_core(self):
        """发送重启请求，重启AstrBot，并记录重启信息。"""
        try:
            if self.is_desktop_mode:
                logger.warning(
                    "🖥️ [桌面端] 即将发送重启请求。桌面端的进程由 Tauri 托管，"
                    "通过 HTTP API 触发的重启可能导致 Tauri 丢失对后端进程的跟踪。"
                    "如重启后出现异常，请通过桌面端托盘菜单手动重启后端。"
                )
            token = await self._get_auth_token()
            headers = {"Authorization": f"Bearer {token}"}
            session = self._get_http_session()
            async with session.post(self.restart_url, headers=headers) as response:
                if response.status == 200:
                    logger.info("系统重启请求已发送")
                else:
                    logger.error(f"重启请求失败，状态码: {response.status}")
                    raise RuntimeError(f"重启请求失败，状态码: {response.status}")
        except Exception as e:
            logger.error(f"发送重启请求时出错: {e}")
            raise e

    # ============================================================
    # 指令过滤与平台 reset 联动
    # ============================================================

    # ============================================================
    # 群聊和私聊入口
    # ============================================================

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=-1,
    )
    async def on_group_message(self, event: AstrMessageEvent):
        """
        Process enabled group and private messages.

        Args:
            event: Incoming AstrBot message event.
        """
        _cleanup_message_id = None
        try:
            is_private_chat = event.is_private_chat()
            takeover_for_event = (
                self.takeover_private_reply
                if is_private_chat
                else self.takeover_group_reply
            )
            if (is_private_chat and not self.enable_private_chat) or (
                not is_private_chat and not self.enable_group_chat
            ):
                return

            # Reset both official history and only this scoped in-memory session.
            _rst_txt = (
                (event.get_message_str() or "").strip().replace("@", "").strip().lower()
            )
            if _rst_txt in ("reset", "/reset"):
                try:
                    await ContextManager.clear_official_history_for_event(
                        self.context, event
                    )
                except Exception as reset_err:
                    logger.warning(
                        f"Reset could not clear official history: {reset_err}",
                        exc_info=True,
                    )
                _raw_reset_chat_id = (
                    event.get_group_id()
                    if not event.is_private_chat()
                    else event.get_sender_id()
                )
                _reset_chat_key = ProbabilityManager.get_chat_key(
                    event.get_platform_name(),
                    event.is_private_chat(),
                    _raw_reset_chat_id,
                )
                self._cancel_private_wake(_reset_chat_key)
                async with self.concurrent_lock:
                    _reset_message_ids = {
                        message_id
                        for message_id, owner_chat_key in self.processing_sessions.items()
                        if owner_chat_key == _reset_chat_key
                    }
                    for message_id in _reset_message_ids:
                        self.processing_sessions.pop(message_id, None)
                        self._message_cache_snapshots.pop(message_id, None)
                        self._smart_batch_snapshots.pop(message_id, None)
                        self._pending_bot_replies.pop(message_id, None)
                        self.raw_reply_cache.pop(message_id, None)
                        self._duplicate_blocked_messages.pop(message_id, None)
                        self._saved_messages.pop(message_id, None)
                        self._ai_error_message_ids.discard(message_id)
                        self._agent_done_flags.discard(message_id)
                    self._chat_flow_owners.pop(_reset_chat_key, None)
                    self._group_message_seq.pop(_reset_chat_key, None)
                    self._last_bot_reply_context.pop(_reset_chat_key, None)
                    self._private_recent_emoji.pop(_reset_chat_key, None)
                    self.poke_trace_records.pop(_reset_chat_key, None)
                    self.pending_messages_cache.pop(_reset_chat_key, None)
                    self.recent_replies_cache.pop(_reset_chat_key, None)
                    if hasattr(self, "_pre_decision_context_by_chat"):
                        self._pre_decision_context_by_chat.pop(_reset_chat_key, None)
                    self.participation_throttle.reset(_reset_chat_key)
                    self._private_conversation_state.clear(_reset_chat_key)
                    self._private_late_night_active_until.pop(_reset_chat_key, None)
                    self._private_conversation_state.clear_pending_messages(
                        _reset_chat_key
                    )
                await SmartConcurrentManager.clear_chat(_reset_chat_key)
                event.stop_event()
                if self.debug_mode:
                    logger.info(
                        "Reset cleared official history and scoped plugin state"
                    )
                return

            # 直接打掉平台产生的真空消息
            _raw_msg_str = event.get_message_str()
            _msg_components = None
            try:
                if hasattr(event, "message_obj") and hasattr(
                    event.message_obj, "message"
                ):
                    _msg_components = event.message_obj.message
            except Exception:
                pass
            if (not _raw_msg_str or not _raw_msg_str.strip()) and not _msg_components:
                # 平台系统事件/真空消息（如进群通知）：不进入参与判断，静默放行
                event.call_llm = True
                return

            msg_id = self._get_processing_id(event)
            source_event_id = self._build_source_event_id(event)
            arrival_seq, _ = self._ensure_arrival_metadata(event)
            if is_private_chat:
                platform_message_id = str(
                    getattr(getattr(event, "message_obj", None), "message_id", "") or ""
                )
                logged_arrival_content = (event.get_message_str() or "").replace(
                    "\n", " | "
                )
                logger.info(
                    f"[Private Smart] Arrival seq={arrival_seq} "
                    f"message_id={platform_message_id or 'unknown'} "
                    f"content={logged_arrival_content[:500]}"
                )
            _cleanup_message_id = msg_id

            # 消息去重（防止平台重复推送）
            current_time = time.time()
            if len(self._seen_message_ids) > 100:
                self._seen_message_ids = {
                    k: v
                    for k, v in self._seen_message_ids.items()
                    if current_time - v < 60
                }
            if source_event_id in self._seen_message_ids:
                if self.debug_mode:
                    logger.info(
                        f"[消息去重] 检测到重复消息 {source_event_id[:30]}...，跳过处理"
                    )
                if (
                    self.takeover_private_reply
                    if is_private_chat
                    else self.takeover_group_reply
                ):
                    try:
                        event.stop_event()
                    except Exception:
                        pass
                return
            self._seen_message_ids[source_event_id] = current_time

            # 指令消息跳过
            if msg_id in self.command_messages:
                if self.debug_mode:
                    logger.info("消息已被标记为指令，跳过处理")
                return

            # 插件兼容性检查：其他插件已回复则跳过AI处理但保留缓存
            if getattr(event, "_has_send_oper", False):
                try:
                    if self._is_enabled(event):
                        raw_chat_id = (
                            event.get_group_id()
                            if not is_private_chat
                            else event.get_sender_id()
                        )
                        chat_id = ProbabilityManager.get_chat_key(
                            event.get_platform_name(), is_private_chat, raw_chat_id
                        )
                        message_text = (
                            MessageCleaner.extract_raw_message_from_event(
                                event, self_id=str(event.get_self_id())
                            )
                            or event.get_message_str()
                            or ""
                        )
                        if message_text.strip():
                            try:
                                mention_info = await self._check_mention_others(event)
                            except Exception:
                                mention_info = None
                            cached_message = {
                                "role": "user",
                                "content": message_text,
                                "timestamp": current_time,
                                "message_id": msg_id,
                                "sender_id": event.get_sender_id(),
                                "sender_name": event.get_sender_name(),
                                "message_timestamp": event.message_obj.timestamp
                                if hasattr(event, "message_obj")
                                and hasattr(event.message_obj, "timestamp")
                                else None,
                                "mention_info": mention_info,
                                "is_at_message": False,
                                "has_trigger_keyword": False,
                                "poke_info": None,
                                "persistent_poke_event_text": "",
                                "image_urls": [],
                                "is_at_all_message": False,
                                "is_empty_at": False,
                            }
                            self.cache_manager.add_to_cache(
                                chat_id,
                                cached_message,
                                source="插件兼容-其他插件已回复",
                            )
                except Exception as e:
                    logger.warning(f"[插件兼容] 缓存消息时出错（不影响后续）: {e}")
                return

            # 用户黑名单
            if self._is_user_blacklisted(event):
                if takeover_for_event:
                    event.stop_event()
                return

            # @全体成员过滤
            if self._should_ignore_at_all(event):
                if self.debug_mode:
                    logger.info("[@全体成员检测] 消息包含@全体成员，本插件跳过处理")
                if takeover_for_event:
                    event.stop_event()
                return

            try:
                event.set_extra("is_at_all_message", self._is_at_all_message(event))
            except Exception as e:
                logger.warning(f"[@全体成员识别] 写入事件标记失败，按普通消息继续: {e}")

            # 过滤伪造的戳一戳文本标识符
            message_str = event.get_message_str()
            if MessageCleaner.is_only_poke_marker(message_str):
                if self.debug_mode:
                    logger.info(
                        "【戳一戳标识符过滤】消息只包含[Poke:poke]标识符，跳过处理"
                    )
                if takeover_for_event:
                    event.stop_event()
                return

            # @他人过滤
            if self._should_ignore_at_others(event):
                if self.debug_mode:
                    logger.info("[@他人检测] 消息符合忽略条件，本插件跳过处理")
                if takeover_for_event:
                    event.stop_event()
                return

            # 戳一戳消息检测（忽略配置）
            poke_result = await self._check_poke_message(event)
            if poke_result.get("is_poke") and poke_result.get("should_ignore"):
                if self.debug_mode:
                    logger.info("【戳一戳检测】消息符合忽略条件，本插件跳过处理")
                if takeover_for_event:
                    event.stop_event()
                return

            # 处理群消息
            async for result in self._process_message(event):
                yield result
        except Exception as e:
            logger.error(f"处理群消息时发生错误: {e}", exc_info=True)
            try:
                is_private_on_error = event.is_private_chat()
                takeover_on_error = (
                    self.takeover_private_reply
                    if is_private_on_error
                    else self.takeover_group_reply
                )
                if takeover_on_error:
                    event.stop_event()
            except Exception:
                pass
        finally:
            # 安全网：确保 processing_sessions 条目不会泄漏
            if _cleanup_message_id:
                async with self.concurrent_lock:
                    self.processing_sessions.pop(_cleanup_message_id, None)
                self._message_cache_snapshots.pop(_cleanup_message_id, None)
                self._duplicate_blocked_messages.pop(_cleanup_message_id, None)
                self._smart_batch_snapshots.pop(_cleanup_message_id, None)

    # ============================================================
    # 消息处理主流程
    # ============================================================

    def _cancel_private_wake(self, chat_key: str) -> None:
        """Cancel the pending proactive wake-up for one private chat.

        Args:
            chat_key: Scoped platform and private-chat key.
        """
        key = str(chat_key or "").strip()
        wake_task = self._private_wake_tasks.pop(key, None)
        if wake_task is not None and not wake_task.done():
            wake_task.cancel()

    def _schedule_private_wake(
        self,
        event: AstrMessageEvent,
        chat_key: str,
        boundary: Any,
    ) -> None:
        """Schedule one adaptive wake-up review for a sleep boundary.

        Args:
            event: Original event used only to capture scalar session metadata.
            chat_key: Scoped platform and private-chat key.
            boundary: Sleep boundary containing the selected wake timestamp.
        """
        key = str(chat_key or "").strip()
        wake_at = getattr(boundary, "wake_at", None)
        expire_after = getattr(boundary, "expire_after", None)
        if not key or getattr(boundary, "kind", None) != "sleep" or wake_at is None:
            return

        wake_identity = {
            "session": str(event.unified_msg_origin),
            "platform_name": str(event.get_platform_name() or ""),
            "sender_id": str(event.get_sender_id() or ""),
            "sender_name": str(event.get_sender_name() or ""),
            "self_id": str(event.get_self_id() or ""),
        }
        self._cancel_private_wake(key)

        async def run_wake_review() -> None:
            current_task = asyncio.current_task()
            pending_messages: list[dict[str, Any]] = []
            try:
                review_times = [float(wake_at)]
                if expire_after is not None:
                    final_review_at = max(float(wake_at), float(expire_after) - 0.5)
                    if final_review_at > review_times[0]:
                        review_times.append(final_review_at)
                for review_at in review_times:
                    await asyncio.sleep(max(0.0, review_at - time.time()))
                    async with self.concurrent_lock:
                        active_boundary = self._private_conversation_state.get(key)
                        if active_boundary is not boundary:
                            return
                        pending_messages = (
                            self._private_conversation_state.take_pending_messages(key)
                        )
                    if not pending_messages:
                        continue
                    try:
                        await self._run_private_wake_review(
                            wake_identity,
                            key,
                            boundary,
                            pending_messages,
                        )
                    except Exception as exc:
                        logger.error(
                            f"[Private wake] Delayed wake-up review failed: {exc}",
                            exc_info=True,
                        )
                        async with self.concurrent_lock:
                            for pending_message in pending_messages:
                                self._private_conversation_state.record_pending_message(
                                    key, pending_message
                                )
                    pending_messages = []
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    f"[Private wake] Delayed wake-up scheduler failed: {exc}",
                    exc_info=True,
                )
                async with self.concurrent_lock:
                    for pending_message in pending_messages:
                        self._private_conversation_state.record_pending_message(
                            key, pending_message
                        )
            finally:
                if self._private_wake_tasks.get(key) is current_task:
                    self._private_wake_tasks.pop(key, None)

        self._private_wake_tasks[key] = asyncio.create_task(
            run_wake_review(),
            name=f"persona-private-wake:{key}",
        )

    async def _run_private_wake_review(
        self,
        wake_identity: dict[str, str],
        chat_key: str,
        boundary: Any,
        pending_messages: list[dict[str, Any]],
    ) -> None:
        """Queue a fresh event for messages received during private sleep.

        Args:
            wake_identity: Persistable session and sender metadata from the original event.
            chat_key: Scoped platform and private-chat key.
            boundary: Boundary that scheduled this review.
            pending_messages: Messages received while the boundary was active.
        """
        pending_text = "\n".join(
            str(message.get("content", "") or "").strip()
            for message in pending_messages
            if str(message.get("content", "") or "").strip()
        ).strip()
        if not pending_text:
            return

        from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
        from astrbot.core.platform.message_session import MessageSession

        try:
            session = MessageSession.from_str(wake_identity["session"])
            latest_message = pending_messages[-1]
            latest_content = str(latest_message.get("content", "") or "").strip()
            if not latest_content:
                return
            backlog_messages = pending_messages[:-1]
            async with self.concurrent_lock:
                active_boundary = self._private_conversation_state.get(chat_key)
                if active_boundary is not boundary:
                    for pending_message in pending_messages:
                        self._private_conversation_state.record_pending_message(
                            chat_key, pending_message
                        )
                    return
                for backlog_message in backlog_messages:
                    self._private_conversation_state.record_pending_message(
                        chat_key, backlog_message
                    )

            platform = self.context.get_platform_inst(session.platform_id)
            if platform is None:
                raise RuntimeError(
                    f"Private wake platform is unavailable: {session.platform_id}"
                )
            if not getattr(platform.meta(), "support_proactive_message", True):
                logger.info(
                    f"[Private wake] Platform does not support proactive messages: "
                    f"{session.platform_id}"
                )
                async with self.concurrent_lock:
                    for pending_message in pending_messages:
                        self._private_conversation_state.record_pending_message(
                            chat_key, pending_message
                        )
                return
            wake_message = AstrBotMessage()
            wake_message.type = session.message_type
            wake_message.self_id = wake_identity.get("self_id", "") or "astrbot"
            wake_message.session_id = session.session_id
            wake_message.message_id = uuid.uuid4().hex
            wake_message.sender = MessageMember(
                user_id=wake_identity.get("sender_id", "") or session.session_id,
                nickname=wake_identity.get("sender_name", "") or "用户",
            )
            wake_message.message = [Plain(latest_content)]
            wake_message.message_str = latest_content
            wake_message.raw_message = latest_content
            wake_message.timestamp = int(time.time())
            wake_event = platform.create_event(wake_message)
            wake_event.session = session
            wake_event.set_extra("_persona_private_wake", True)
            wake_event.set_extra("_persona_private_wake_backlog", backlog_messages)
            wake_event.set_extra("_persona_private_wake_current", dict(latest_message))
            platform.commit_event(wake_event)
            if self.debug_mode:
                logger.info(
                    f"[Private wake] Queued delayed review for {chat_key} "
                    f"with {len(pending_messages)} pending messages"
                )
        except Exception as exc:
            logger.error(
                f"[Private wake] Could not queue native delayed event: {exc}",
                exc_info=True,
            )
            async with self.concurrent_lock:
                for pending_message in pending_messages:
                    self._private_conversation_state.record_pending_message(
                        chat_key, pending_message
                    )

    async def _format_ai_context(
        self,
        history_messages,
        current_message,
        bot_id,
        window_msgs=None,
        poke_notice="",
    ) -> str:
        """统一格式化 AI 上下文（集中 include 配置，减少主流程重复）。"""
        return await ContextManager.format_context_for_ai(
            history_messages,
            current_message,
            bot_id,
            include_timestamp=self.include_timestamp,
            include_sender_info=self.include_sender_info,
            window_buffered_messages=window_msgs,
            poke_notice=poke_notice,
        )

    async def _resolve_lazy_image_context(
        self,
        event: AstrMessageEvent,
        formatted_context: str,
        image_urls: list[str],
    ) -> tuple[str, list[str]]:
        """Read only images related to an already accepted reply.

        Args:
            event: Anchor event for the current reply.
            formatted_context: Text-only context used to judge image relevance.
            image_urls: Candidate images from the current and nearby messages.

        Returns:
            The reply context and the image URLs or descriptions to send.
        """
        if self.image_read_mode != "lazy" or not image_urls:
            return formatted_context, image_urls

        candidates = list(
            dict.fromkeys(url for url in image_urls if isinstance(url, str) and url)
        )[: self._LAZY_IMAGE_CANDIDATE_LIMIT]
        if not candidates:
            return formatted_context, []

        selected = await ImageHandler.select_relevant_image_urls(
            self.context,
            candidates,
            formatted_context,
            provider_id=self.image_to_text_provider_id,
            timeout=self.image_to_text_timeout,
            session_id=str(getattr(event, "session_id", "") or ""),
        )
        if selected is None:
            return (
                formatted_context
                + "\n\n[Image relevance] Image understanding was unavailable. "
                "Ignore the images and do not infer their contents.",
                [],
            )
        if not selected:
            return (
                formatted_context
                + "\n\n[Image relevance] The available images are not related to this reply. "
                "Ignore them and do not reply about image content.",
                [],
            )

        if self.image_to_text_provider_id:
            descriptions = await ImageHandler.describe_image_urls(
                self.context,
                selected,
                self.image_to_text_provider_id,
                self.image_to_text_prompt,
                timeout=self.image_to_text_timeout,
                image_description_cache=self.image_description_cache,
                session_id=str(getattr(event, "session_id", "") or ""),
            )
            if not descriptions:
                return (
                    formatted_context
                    + "\n\n[Image relevance] Image understanding failed. Ignore the images.",
                    [],
                )
            description_lines = [
                f"- Image {index}: {descriptions[url]}"
                for index, url in enumerate(selected, start=1)
                if url in descriptions
            ]
            return (
                formatted_context
                + "\n\n[Relevant image descriptions]\n"
                + "\n".join(description_lines),
                [],
            )

        return (
            formatted_context
            + f"\n\n[Image relevance] {len(selected)} image(s) are related to the current conversation. "
            "Use them together with the text; ignore other images.",
            selected,
        )

    async def _process_message(self, event: AstrMessageEvent):
        """
        消息处理主流程''  # 占位

        流程：
        初始检查 → 消息触发器（@/关键词）→ 戳一戳/@提及 → 参与候选整理 →
        内容处理（图片/媒体/上下文）→ Smart并发 → AI决策（参与判断）→ 生成并发送回复
        """

        # 步骤1: 初始检查（最基本的过滤）
        (
            should_continue,
            platform_name,
            is_private,
            chat_id,
        ) = await self._perform_initial_checks(event)
        if not should_continue:
            return

        takeover_reply = (
            self.takeover_private_reply if is_private else self.takeover_group_reply
        )

        raw_chat_id = chat_id
        chat_id = ProbabilityManager.get_chat_key(
            platform_name, is_private, raw_chat_id
        )
        private_boundary = (
            self._private_conversation_state.get(chat_id) if is_private else None
        )
        if private_boundary and self.debug_mode:
            logger.info(
                f"[Private boundary] state={private_boundary.kind}, "
                f"reopen_in={private_boundary.seconds_until_reopen()}s, "
                f"expires_in={private_boundary.seconds_until_expiry()}"
            )

        # Smart batching is independent from private DecisionAI gating. Direct
        # private turns can still merge short bursts before formal generation.
        use_smart_batch = (
            (
                self.private_concurrent_mode == "smart"
                and not (
                    private_boundary is not None
                    and private_boundary.kind == "sleep"
                    and getattr(private_boundary, "sleep_mode", "literal")
                    != "ambiguous"
                    and private_boundary.seconds_until_reopen() > 0
                )
            )
            if is_private
            else self.concurrent_mode == "smart"
        )

        # 步骤2: 检查消息触发器（整理参与候选 signal）
        _chat_key_for_seq = chat_id
        current_group_seq = self._group_message_seq.get(_chat_key_for_seq, 0) + 1
        self._group_message_seq[_chat_key_for_seq] = current_group_seq
        (
            is_at_message,
            has_trigger_keyword,
            matched_trigger_keyword,
        ) = await self._check_message_triggers(event)

        # 步骤2.5: 检测@全体成员与戳一戳信息（参与判断前提取）
        is_at_all_message = False
        try:
            is_at_all_message = bool(
                event.get_extra("is_at_all_message", False)
                if hasattr(event, "get_extra")
                else False
            )
        except Exception:
            is_at_all_message = False

        poke_result = await self._check_poke_message(event)
        poke_info_for_probability = (
            poke_result
            if poke_result.get("is_poke") and not poke_result.get("should_ignore")
            else None
        )

        # 提前构建戳一戳文本（概率检查之前）
        persistent_poke_event_text = ""
        poke_notice_text = ""
        if poke_info_for_probability:
            _poke_info_inner = poke_info_for_probability.get("poke_info")
            if _poke_info_inner:
                try:
                    persistent_poke_event_text = (
                        MessageProcessor.build_persistent_poke_event_text(
                            _poke_info_inner
                        )
                    )
                except Exception:
                    pass
                try:
                    _is_pb = _poke_info_inner.get("is_poke_bot", False)
                    _sid = str(_poke_info_inner.get("sender_id", "") or "")
                    _sname = (
                        str(_poke_info_inner.get("sender_name", "") or "").strip()
                        or "未知用户"
                    )
                    _tid = str(_poke_info_inner.get("target_id", "") or "")
                    _tname = (
                        str(_poke_info_inner.get("target_name", "") or "").strip()
                        or "未知用户"
                    )
                    if _is_pb:
                        poke_notice_text = (
                            f"[戳一戳提示]有人在戳你，戳你的人是{_sname}(ID:{_sid})"
                        )
                    else:
                        poke_notice_text = f"[戳一戳提示]这是一个戳一戳消息，但不是戳你的，是{_sname}(ID:{_sid})在戳{_tname}(ID:{_tid})"
                except Exception:
                    pass

        # 步骤2.8: 提前检测@提及信息
        mention_info = await self._check_mention_others(event)

        _poke_payload = (
            poke_info_for_probability.get("poke_info")
            if isinstance(poke_info_for_probability, dict)
            else None
        )
        is_poke_to_bot = bool(
            isinstance(_poke_payload, dict) and _poke_payload.get("is_poke_bot")
        )
        bot_id = str(event.get_self_id() or "")
        reply_target_id = MessageProcessor.get_reply_target_id(event)
        is_reply_to_bot = bool(bot_id and reply_target_id and reply_target_id == bot_id)
        is_reply_to_other = bool(reply_target_id and reply_target_id != bot_id)
        is_explicitly_addressed = bool(
            is_at_message or is_poke_to_bot or is_reply_to_bot
        )
        # Keyword matches enter the ambient decision flow but are not proof that
        # the sender is talking to the bot.
        is_direct_address = bool(is_explicitly_addressed or has_trigger_keyword)
        unaddressed_group_message = not is_private and not is_direct_address
        group_reply_blocked = (
            unaddressed_group_message and self.group_reply_scope == "addressed"
        )

        message_obj = getattr(event, "message_obj", None)
        message_chain = getattr(message_obj, "message", None)
        if message_chain is None:
            current_plain_text = str(event.get_message_str() or "")
            has_media_component = False
        else:
            current_plain_text = "".join(
                str(getattr(component, "text", "") or "")
                for component in message_chain
                if isinstance(component, Plain)
            )
            media_component_types = {
                "animation",
                "audio",
                "face",
                "file",
                "flashimage",
                "image",
                "marketface",
                "record",
                "video",
            }
            has_media_component = False
            for component in message_chain:
                if isinstance(component, dict):
                    component_type = component.get("type", "")
                else:
                    component_type = (
                        getattr(component, "type", "") or type(component).__name__
                    )
                if (
                    str(component_type).replace("_", "").lower()
                    in media_component_types
                ):
                    has_media_component = True
                    break
        compact_current_text = re.sub(r"\s+", "", current_plain_text)
        current_has_question_intent = bool(
            re.search(
                r"[?？]|(?:吗|么|呢|为什么|怎么|如何|什么|谁|哪里|哪儿|多少|几|能不能|有没有)",
                current_plain_text,
            )
        )
        message_outline = str(event.get_message_str() or "").strip().lower()
        is_low_information_short_message = bool(
            len(compact_current_text) <= 8
            and not current_has_question_intent
            and (
                re.fullmatch(
                    r"(?:嗯+|哦+|噢+|啊+|呃+|额+|哈哈+|呵呵+|确实|对|对的|是的|"
                    r"好|好的|行|可以|不行|没事|算了|随便|继续|然后|来了|走了|"
                    r"懂了|知道了|收到|晚安|再见)[。！!~～]*",
                    compact_current_text,
                )
                or compact_current_text.endswith(("吧", "啦", "哦", "呀"))
                and len(compact_current_text) <= 6
                or re.search(
                    r"^(?:我|在|先|要|准备).{0,5}(?:睡|睡觉|睡了|吃饭|走了|"
                    r"听着|上班|下班|回家|出门)",
                    compact_current_text,
                )
            )
        )
        is_media_only_message = bool(
            not compact_current_text
            and (
                has_media_component
                or re.fullmatch(
                    r"\[(?:图片|表情|视频|语音|文件|动画|image|face|video|record|file)(?::[^\]]*)?\]",
                    message_outline,
                )
            )
        )

        if group_reply_blocked:
            # Unaddressed group messages remain cacheable but skip Smart and vision work.
            use_smart_batch = False

        should_treat_as_at = is_direct_address

        # Detect stickers independently from ordinary images.
        is_emoji_message = False
        emoji_signature = ""
        if self.enable_emoji_filter or (
            is_private
            and (
                self.private_collapse_duplicate_emoji
                or self.private_emoji_mode != "ignore"
            )
        ):
            platform_name_lower = platform_name.lower() if platform_name else ""
            is_qq_platform = any(
                kw in platform_name_lower
                for kw in ("qq", "napcat", "lagrange", "aiocqhttp", "onebot")
            )
            if is_qq_platform:
                try:
                    is_emoji_message = EmojiDetector.is_emoji_message(event)
                except Exception as e:
                    logger.warning(f"Emoji detection failed: {e}")

        if is_emoji_message:
            signature_parts = []
            message_chain = getattr(getattr(event, "message_obj", None), "message", [])
            for component in message_chain or []:
                try:
                    component_data = (
                        component.toDict() if hasattr(component, "toDict") else None
                    )
                    if not isinstance(component_data, dict):
                        component_data = {
                            key: getattr(component, key, None)
                            for key in (
                                "type",
                                "id",
                                "face_id",
                                "faceId",
                                "file",
                                "url",
                                "summary",
                                "sub_type",
                                "subType",
                            )
                            if getattr(component, key, None) is not None
                        }
                    stable_data = {
                        key: component_data[key]
                        for key in (
                            "type",
                            "id",
                            "face_id",
                            "faceId",
                            "file",
                            "url",
                            "summary",
                            "sub_type",
                            "subType",
                        )
                        if key in component_data
                    }
                    if stable_data:
                        signature_parts.append(stable_data)
                except Exception:
                    continue

            fallback_text = (event.get_message_str() or "").strip()
            if signature_parts or fallback_text not in ("", "[图片]", "[表情]"):
                signature_payload = {
                    "parts": signature_parts,
                    "text": fallback_text,
                }
                emoji_signature = hashlib.sha1(
                    json.dumps(
                        signature_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()

        private_late_night_hour = datetime.now().astimezone().hour
        private_late_night_active = False
        if is_private:
            active_until = self._private_late_night_active_until.get(chat_id, 0.0)
            if active_until > time.time() and (
                self._PRIVATE_LATE_NIGHT_START_HOUR
                <= private_late_night_hour
                < self._PRIVATE_LATE_NIGHT_END_HOUR
            ):
                private_late_night_active = True
                logger.info(
                    "[Private late-night] Existing awake session; "
                    "bypassing repeated DecisionAI"
                )
            elif active_until:
                self._private_late_night_active_until.pop(chat_id, None)
        private_late_night_review = bool(
            is_private
            and not private_late_night_active
            and (
                self._PRIVATE_LATE_NIGHT_START_HOUR
                <= private_late_night_hour
                < self._PRIVATE_LATE_NIGHT_END_HOUR
            )
            and bool(compact_current_text)
            and not has_media_component
            and not is_emoji_message
            and not is_explicitly_addressed
        )
        # The global datetime context already supplies the current time. Keep the
        # late-night route as a code path, without adding another yes/no instruction.
        private_late_night_hint = ""
        if private_late_night_review:
            logger.info(
                f"[Private late-night] Sending ordinary private text through DecisionAI "
                f"at hour={private_late_night_hour:02d}"
            )

        ambient_attention_blocked = bool(
            self.group_reply_scope == "ambient"
            and unaddressed_group_message
            and (
                (
                    (
                        is_reply_to_other
                        or (
                            isinstance(mention_info, dict)
                            and mention_info.get("has_at_others")
                            and not mention_info.get("has_at_ai")
                        )
                        or is_at_all_message
                    )
                    and not compact_current_text
                )
                or is_media_only_message
                or is_low_information_short_message
                or (is_emoji_message and not compact_current_text)
            )
        )
        if ambient_attention_blocked:
            # Let substantive ambient messages reach the model, but discard obvious noise first.
            use_smart_batch = False

        if is_private and emoji_signature and self.private_collapse_duplicate_emoji:
            try:
                duplicate_window = max(
                    0.0, float(self.private_duplicate_emoji_window_ms) / 1000
                )
            except (TypeError, ValueError):
                duplicate_window = 1.5
            now = time.monotonic()
            recent_signatures = self._private_recent_emoji.setdefault(chat_id, {})
            for signature, seen_at in list(recent_signatures.items()):
                if now - seen_at > duplicate_window:
                    recent_signatures.pop(signature, None)
            previous_seen_at = recent_signatures.get(emoji_signature)
            recent_signatures[emoji_signature] = now
            if (
                previous_seen_at is not None
                and now - previous_seen_at <= duplicate_window
            ):
                if self.debug_mode:
                    logger.info("Collapsed a repeated private sticker message")
                if self.takeover_private_reply:
                    event.stop_event()
                return

        force_private_media_reply = False
        private_media_kind = ""
        if is_private:
            try:
                has_image = PlatformLTMHelper.has_image_in_message(event)
                is_pure_image = PlatformLTMHelper.is_pure_image_message(event)
            except Exception:
                has_image = False
                is_pure_image = False
            if is_pure_image:
                if is_emoji_message:
                    private_media_kind = "sticker"
                    private_media_policy = self.private_emoji_mode
                elif has_image:
                    private_media_kind = "image"
                    private_media_policy = self.private_image_mode
                else:
                    private_media_policy = "decide"
                if private_media_policy == "ignore":
                    if self.debug_mode:
                        logger.info(
                            f"Ignored a private {private_media_kind or 'media'}-only message"
                        )
                    if self.takeover_private_reply:
                        event.stop_event()
                    return
                force_private_media_reply = private_media_policy == "always"

        # Step 3 has no random gate; eligible messages rely on DecisionAI and local policy.

        # 步骤3.5: 戳一戳反戳逻辑（正式参与判断之前）
        poke_info = (
            poke_info_for_probability.get("poke_info")
            if poke_info_for_probability
            else None
        )
        if poke_info:
            reversed_and_discarded = await self._maybe_reverse_poke_on_poke(
                event, poke_info, is_private, raw_chat_id
            )
            if reversed_and_discarded:
                return

        # @消息/关键词触发提前检查是否已被其他插件处理
        if is_direct_address:
            if ReplyHandler.check_if_already_replied(event):
                trigger_label = "@消息" if is_at_message else "直接触发消息"
                logger.info(f"{trigger_label}已被其他插件处理,跳过后续流程")
                return

        # Register before image, memory, and other expensive preparation so
        # burst ordering follows message arrival rather than processing speed.
        processing_id = self._get_processing_id(event)
        source_event_id = self._build_source_event_id(event)
        arrival_seq, arrival_monotonic = self._ensure_arrival_metadata(event)
        if use_smart_batch:
            await SmartConcurrentManager.register_arrival(
                chat_id=chat_id,
                processing_id=processing_id,
                source_event_id=source_event_id,
                arrival_seq=arrival_seq,
                arrival_monotonic=arrival_monotonic,
            )

        # 步骤4-6: 处理消息内容（图片处理等耗时操作）
        result = await self._process_message_content(
            event,
            raw_chat_id,
            current_group_seq,
            should_treat_as_at,
            mention_info,
            has_trigger_keyword,
            poke_info,
            raw_is_at_message=is_at_message,
            is_emoji_message=is_emoji_message,
            emoji_signature=emoji_signature,
            is_at_all_message=is_at_all_message,
            persistent_poke_event_text=persistent_poke_event_text,
            force_defer_image_processing=False,
        )
        if not result[0]:
            # A plugin-owned media filter is still a silent decision when takeover is enabled.
            if takeover_reply:
                try:
                    event.stop_event()
                except Exception:
                    pass
            if use_smart_batch:
                await SmartConcurrentManager.remove_self(chat_id, processing_id)
            return

        (
            _,
            original_message_text,
            message_text,
            formatted_context,
            image_urls,
            history_messages,
            cached_message_data,
            emoji_marker_applied,
        ) = result

        def _build_current_message_for_ai(current_text: str) -> str:
            _is_empty_at = MessageCleaner.is_empty_at_message(
                original_message_text,
                is_at_message,
                mention_info=mention_info,
                mode="only_ai",
            )
            _current_message = MessageProcessor.add_metadata_to_message(
                event,
                current_text,
                self.include_timestamp,
                self.include_sender_info,
                mention_info,
                "keyword"
                if has_trigger_keyword
                else "at"
                if should_treat_as_at
                else "ai_decision",
                poke_info,
                _is_empty_at,
                "",
                "",
                is_at_all_message=is_at_all_message,
                persistent_poke_event_text=persistent_poke_event_text,
            )
            return _current_message

        current_message_for_ai = _build_current_message_for_ai(message_text)

        merged_image_urls = list(image_urls or [])
        try:
            if (
                self.enable_image_processing
                and (
                    self.image_read_mode == "lazy" or not self.image_to_text_provider_id
                )
                and chat_id in self.pending_messages_cache
            ):
                for _cached in self.pending_messages_cache[chat_id]:
                    if isinstance(_cached, dict):
                        _urls = _cached.get("image_urls") or []
                        if _urls and _cached.get("decision_state") != "observed":
                            merged_image_urls.extend(_urls)
                if merged_image_urls:
                    _seen_urls = set()
                    _dedup_urls = []
                    for _u in merged_image_urls:
                        if _u and _u not in _seen_urls:
                            _seen_urls.add(_u)
                            _dedup_urls.append(_u)
                    merged_image_urls = _dedup_urls
        except Exception as e:
            logger.warning(f"[图片缓存] 合并图片URL失败: {e}")

        current_message_cache = cached_message_data
        early_message_id = processing_id
        _is_forced = is_at_message or has_trigger_keyword

        if use_smart_batch and cached_message_data:
            try:
                _smart_content = MessageCleaner.clean_message(message_text or "")
            except Exception:
                _smart_content = message_text or ""
            _smart_cached = dict(cached_message_data)
            attached = await SmartConcurrentManager.attach_payload(
                chat_id=chat_id,
                processing_id=processing_id,
                content=_smart_content,
                sender_name=self._safe_sender_display(event),
                sender_id=str(event.get_sender_id()),
                cached_data=_smart_cached,
                is_forced=_is_forced,
            )
            if not attached:
                logger.warning(
                    f"[Smart] Dropping late payload for expired message {processing_id[:20]}..."
                )
                if takeover_reply:
                    try:
                        event.stop_event()
                    except Exception:
                        pass
                return
            if self.debug_mode:
                logger.info(
                    f"🔀 [Smart并发] 消息 {processing_id[:20]}... 已挂载批处理载荷"
                )

        _welcome_skip_all = (
            (
                event.get_extra("is_welcome_message")
                and event.get_extra("welcome_message_mode") == "skip_all"
            )
            if hasattr(event, "get_extra")
            else False
        )
        _at_all_skip_all = is_at_all_message and self.at_all_message_mode == "skip_all"
        has_at_others = bool(
            isinstance(mention_info, dict) and mention_info.get("has_at_others")
        )
        private_sleep_locked = bool(
            is_private
            and private_boundary
            and private_boundary.kind == "sleep"
            and getattr(private_boundary, "sleep_mode", "literal") != "ambiguous"
            and private_boundary.seconds_until_reopen() > 0
        )
        speculative_private_decision = bool(
            is_private
            and not private_late_night_active
            and use_smart_batch
            and not merged_image_urls
            and (not force_private_media_reply or private_boundary is not None)
            and (
                self.private_reply_mode != "direct"
                or private_boundary is not None
                or private_late_night_review
                or force_private_media_reply
            )
            and (
                private_boundary is not None
                or not (_welcome_skip_all or _at_all_skip_all)
            )
            and not private_sleep_locked
        )
        speculative_private_reply = bool(
            is_private
            and self.private_reply_mode == "direct"
            and use_smart_batch
            and bool(message_text.strip())
            and not merged_image_urls
            and not force_private_media_reply
            and not private_late_night_review
            and private_boundary is None
            and not private_sleep_locked
            and not is_at_message
            and not has_trigger_keyword
            and not is_emoji_message
        )
        speculative_reply_task: asyncio.Task | None = None

        async def _discard_speculative_reply() -> None:
            nonlocal speculative_reply_task
            if speculative_reply_task is None:
                return
            task = speculative_reply_task
            speculative_reply_task = None
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(f"[Private Speculative] Reply discarded: {exc}")

        speculative_decision_task: asyncio.Task | None = None

        async def _discard_speculative_decision() -> None:
            nonlocal speculative_decision_task
            if speculative_decision_task is None:
                return
            task = speculative_decision_task
            speculative_decision_task = None
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(f"[Private Smart] Speculative decision discarded: {exc}")

        # Start the private decision while the Smart window collects possible followers.
        # A follower invalidates this result and forces one authoritative combined decision.
        if speculative_private_decision:
            try:
                has_earlier_private_message = (
                    await SmartConcurrentManager.has_earlier_pending(
                        chat_id, processing_id
                    )
                )
            except Exception:
                has_earlier_private_message = False
            if not has_earlier_private_message:
                speculative_context = formatted_context + private_late_night_hint
                if private_boundary:
                    speculative_context += (
                        "\n\n"
                        + self._private_conversation_state.build_decision_context(
                            chat_id
                        )
                    )
                speculative_decision_task = asyncio.create_task(
                    self._check_ai_decision(
                        event,
                        speculative_context,
                        is_at_message,
                        has_trigger_keyword,
                        [] if self.image_read_mode == "lazy" else merged_image_urls,
                        matched_trigger_keyword=matched_trigger_keyword,
                        original_message_text=original_message_text,
                        force_ai_decision=is_private
                        and (
                            bool(private_media_kind)
                            or bool(private_boundary)
                            or private_late_night_review
                        ),
                        is_directly_addressed=is_explicitly_addressed,
                        is_reply_to_other=is_reply_to_other,
                        has_at_others=has_at_others,
                    )
                )
                logger.info(
                    "[Private Smart] Started speculative decision before the batch window"
                )

        # Start a buffered formal draft for a direct private anchor. Tools are
        # described but never executed until a committed full Agent run.
        if speculative_private_reply:
            try:
                has_earlier_private_message = (
                    await SmartConcurrentManager.has_earlier_pending(
                        chat_id, processing_id
                    )
                )
            except Exception:
                has_earlier_private_message = False
            if not has_earlier_private_message:
                speculative_reply_task = asyncio.create_task(
                    self._generate_speculative_private_reply(
                        event,
                        formatted_context,
                        platform_name,
                        raw_chat_id,
                        history_messages,
                    )
                )
                logger.info(
                    "[Private Speculative] Started buffered formal reply before the batch window"
                )

        # Smart 模式下先按 arrival_seq 等待更早消息，确保只有 anchor 进入 AI 决策
        smart_claim = None
        if is_private:
            try:
                wait_interval_seconds = max(float(self.concurrent_wait_interval), 0.1)
                private_window_seconds = max(
                    float(self.private_batch_wait_ms) / 1000, 0.0
                )
                # Let followers observe the anchor claim across the configured window.
                smart_wait_loops = max(
                    3,
                    int(private_window_seconds / wait_interval_seconds) + 2,
                )
            except (TypeError, ValueError):
                smart_wait_loops = 6
        else:
            smart_wait_loops = max(1, self.concurrent_wait_max_loops)
        smart_batch_had_followers = False
        for smart_wait_idx in range(smart_wait_loops):
            if not use_smart_batch:
                break
            if await SmartConcurrentManager.is_consumed(processing_id):
                logger.info(
                    f"🔀 [Smart并发] 消息 {processing_id[:20]}... 已在决策前被更早批次吸收，跳过独立处理"
                )
                await _discard_speculative_decision()
                await _discard_speculative_reply()
                if takeover_reply:
                    try:
                        event.stop_event()
                    except Exception:
                        pass
                await SmartConcurrentManager.remove_self(chat_id, processing_id)
                self._message_cache_snapshots.pop(processing_id, None)
                self._smart_batch_snapshots.pop(processing_id, None)
                return

            if await SmartConcurrentManager.has_earlier_pending(chat_id, processing_id):
                if smart_wait_idx == 0 and self.debug_mode:
                    logger.info(
                        "🔀 [Smart并发] 决策前检测到更早到达的消息尚未完成，等待其先成为 anchor"
                    )
                await asyncio.sleep(self.concurrent_wait_interval)
                continue

            claim_delay = (
                max(0.0, float(self.private_batch_wait_ms) / 1000)
                if is_private
                else self.smart_concurrent_claim_delay
            )
            if (
                smart_wait_idx == 0
                and claim_delay > 0
                and (is_private or not _is_forced)
            ):
                await asyncio.sleep(claim_delay)

            smart_claim = await SmartConcurrentManager.claim_batch(
                chat_id,
                processing_id,
                max_batch_size=(self.private_batch_max_size if is_private else None),
            )
            if smart_claim.get("is_consumed"):
                logger.info(
                    f"🔀 [Smart并发] 消息 {processing_id[:20]}... 已在 claim 阶段被更早 anchor 吸收，跳过独立处理"
                )
                await _discard_speculative_decision()
                await _discard_speculative_reply()
                if takeover_reply:
                    try:
                        event.stop_event()
                    except Exception:
                        pass
                await SmartConcurrentManager.remove_self(chat_id, processing_id)
                self._message_cache_snapshots.pop(processing_id, None)
                self._smart_batch_snapshots.pop(processing_id, None)
                return
            if smart_claim.get("is_anchor"):
                merged_entries = smart_claim.get("merged_entries", []) or []
                if merged_entries:
                    smart_batch_had_followers = True
                    logger.info(
                        f"🔀 [Smart并发] 以当前消息为 anchor，吸收了 {len(merged_entries)} 条后续消息进入同一批次"
                    )
                    smart_window_messages = []
                    for _sm in merged_entries:
                        _sm_cache_entry = dict(_sm.get("cached_data") or {})
                        if not _sm_cache_entry:
                            continue
                        _sm_cache_entry["window_buffered"] = True
                        _sm_cache_entry["smart_merged"] = True
                        _sm_cache_entry["smart_batch_dynamic_hint"] = True
                        smart_window_messages.append(_sm_cache_entry)
                    if is_private and self.private_collapse_duplicate_emoji:
                        seen_emoji_signatures = set()
                        anchor_signature = (current_message_cache or {}).get(
                            "emoji_signature", ""
                        )
                        if anchor_signature:
                            seen_emoji_signatures.add(anchor_signature)
                        deduplicated_messages = []
                        for smart_message in smart_window_messages:
                            signature = smart_message.get("emoji_signature", "")
                            if signature and signature in seen_emoji_signatures:
                                continue
                            if signature:
                                seen_emoji_signatures.add(signature)
                            deduplicated_messages.append(smart_message)
                        if len(deduplicated_messages) != len(smart_window_messages):
                            logger.info(
                                "Collapsed repeated stickers inside one private batch"
                            )
                        smart_window_messages = deduplicated_messages
                    if smart_window_messages:
                        self._smart_batch_snapshots[processing_id] = [
                            copy.deepcopy(_msg) for _msg in smart_window_messages
                        ]
                break
        else:
            # The anchor can claim the follower immediately after the last poll.
            if await SmartConcurrentManager.is_consumed(processing_id):
                logger.info(
                    f"[Smart] Private message {processing_id[:20]}... was absorbed after the final wait poll"
                )
                await _discard_speculative_decision()
                await _discard_speculative_reply()
                if takeover_reply:
                    try:
                        event.stop_event()
                    except Exception:
                        pass
                await SmartConcurrentManager.remove_self(chat_id, processing_id)
                self._message_cache_snapshots.pop(processing_id, None)
                self._smart_batch_snapshots.pop(processing_id, None)
                return
            logger.warning(
                f"⚠️ [Smart并发] 消息 {processing_id[:20]}... 在决策前等待更早消息超时，按当前单条消息继续"
            )

        try:
            if current_message_cache:
                self._message_cache_snapshots[early_message_id] = copy.deepcopy(
                    current_message_cache
                )
        except Exception as e:
            logger.warning(f"[并发保护] 保存缓存副本失败: {e}")

        smart_batch_messages = []
        if use_smart_batch:
            smart_batch_messages = self._smart_batch_snapshots.get(early_message_id, [])
            if self.image_read_mode == "lazy":
                for batch_message in smart_batch_messages:
                    if not isinstance(batch_message, dict):
                        continue
                    for image_url in batch_message.get("image_urls") or []:
                        if isinstance(image_url, str) and image_url:
                            merged_image_urls.append(image_url)
                merged_image_urls = list(dict.fromkeys(merged_image_urls))

        # 步骤7: AI决策判断（第二道核心过滤）
        has_at_others = bool(
            isinstance(mention_info, dict) and mention_info.get("has_at_others")
        )
        decision_result = ParticipationDecision.silent(source="policy")
        _welcome_skip_all = (
            (
                event.get_extra("is_welcome_message")
                and event.get_extra("welcome_message_mode") == "skip_all"
            )
            if hasattr(event, "get_extra")
            else False
        )
        _at_all_skip_all = is_at_all_message and self.at_all_message_mode == "skip_all"

        if (_welcome_skip_all or _at_all_skip_all) and not private_boundary:
            decision_result = ParticipationDecision(
                reply=True,
                target="bot" if is_explicitly_addressed else "open",
                participation="direct" if is_explicitly_addressed else "open",
                information="substantive",
                interest="weak",
                reason_code="direct_request"
                if is_explicitly_addressed
                else "shared_interest",
                confidence="low",
                source="forced",
            )
            if self.debug_mode:
                logger.info("【步骤7】skip_all 模式消息，跳过AI决策，强制处理")
        elif force_private_media_reply and not private_boundary:
            decision_result = ParticipationDecision(
                reply=True,
                target="bot",
                participation="direct",
                information="substantive",
                interest="weak",
                reason_code="direct_request",
                confidence="high",
                source="forced",
            )
            if self.debug_mode:
                logger.info(
                    f"Forced handling for private {private_media_kind or 'media'}-only message"
                )
        elif ambient_attention_blocked:
            decision_result = ParticipationDecision.silent(
                source="policy", reason_code="none"
            )
            logger.info("[Ambient attention] Skipping obvious noise before decision AI")
        elif group_reply_blocked:
            decision_result = ParticipationDecision.silent(
                source="policy", reason_code="none"
            )
            logger.info(
                "[Group boundary] Skipping decision AI for an unaddressed group message"
            )
        elif (
            is_private
            and private_boundary
            and private_boundary.kind == "sleep"
            and getattr(private_boundary, "sleep_mode", "literal") != "ambiguous"
            and private_boundary.seconds_until_reopen() > 0
        ):
            decision_result = ParticipationDecision.silent(
                source="policy", reason_code="none"
            )
            logger.info(
                "[Private sleep] Before earliest reopening; skipping decision AI and retaining message"
            )
        elif is_private and private_late_night_active:
            decision_result = ParticipationDecision(
                reply=True,
                target="bot",
                participation="direct",
                information="substantive",
                interest="weak",
                reason_code="direct_request",
                confidence="medium",
                source="policy",
            )
            logger.info(
                "[Private late-night] Awake session accepted message without repeated DecisionAI"
            )
        else:
            decision_context = formatted_context
            if use_smart_batch and smart_batch_messages:
                try:
                    decision_context = await self._format_ai_context(
                        history_messages,
                        current_message_for_ai,
                        event.get_self_id(),
                        window_msgs=smart_batch_messages,
                        poke_notice=poke_notice_text,
                    )
                except Exception as smart_ctx_err:
                    logger.warning(
                        f"[Smart并发] 决策阶段重建批次上下文失败，回退原上下文: {smart_ctx_err}"
                    )

            image_question_text = original_message_text or ""
            if smart_batch_messages:
                for batch_message in smart_batch_messages:
                    if not isinstance(batch_message, dict):
                        continue
                    batch_content = batch_message.get("content", "")
                    if isinstance(batch_content, str) and batch_content.strip():
                        image_question_text += "\n" + batch_content

            decision_context_for_ai = decision_context + private_late_night_hint
            image_question_requested = bool(
                self.image_read_mode == "lazy"
                and merged_image_urls
                and re.search(
                    r"(?:这张?(?:图|图片|照片)|这幅图|图里|图中|画面|图像|图片|照片|截图).{0,12}"
                    r"(?:怎么样|如何|什么|啥|好看|内容|意思|说明|描述|评价|识别|看得出|看出来)"
                    r"|(?:看(?:看)?(?:一下)?|打开|分析|识别)(?:这张?)?(?:图|图片|照片|截图)"
                    r"|(?:怎么样|如何|好看吗|什么内容|什么意思|能看出什么).{0,8}"
                    r"(?:这张?(?:图|图片|照片)|图片|照片|截图)",
                    image_question_text,
                    flags=re.IGNORECASE,
                )
            )
            if self.image_read_mode == "lazy" and merged_image_urls:
                decision_context_for_ai += (
                    "\n\n[Image handling] Image content has not been read yet. "
                    "Do not reply merely because an [图片] placeholder exists. "
                    "Return yes only when the text explicitly asks about the image "
                    "or the conversation itself is worth answering."
                )
            if private_boundary:
                decision_context_for_ai += (
                    "\n\n"
                    + self._private_conversation_state.build_decision_context(chat_id)
                )
            if image_question_requested and not private_boundary:
                decision_context_for_ai += (
                    " The sender explicitly asks about an image; treat this as a "
                    "direct request and return yes unless the message is unsafe or invalid."
                )
            if (
                is_private
                and speculative_decision_task is not None
                and not smart_batch_had_followers
            ):
                try:
                    decision_result = await speculative_decision_task
                    speculative_decision_task = None
                    logger.info(
                        "[Private Smart] No follower arrived; reused speculative decision"
                    )
                except Exception as speculative_err:
                    speculative_decision_task = None
                    logger.warning(
                        f"[Private Smart] Speculative decision failed; retrying authoritatively: {speculative_err}"
                    )
                    decision_result = await self._check_ai_decision(
                        event,
                        decision_context_for_ai,
                        is_at_message,
                        has_trigger_keyword,
                        [] if self.image_read_mode == "lazy" else merged_image_urls,
                        matched_trigger_keyword=matched_trigger_keyword,
                        original_message_text=original_message_text,
                        force_ai_decision=is_private
                        and (
                            bool(private_media_kind)
                            or bool(private_boundary)
                            or private_late_night_review
                        ),
                        is_directly_addressed=is_explicitly_addressed,
                        is_reply_to_other=is_reply_to_other,
                        has_at_others=has_at_others,
                    )
            else:
                if speculative_decision_task is not None:
                    if smart_batch_had_followers:
                        logger.info(
                            "[Private Smart] Followers arrived; invalidating speculative decision"
                        )
                    await _discard_speculative_decision()
                    await _discard_speculative_reply()
                    try:
                        delattr(event, "_decision_ai_error")
                    except AttributeError:
                        pass
                decision_result = await self._check_ai_decision(
                    event,
                    decision_context_for_ai,
                    is_at_message,
                    has_trigger_keyword,
                    [] if self.image_read_mode == "lazy" else merged_image_urls,
                    matched_trigger_keyword=matched_trigger_keyword,
                    original_message_text=original_message_text,
                    force_ai_decision=is_private
                    and (
                        bool(private_media_kind)
                        or bool(private_boundary)
                        or private_late_night_review
                    ),
                    is_directly_addressed=is_explicitly_addressed,
                    is_reply_to_other=is_reply_to_other,
                    has_at_others=has_at_others,
                )
            if (
                is_private
                and private_boundary
                and private_boundary.kind == "sleep"
                and decision_result.boundary_interpretation != "none"
            ):
                previous_boundary_kind = private_boundary.kind
                private_boundary = (
                    self._private_conversation_state.apply_sleep_interpretation(
                        chat_id,
                        decision_result.boundary_interpretation,
                    )
                    or private_boundary
                )
                if (
                    previous_boundary_kind == "sleep"
                    and private_boundary.kind != "sleep"
                ):
                    self._cancel_private_wake(chat_id)
                if self.debug_mode:
                    logger.info(
                        "[Private boundary] applied DecisionAI interpretation="
                        f"{decision_result.boundary_interpretation}, "
                        f"state={private_boundary.kind}/"
                        f"{getattr(private_boundary, 'sleep_mode', 'none')}"
                    )
            if (
                private_boundary
                and private_boundary.kind == "sleep"
                and getattr(private_boundary, "sleep_mode", "literal") != "ambiguous"
                and private_boundary.seconds_until_reopen() > 0
            ):
                decision_result = decision_result.with_reply(
                    False, source="policy", reason_code="none"
                )
                if self.debug_mode:
                    logger.info(
                        "[Private sleep] Earliest reopening has not arrived; keeping the message unanswered"
                    )
            if (
                image_question_requested
                and not decision_result.reply
                and not decision_result.error
                and not private_boundary
            ):
                decision_result = decision_result.with_reply(
                    True,
                    target="bot",
                    participation="direct",
                    information="substantive",
                    interest="weak",
                    reason_code="direct_request",
                    source="policy",
                )
                logger.info(
                    "Direct image question detected; continuing to image relevance check"
                )

        # A continuation claim is a factual handoff, not a substitute for a
        # current direct request. Verify its sender relation from structured history.
        if not is_private and decision_result.continuation == "yes":
            current_sender_id = str(event.get_sender_id() or "").strip()
            bot_id = str(event.get_self_id() or "").strip()
            _reply_context_key = ProbabilityManager.get_chat_key(
                platform_name, is_private, raw_chat_id
            )
            _last_reply_context = self._last_bot_reply_context.get(
                _reply_context_key, {}
            )
            last_bot_reply_sender_id = str(
                _last_reply_context.get("sender_id") or ""
            ).strip()
            if not last_bot_reply_sender_id:
                previous_sender_id = ""
                for historical_message in history_messages or []:
                    sender = getattr(historical_message, "sender", None)
                    sender_id = str(getattr(sender, "user_id", "") or "").strip()
                    if (
                        not sender_id
                        or sender_id == "unknown"
                        or sender_id.startswith(("history_user_", "refresh_user_"))
                    ):
                        continue
                    if sender_id == bot_id:
                        last_bot_reply_sender_id = previous_sender_id
                    else:
                        previous_sender_id = sender_id

            continuation_verified = bool(
                current_sender_id
                and last_bot_reply_sender_id
                and current_sender_id == last_bot_reply_sender_id
            )
            if not continuation_verified:
                if is_explicitly_addressed:
                    decision_result = decision_result.with_reply(
                        decision_result.reply,
                        continuation="no",
                        reason_code=(
                            "direct_request" if decision_result.reply else "none"
                        ),
                        source="policy",
                    )
                    logger.info(
                        "[Continuation boundary] Unverified continuation downgraded to direct request "
                        f"(current_sender={current_sender_id or 'unknown'}, "
                        f"reply_sender={last_bot_reply_sender_id or 'unknown'})"
                    )
                else:
                    decision_result = decision_result.with_reply(
                        False,
                        continuation="no",
                        reason_code="none",
                        source="policy",
                    )
                    logger.info(
                        "[Continuation boundary] Suppressed unverified group continuation "
                        f"(current_sender={current_sender_id or 'unknown'}, "
                        f"reply_sender={last_bot_reply_sender_id or 'unknown'})"
                    )

        should_reply = decision_result.reply
        decision_ai_failed = bool(getattr(event, "_decision_ai_error", False))
        if (
            not should_reply
            and decision_ai_failed
            and is_private
            and takeover_reply
            and not private_boundary
        ):
            # A failed attention check must not turn a normal private turn into silent loss.
            # The formal reply still goes through the provider's own safety checks.
            decision_result = decision_result.with_reply(
                True,
                target="bot",
                participation="direct",
                information="substantive",
                interest="weak",
                reason_code="direct_request",
                confidence="low",
                source="fallback",
                error="",
            )
            should_reply = True
            try:
                delattr(event, "_decision_ai_error")
            except AttributeError:
                pass
            logger.warning(
                "[DecisionAI] Private participation evaluation failed; "
                "falling back to the formal reply path"
            )
        if should_reply and private_late_night_review:
            event.set_extra("_persona_late_night_session_candidate", chat_id)
        if should_reply and is_private:
            if private_boundary:
                # Keep the old boundary until after_message_sent confirms a visible reply.
                if self.debug_mode:
                    logger.info(
                        "[Private boundary] Decision accepted reopening; "
                        "waiting for a visible reply before clearing state"
                    )
            else:
                self._private_conversation_state.clear_pending_messages(chat_id)
        if should_reply and not is_private:
            chat_key = ProbabilityManager.get_chat_key(
                platform_name, is_private, raw_chat_id
            )
            allowed, throttle_reason = self.participation_throttle.allow_and_record(
                chat_key, decision_result, is_private=is_private
            )
            if not allowed:
                decision_result = decision_result.with_reply(
                    False, source="policy", reason_code="none"
                )
                should_reply = False
                logger.info(
                    f"[Participation budget] suppressed {throttle_reason}: "
                    f"{decision_result.summary()}"
                )

        if not should_reply:
            if decision_ai_failed:
                logger.warning(
                    "[DecisionAI] Participation evaluation failed; "
                    "keeping the current silence policy"
                )
            if takeover_reply:
                try:
                    event.stop_event()
                except Exception:
                    pass
            # Keep boundary messages visible for a later reopening decision.
            private_wake_backlog = (
                event.get_extra("_persona_private_wake_backlog", []) or []
            )
            if is_private and private_boundary and private_wake_backlog:
                async with self.concurrent_lock:
                    for backlog_message in private_wake_backlog:
                        self._private_conversation_state.record_pending_message(
                            chat_id, backlog_message
                        )
            if cached_message_data:
                observed_message = dict(cached_message_data)
                is_boundary_pending = is_private and private_boundary is not None
                if is_boundary_pending:
                    wake_current = event.get_extra(
                        "_persona_private_wake_current", None
                    )
                    if isinstance(wake_current, dict):
                        observed_message = dict(wake_current)
                    async with self.concurrent_lock:
                        self._private_conversation_state.record_pending_message(
                            chat_id, observed_message
                        )
                    logger.debug(
                        "[Private boundary] Rejected message retained for a later reopening decision"
                    )
                else:
                    observed_message["decision_state"] = "observed"
                    self.cache_manager.add_to_cache(
                        chat_id,
                        observed_message,
                        source="AI decision observation",
                    )
                    logger.debug(
                        "[DecisionAI] Rejected message retained as an observation"
                    )

            # Retain absorbed private followers in the same boundary backlog.
            if use_smart_batch:
                smart_batch_messages = self._smart_batch_snapshots.pop(
                    early_message_id, []
                )
                for _smart_msg in smart_batch_messages:
                    _fallback_cache = dict(_smart_msg)
                    _fallback_cache.pop("window_buffered", None)
                    _fallback_cache.pop("smart_batch_dynamic_hint", None)
                    if is_private and private_boundary is not None:
                        async with self.concurrent_lock:
                            self._private_conversation_state.record_pending_message(
                                chat_id, _fallback_cache
                            )
                    else:
                        _fallback_cache["decision_state"] = "observed"
                        self.cache_manager.add_to_cache(
                            chat_id,
                            _fallback_cache,
                            source="AI decision observation - Smart batch",
                        )

            if self.debug_mode:
                cache_count = self.cache_manager.get_cache_count(chat_id)
                logger.info(f"  [缓存验证] 当前会话缓存数量: {cache_count} 条")

            self._message_cache_snapshots.pop(early_message_id, None)

            if self.debug_mode:
                logger.info("=" * 60)
            return

        # Private messages are independent turns unless Smart explicitly absorbs
        # them; do not add the legacy ten-second per-chat wait to either path.
        message_id = processing_id
        private_chat_parallel = bool(is_private)
        max_wait_loops = 1 if private_chat_parallel else self.concurrent_wait_max_loops
        wait_interval = 0 if private_chat_parallel else self.concurrent_wait_interval

        _concurrent_waited = False
        for loop_count in range(max_wait_loops):
            if use_smart_batch:
                consumed = await SmartConcurrentManager.is_consumed(message_id)
                if consumed:
                    logger.info(
                        f"🔀 [Smart并发] 消息 {message_id[:20]}... 已被更早批次吸收，跳过独立回复"
                    )
                    if takeover_reply:
                        try:
                            event.stop_event()
                        except Exception:
                            pass
                    await SmartConcurrentManager.remove_self(chat_id, message_id)
                    self._message_cache_snapshots.pop(message_id, None)
                    _smart_batch_followers = self._smart_batch_snapshots.pop(
                        message_id, None
                    )
                    if _smart_batch_followers:
                        for _sm in _smart_batch_followers:
                            _fallback = dict(_sm)
                            _fallback.pop("window_buffered", None)
                            _fallback.pop("smart_batch_dynamic_hint", None)
                            self.cache_manager.add_to_cache(
                                chat_id,
                                _fallback,
                                source="Smart并发-决策后被吸收-followers",
                            )
                    if cached_message_data:
                        self.cache_manager.add_to_cache(
                            chat_id,
                            dict(cached_message_data),
                            source="Smart并发-决策后被吸收-anchor",
                        )
                    return

                if await SmartConcurrentManager.has_earlier_pending(
                    chat_id, message_id
                ):
                    if loop_count == 0 and self.debug_mode:
                        logger.info(
                            "🔀 [Smart并发] 检测到更早到达的消息尚未完成，等待其先成为 anchor"
                        )
                    await asyncio.sleep(wait_interval)
                    continue

            # 获取锁进行原子性检查和标记
            async with self.concurrent_lock:
                if message_id in self.processing_sessions:
                    logger.info(
                        f"🚫 [并发去重] 消息 {message_id[:30]}... 已在处理中，跳过重复处理"
                    )
                    if takeover_reply:
                        try:
                            event.stop_event()
                        except Exception:
                            pass
                    return

                existing_processing = [
                    msg_id
                    for msg_id, cid in self.processing_sessions.items()
                    if cid == chat_id and msg_id != message_id
                ]

                if not existing_processing:
                    self.processing_sessions[message_id] = chat_id
                    if self.debug_mode:
                        logger.info(f"  已标记消息 {message_id[:30]}... 为本插件处理中")
                    break

            if loop_count == 0 and not private_chat_parallel:
                logger.warning(
                    f"⚠️ [并发检测] 会话 {chat_id} 中有 {len(existing_processing)} 条消息正在处理中，"
                    f"开始等待（最多 {max_wait_loops} 次，每次 {wait_interval} 秒）..."
                )
            if not private_chat_parallel:
                _concurrent_waited = True
            await asyncio.sleep(wait_interval)

            if self.debug_mode:
                logger.info(
                    f"  [并发等待] 第 {loop_count + 1}/{max_wait_loops} 次检测..."
                )
        else:
            async with self.concurrent_lock:
                still_processing = [
                    msg_id
                    for msg_id, cid in self.processing_sessions.items()
                    if cid == chat_id and msg_id != message_id
                ]
                if still_processing and not private_chat_parallel:
                    logger.warning(
                        f"⚠️ [并发警告] 等待 {max_wait_loops * wait_interval:.1f} 秒后仍有 "
                        f"{len(still_processing)} 条消息在处理，强制继续执行（可能产生竞争）"
                    )
                self.processing_sessions[message_id] = chat_id
                if self.debug_mode:
                    logger.info(f"  已标记消息 {message_id[:30]}... 为本插件处理中")

        # 并发等待后刷新上下文（仅当本消息确实等待过更早消息）
        if _concurrent_waited and history_messages is not None:
            try:
                _refreshed_history = await self._refresh_history_after_wait(
                    event, raw_chat_id, history_messages, self.max_context_messages
                )
                if _refreshed_history is not None:
                    history_messages = _refreshed_history
                    _bot_id = event.get_self_id()
                    _window_buffered_msgs = (
                        self.cache_manager.get_window_buffered_messages(chat_id)
                    )
                    formatted_context = await self._format_ai_context(
                        history_messages,
                        current_message_for_ai,
                        _bot_id,
                        window_msgs=_window_buffered_msgs,
                        poke_notice=poke_notice_text,
                    )
                    if self.debug_mode:
                        logger.info(
                            f"🔄 [并发刷新] 已刷新上下文，历史消息: {len(history_messages)} 条，"
                            f"上下文长度: {len(formatted_context)} 字符"
                        )
            except Exception as _refresh_err:
                logger.warning(
                    f"🔄 [并发刷新] 刷新上下文失败，使用原始上下文: {_refresh_err}"
                )

        # 表情包标记回退逻辑（处理跳过路径）
        if is_emoji_message and self.enable_emoji_filter and not emoji_marker_applied:
            has_image_info = bool(merged_image_urls) or (
                "[图片内容:" in message_text if message_text else False
            )
            if has_image_info and message_text and EMOJI_MARKER not in message_text:
                market_face_label = EmojiDetector.describe_market_faces(event)
                market_face_suffix = (
                    f"（QQ商城表情：{market_face_label}）" if market_face_label else ""
                )
                message_text = (
                    EmojiDetector.add_emoji_marker(message_text) + market_face_suffix
                )
                current_message_for_ai = _build_current_message_for_ai(message_text)
                bot_id = event.get_self_id()
                formatted_context = await self._format_ai_context(
                    history_messages,
                    current_message_for_ai,
                    bot_id,
                    window_msgs=self.cache_manager.get_window_buffered_messages(
                        chat_id
                    ),
                    poke_notice=poke_notice_text,
                )
                emoji_marker_applied = True

        try:
            smart_batch_reply_hint = ""
            reply_context_hint = ""
            if not is_private:
                has_at_others = bool(
                    isinstance(mention_info, dict) and mention_info.get("has_at_others")
                )
                if is_reply_to_other or has_at_others:
                    if is_explicitly_addressed:
                        reply_context_hint = (
                            "[系统提示-群聊回复边界] 当前消息中有一部分明确对你说，也涉及其他群友。"
                            "请只回答明确给你的部分；不要替其他群友回答、承诺或冒充对方。"
                        )
                    else:
                        reply_context_hint = (
                            "[系统提示-群聊旁观边界] 当前消息直接指向其他群友；"
                            "本次若回复，请用你自己的口吻、立场和经历补充相关内容。"
                            "不要替被@、被回复或被引用的用户作答；"
                            "消息里的“你”默认指向那位群友，不是你。"
                        )
            if decision_result.handoff_hint:
                reply_context_hint = (
                    f"{reply_context_hint}\n\n{decision_result.handoff_hint}"
                    if reply_context_hint
                    else decision_result.handoff_hint
                )
            reply_message_text = message_text
            if use_smart_batch:
                if smart_batch_messages:
                    try:
                        if is_private:
                            combined_parts = []
                            if message_text and message_text.strip():
                                combined_parts.append(message_text.strip())
                            for batch_message in smart_batch_messages:
                                batch_content = ContextManager._content_to_safe_text(
                                    batch_message.get("content", "")
                                ).strip()
                                if batch_content:
                                    combined_parts.append(batch_content)
                            if combined_parts:
                                reply_message_text = "\n".join(combined_parts)
                                current_message_for_ai = _build_current_message_for_ai(
                                    reply_message_text
                                )
                                formatted_context = await self._format_ai_context(
                                    history_messages,
                                    current_message_for_ai,
                                    event.get_self_id(),
                                    window_msgs=[],
                                    poke_notice=poke_notice_text,
                                )
                            else:
                                reply_message_text = message_text
                        else:
                            formatted_context = await self._format_ai_context(
                                history_messages,
                                current_message_for_ai,
                                event.get_self_id(),
                                window_msgs=smart_batch_messages,
                                poke_notice=poke_notice_text,
                            )
                    except Exception as smart_reply_ctx_err:
                        logger.warning(
                            f"[Smart并发] 回复阶段重建批次上下文失败，回退原上下文: {smart_reply_ctx_err}"
                        )

                if is_private and use_smart_batch:
                    logged_private_input = reply_message_text.replace("\n", " | ")
                    logger.info(
                        f"[Private Smart] Formal input ({1 + len(smart_batch_messages)} messages): "
                        f"{logged_private_input[:500]}"
                    )

                if (
                    use_smart_batch
                    and self.enable_smart_batch_reply_hint
                    and smart_batch_messages
                ):
                    try:
                        smart_batch_summary = self._summarize_smart_batch_messages(
                            smart_batch_messages,
                            anchor_sender_id=event.get_sender_id(),
                            anchor_content=message_text,
                        )
                        smart_batch_reply_hint = self._build_smart_batch_reply_hint(
                            event, smart_batch_summary
                        )
                    except Exception as smart_hint_err:
                        logger.warning(
                            f"[Smart并发] 生成批次回复提示失败，降级忽略: {smart_hint_err}"
                        )
                        smart_batch_reply_hint = ""

            precomputed_reply_text = None
            if speculative_reply_task is not None:
                if smart_batch_had_followers:
                    await _discard_speculative_reply()
                elif not speculative_reply_task.done():
                    await _discard_speculative_reply()
                    logger.info(
                        "[Private Speculative] Draft still running at the Smart commit; "
                        "canceling it and using the normal reply path"
                    )
                else:
                    try:
                        precomputed_reply_text = await speculative_reply_task
                        speculative_reply_task = None
                        if precomputed_reply_text:
                            logger.info(
                                "[Private Speculative] Reusing buffered formal reply after the batch window"
                            )
                        else:
                            logger.info(
                                "[Private Speculative] Draft unavailable; falling back to normal reply path"
                            )
                    except asyncio.CancelledError:
                        speculative_reply_task = None
                    except Exception as speculative_err:
                        speculative_reply_task = None
                        logger.warning(
                            f"[Private Speculative] Draft failed; falling back to normal reply path: {speculative_err}"
                        )

            if self.image_read_mode == "lazy" and merged_image_urls:
                (
                    formatted_context,
                    merged_image_urls,
                ) = await self._resolve_lazy_image_context(
                    event,
                    formatted_context,
                    merged_image_urls,
                )

            async for result in self._generate_and_send_reply(
                event,
                formatted_context,
                reply_message_text,
                platform_name,
                is_private,
                raw_chat_id,
                is_at_message,
                has_trigger_keyword,
                merged_image_urls,
                history_messages,
                current_message_cache,
                smart_batch_reply_hint=smart_batch_reply_hint,
                reply_context_hint=reply_context_hint,
                precomputed_reply_text=precomputed_reply_text,
            ):
                yield result
        finally:
            await _discard_speculative_decision()
            await _discard_speculative_reply()
            async with self.concurrent_lock:
                owner = self._chat_flow_owners.get(chat_id)
                if owner and owner.get("processing_id") == message_id:
                    self._chat_flow_owners.pop(chat_id, None)

            if use_smart_batch:
                await SmartConcurrentManager.remove_self(chat_id, message_id)

    # ============================================================
    # 初始检查与触发器
    # ============================================================

    async def _perform_initial_checks(self, event: AstrMessageEvent) -> tuple:
        """
        执行初始检查

        Returns:
            (should_continue, platform_name, is_private, chat_id)
        """
        if self.debug_mode:
            logger.info("=" * 60)
            logger.info("【步骤1】开始基础检查")

        if not self._is_enabled(event):
            if self.debug_mode:
                logger.info("【步骤1】群组未启用插件,跳过处理")
            return False, None, None, None

        if MessageProcessor.is_message_from_bot(event):
            if self.debug_mode:
                logger.info("忽略机器人自己的消息")
            return False, None, None, None

        platform_name = event.get_platform_name()
        is_private = event.is_private_chat()
        chat_id = event.get_group_id() if not is_private else event.get_sender_id()

        if self.debug_mode:
            logger.info("【步骤1】基础信息:")
            logger.info(f"  平台: {platform_name}")
            logger.info(f"  类型: {'私聊' if is_private else '群聊'}")
            logger.info(f"  会话ID: {chat_id}")
            logger.info(f"  发送者: {event.get_sender_name()}({event.get_sender_id()})")

        # 黑名单关键词检查
        blacklist_keywords = self.blacklist_keywords
        if KeywordChecker.check_blacklist_keywords(event, blacklist_keywords):
            if self.debug_mode:
                logger.info("【步骤2】黑名单关键词匹配，丢弃消息")
                logger.info("=" * 60)
            if self.takeover_private_reply if is_private else self.takeover_group_reply:
                event.stop_event()
            return False, None, None, None

        return True, platform_name, is_private, chat_id

    async def _check_message_triggers(self, event: AstrMessageEvent) -> tuple:
        """
        检查消息触发器（@消息和触发关键词）

        Returns:
            (is_at_message, has_trigger_keyword, matched_trigger_keyword)
        """
        is_at_message = MessageProcessor.is_at_message(event)

        if self.debug_mode:
            logger.info(
                f"【步骤3】@消息检测: {'是@消息' if is_at_message else '非@消息'}"
            )

        trigger_keywords = self.trigger_keywords
        has_trigger_keyword, matched_trigger_keyword = (
            KeywordChecker.check_trigger_keywords_with_match(event, trigger_keywords)
        )

        if has_trigger_keyword:
            if self.debug_mode:
                logger.info(
                    f"【步骤4】检测到触发关键词: {matched_trigger_keyword}，进入统一参与判断"
                )

        return is_at_message, has_trigger_keyword, matched_trigger_keyword

    # ============================================================
    # 参与判断
    # ============================================================

    # ============================================================
    # AI决策判断（参与判断）
    # ============================================================

    async def _check_ai_decision(
        self,
        event: AstrMessageEvent,
        formatted_context: str,
        is_at_message: bool,
        has_trigger_keyword: bool,
        image_urls: list[str] | None = None,
        matched_trigger_keyword: str = "",
        original_message_text: str = "",
        force_ai_decision: bool = False,
        is_directly_addressed: bool = False,
        is_reply_to_other: bool = False,
        has_at_others: bool = False,
    ) -> ParticipationDecision:
        """
        执行AI决策判断（在处理完消息内容后）

        Args:
            force_ai_decision: Keep AI gating for private media in direct mode.
            is_directly_addressed: Whether the current message explicitly targets the bot.
            is_reply_to_other: Whether the current message replies to another user.
            has_at_others: Whether the current message mentions another user.

        Returns:
            A validated participation decision for the reply pipeline.
        """
        platform_name = event.get_platform_name()
        is_private = event.is_private_chat()
        chat_id = event.get_group_id() if not is_private else event.get_sender_id()

        # Direct private chat treats the sender as the conversation target.
        # Keep AI gating available for private media and an explicit decide mode.
        if is_private and self.private_reply_mode == "direct" and not force_ai_decision:
            if self.debug_mode:
                logger.info("Private direct mode: bypassing group-style reply decision")
            return ParticipationDecision(
                reply=True,
                target="bot",
                participation="direct",
                information="substantive",
                interest="weak",
                reason_code="direct_request",
                confidence="high",
                source="policy",
            )

        # 在参与判断AI之前注入记忆（可选，pre_decision 模式）
        decision_formatted_context = formatted_context
        if (
            self.enable_memory_injection
            and self.memory_insertion_timing == "pre_decision"
        ):
            memory_mode = self.memory_plugin_mode
            livingmemory_top_k = self.livingmemory_top_k
            livingmemory_version = self.livingmemory_version
            livingmemory_persona_compat_mode = self.livingmemory_persona_compat_mode

            memory_mode, livingmemory_version = MemoryInjector.resolve_mode(
                self.context, memory_mode, livingmemory_version
            )

            if memory_mode is None:
                if self.debug_mode:
                    logger.info("[决策AI] auto模式未检测到可用的记忆插件，跳过记忆注入")
            elif MemoryInjector.check_memory_plugin_available(
                self.context, mode=memory_mode, version=livingmemory_version
            ):
                try:
                    memories = await MemoryInjector.get_memories(
                        self.context,
                        event,
                        mode=memory_mode,
                        top_k=livingmemory_top_k,
                        version=livingmemory_version,
                        persona_compat_mode=livingmemory_persona_compat_mode,
                    )
                    mem_text = str(memories).strip() if memories is not None else ""
                    if mem_text and ("当前没有任何记忆" not in mem_text):
                        old_len = len(decision_formatted_context)
                        decision_formatted_context = (
                            MemoryInjector.inject_memories_to_message(
                                decision_formatted_context, mem_text
                            )
                        )
                        if self.debug_mode:
                            logger.info(
                                f"[决策AI] 已在判定前注入记忆({memory_mode}模式)，长度增加: {len(decision_formatted_context) - old_len} 字符"
                            )
                        try:
                            ckey = ProbabilityManager.get_chat_key(
                                platform_name, is_private, chat_id
                            )
                            if not hasattr(self, "_pre_decision_context_by_chat"):
                                self._pre_decision_context_by_chat = {}
                            self._pre_decision_context_by_chat[ckey] = (
                                decision_formatted_context
                            )
                        except Exception:
                            pass
                except Exception as e:
                    logger.warning(f"[决策AI] 判定前注入记忆失败: {e}", exc_info=True)
            elif self.debug_mode:
                logger.info(
                    f"[决策AI] 记忆插件({memory_mode}模式)不可用，判定前跳过记忆注入"
                )

        # Every eligible message goes through the same interest decision.
        # Keywords and @ signals change attention context, never the final answer.

        if self.debug_mode:
            logger.info("【步骤9】调用决策AI判断是否回复")

        _decision_start = time.time()

        # A keyword is an attention signal, never a reply bypass.
        is_keyword_triggered = has_trigger_keyword

        decision_extra_prompt = self.decision_ai_extra_prompt
        decision_prompt_mode = self.decision_ai_prompt_mode
        decision_reply_tendency = self.decision_ai_reply_tendency
        if is_private:
            decision_reply_tendency = self.private_decision_ai_reply_tendency
            if self.private_decision_ai_extra_prompt.strip():
                decision_extra_prompt = self.private_decision_ai_extra_prompt
                decision_prompt_mode = self.private_decision_ai_prompt_mode

        decision_result = await DecisionAI.evaluate(
            self.context,
            event,
            decision_formatted_context,
            self.decision_ai_provider_id,
            decision_extra_prompt,
            self.decision_ai_timeout,
            decision_prompt_mode,
            image_urls=image_urls,
            include_sender_info=self.include_sender_info,
            is_keyword_triggered=is_keyword_triggered,
            matched_keyword=matched_trigger_keyword,
            enable_reasoning=self.enable_decision_ai_reasoning,
            reasoning_log_enabled=self.decision_ai_reasoning_log,
            reasoning_log_mode=self.decision_ai_reasoning_log_mode,
            reasoning_start_marker=self.judgment_reasoning_start_marker,
            reasoning_end_marker=self.judgment_reasoning_end_marker,
            include_persona=self.decision_ai_include_persona,
            configured_persona_name=self.decision_ai_persona_name,
            reply_tendency=decision_reply_tendency,
            is_private=is_private,
            is_directly_addressed=is_directly_addressed,
            is_reply_to_other=is_reply_to_other,
            has_at_others=has_at_others,
        )

        if self.debug_mode:
            _decision_elapsed = time.time() - _decision_start
            logger.info(f"【步骤9】决策AI判断完成，耗时: {_decision_elapsed:.2f}秒")

        if not decision_result.reply:
            logger.debug(f"决策AI判断: 不应该回复此消息 ({decision_result.summary()})")
            # 清理pre_decision缓存（防止内存残留）
            try:
                ckey = ProbabilityManager.get_chat_key(
                    platform_name, is_private, chat_id
                )
                if (
                    hasattr(self, "_pre_decision_context_by_chat")
                    and ckey in self._pre_decision_context_by_chat
                ):
                    del self._pre_decision_context_by_chat[ckey]
            except Exception:
                pass
            return decision_result

        logger.debug(f"决策AI判断: 应该回复此消息 ({decision_result.summary()})")
        return decision_result

    # ============================================================
    # 消息内容处理
    # ============================================================

    async def _process_message_content(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        current_group_seq: int,
        is_at_message: bool,
        mention_info: dict = None,
        has_trigger_keyword: bool = False,
        poke_info: dict = None,
        raw_is_at_message: bool = None,
        is_emoji_message: bool = False,
        emoji_signature: str = "",
        is_at_all_message: bool = False,
        persistent_poke_event_text: str = "",
        force_defer_image_processing: bool = False,
    ) -> tuple:
        """
        处理消息内容（图片处理、上下文格式化）

        Args:
            force_defer_image_processing: Defer vision inference for unaddressed
                group messages until the reply decision accepts the conversation.

        Returns:
            (should_continue, original_message_text, processed_message, formatted_context,
             image_urls, history_messages, cached_message, emoji_marker_applied)
        """
        state_chat_id = ProbabilityManager.get_chat_key(
            event.get_platform_name(), event.is_private_chat(), chat_id
        )

        if self.debug_mode:
            logger.info("【步骤6】提取纯净原始消息")

        original_message_text = MessageCleaner.extract_raw_message_from_event(
            event, self_id=str(event.get_self_id())
        )
        if self.debug_mode:
            logger.info(f"  纯净原始消息: {original_message_text[:100]}...")

        real_is_at_message = (
            raw_is_at_message if raw_is_at_message is not None else is_at_message
        )

        # only_ai: 只把"只包含@AI且没有他人/全体/正文"的空消息视为单独无信息@AI
        is_empty_at = MessageCleaner.is_empty_at_message(
            original_message_text,
            real_is_at_message,
            mention_info=mention_info,
            mode="only_ai",
        )
        if is_empty_at and self.debug_mode:
            logger.info("  纯@消息将使用特殊处理")

        # 处理图片
        if self.debug_mode:
            logger.info("【步骤6.5】处理图片内容")

        (
            should_continue,
            processed_message,
            image_urls,
            image_retained,
        ) = await ImageHandler.process_message_images(
            event,
            self.context,
            self.enable_image_processing,
            self.image_to_text_scope,
            self.image_to_text_provider_id,
            self.image_to_text_prompt,
            real_is_at_message,
            has_trigger_keyword,
            self.image_to_text_timeout,
            self.image_description_cache,
            self.max_images_per_message,
            self_id=str(event.get_self_id()),
            defer_image_processing=(
                self.image_read_mode == "lazy" or force_defer_image_processing
            ),
        )

        if not should_continue:
            logger.info("图片处理后决定丢弃此消息（图片被过滤或处理失败）")
            if self.debug_mode:
                logger.info("【步骤6.5】图片处理判定丢弃消息，不缓存")
                logger.info("=" * 60)
            return False, None, None, None, None, None, None, False, {}

        # 提取非图片媒体文件（语音/视频/文件）的路径并内联注入
        _media_audio_urls: list = []
        try:
            (
                _media_audio_urls,
                _media_video_paths,
                _media_file_infos,
            ) = await ImageHandler.extract_media_urls(event)
            if _media_audio_urls or _media_video_paths or _media_file_infos:
                processed_message = ImageHandler.enrich_media_markers(
                    processed_message,
                    audio_urls=_media_audio_urls,
                    video_paths=_media_video_paths,
                    file_infos=_media_file_infos,
                )
                event.set_extra("_plugin_media_audio_urls", _media_audio_urls)
                if self.debug_mode:
                    logger.info(
                        f"【步骤6.45】提取非图片媒体: audio={len(_media_audio_urls)}个, "
                        f"video={len(_media_video_paths)}个, file={len(_media_file_infos)}个"
                    )
        except Exception as e:
            logger.warning(f"[媒体提取] 提取非图片媒体文件时出错（已跳过）: {e}")

        # 表情包标记注入（正常处理路径）
        emoji_marker_applied = False
        if is_emoji_message and self.enable_emoji_filter and image_retained:
            market_face_label = EmojiDetector.describe_market_faces(event)
            market_face_suffix = (
                f"（QQ商城表情：{market_face_label}）" if market_face_label else ""
            )
            if processed_message:
                processed_message = (
                    EmojiDetector.add_emoji_marker(processed_message)
                    + market_face_suffix
                )
            else:
                processed_message = (
                    EmojiDetector.add_emoji_marker("") + market_face_suffix
                )
            emoji_marker_applied = True

        current_message_id = self._get_processing_id(event)

        # 准备待缓存的用户消息数据
        cached_message = {
            "role": "user",
            "content": processed_message,
            "timestamp": time.time(),
            "message_id": current_message_id,
            "sender_id": event.get_sender_id(),
            "sender_name": event.get_sender_name(),
            "message_timestamp": event.message_obj.timestamp
            if hasattr(event, "message_obj") and hasattr(event.message_obj, "timestamp")
            else None,
            "mention_info": mention_info,
            "group_seq": current_group_seq,
            "is_at_message": is_at_message,
            "has_trigger_keyword": has_trigger_keyword,
            "poke_info": poke_info,
            "persistent_poke_event_text": persistent_poke_event_text,
            "image_urls": image_urls or [],
            "audio_urls": _media_audio_urls,
            "is_emoji_message": is_emoji_message,
            "emoji_signature": emoji_signature,
            "is_at_all_message": is_at_all_message,
            "is_empty_at": is_empty_at,
        }

        # 确定触发方式
        trigger_type = None
        if real_is_at_message:
            trigger_type = "at"
        elif has_trigger_keyword:
            trigger_type = "keyword"
        elif MessageProcessor.is_reply_to_bot(event) or (
            isinstance(poke_info, dict) and poke_info.get("is_poke_bot")
        ):
            trigger_type = "direct"
        else:
            trigger_type = "ai_decision"

        # 戳过对方追踪提示
        poke_trace_text = ""
        if (
            self.poke_trace_enabled
            and self._is_poke_enabled_in_group(chat_id)
            and self._check_and_consume_poke_trace(state_chat_id, event.get_sender_id())
        ):
            _n = self._safe_sender_display(event)
            _id = event.get_sender_id()
            poke_trace_text = f"[戳过对方提示]你刚刚戳过这条消息的发送者{_n}(ID:{_id})"

        message_text_for_ai = MessageProcessor.add_metadata_to_message(
            event,
            processed_message,
            self.include_timestamp,
            self.include_sender_info,
            mention_info,
            trigger_type,
            poke_info,
            is_empty_at,
            "",
            "",
            is_at_all_message=is_at_all_message,
            persistent_poke_event_text=persistent_poke_event_text,
            poke_trace_text=poke_trace_text,
        )

        # [戳一戳提示] 由 format_context_for_ai 追加到分隔符之外
        _poke_notice_text = ""
        if poke_info and isinstance(poke_info, dict):
            try:
                _is_poke_bot = poke_info.get("is_poke_bot", False)
                _sender_id = str(poke_info.get("sender_id", "") or "")
                _sender_name = (
                    str(poke_info.get("sender_name", "") or "").strip() or "未知用户"
                )
                _sender_display = (
                    f"{_sender_name}(ID:{_sender_id})" if _sender_id else _sender_name
                )
                _target_id = str(poke_info.get("target_id", "") or "")
                _target_name = (
                    str(poke_info.get("target_name", "") or "").strip() or "未知用户"
                )
                _target_display = (
                    f"{_target_name}(ID:{_target_id})" if _target_id else _target_name
                )
                if _is_poke_bot:
                    _poke_notice_text = (
                        f"[戳一戳提示]有人在戳你，戳你的人是{_sender_display}"
                    )
                else:
                    _poke_notice_text = f"[戳一戳提示]这是一个戳一戳消息，但不是戳你的，是{_sender_display}在戳{_target_display}"
            except Exception:
                _poke_notice_text = ""

        if self.debug_mode:
            logger.info("【步骤7.5】为当前消息添加元数据（用于AI识别）")
            logger.info(f"  添加元数据后: {message_text_for_ai[:150]}...")

        # 提取历史上下文
        max_context = self.max_context_messages

        # 配置矫正
        if not isinstance(max_context, int):
            try:
                max_context = int(max_context)
            except (ValueError, TypeError):
                max_context = -1
        if isinstance(max_context, int) and max_context < -1:
            max_context = -1

        if self.debug_mode:
            logger.info("【步骤8】提取历史上下文")

        # 获取 AstrBot 官方历史消息并合并待决策缓存。
        if isinstance(max_context, int) and max_context == 0:
            history_messages = []
        else:
            history_messages = await ContextManager.get_history_messages_with_fallback(
                event=event,
                max_messages=max_context,
                context=self.context,
                cached_messages=[],
            )
            # Generic role history lacks reliable group sender and timestamp metadata.
            # Keep it for private chats, where the conversation is one-to-one.
            try:
                cm = self.context.conversation_manager
                if is_private and cm:
                    uid = event.unified_msg_origin
                    cid = await cm.get_curr_conversation_id(uid)
                    if cid:
                        conv = await cm.get_conversation(
                            unified_msg_origin=uid, conversation_id=cid
                        )
                        official_history = None
                        if conv is not None:
                            if getattr(conv, "history", None):
                                try:
                                    official_history = json.loads(conv.history)
                                except Exception:
                                    official_history = None
                            if official_history is None and getattr(
                                conv, "content", None
                            ):
                                if isinstance(conv.content, list):
                                    official_history = conv.content
                                else:
                                    try:
                                        official_history = json.loads(conv.content)
                                    except Exception:
                                        official_history = None
                        if (
                            isinstance(official_history, list)
                            and len(official_history) > 0
                        ):
                            hist_msgs = []
                            self_id = event.get_self_id()
                            platform_name = event.get_platform_name()
                            is_private_chat = event.is_private_chat()
                            default_user_name = "对方" if is_private_chat else "群友"
                            if isinstance(max_context, int):
                                if max_context == -1:
                                    msgs_iter = official_history
                                elif max_context > 0:
                                    msgs_iter = official_history[-max_context:]
                                else:
                                    msgs_iter = []
                            else:
                                msgs_iter = official_history
                            for idx, msg in enumerate(msgs_iter):
                                if (
                                    isinstance(msg, dict)
                                    and "role" in msg
                                    and "content" in msg
                                ):
                                    m = AstrBotMessage()
                                    m.message_str = (
                                        ContextManager._content_to_safe_text(
                                            msg.get("content")
                                        )
                                    )
                                    m.platform_name = platform_name
                                    _ts = (
                                        msg.get("timestamp")
                                        or msg.get("ts")
                                        or msg.get("time")
                                    )
                                    try:
                                        m.timestamp = (
                                            int(float(_ts)) if _ts else int(time.time())
                                        )
                                    except Exception:
                                        m.timestamp = int(time.time())
                                    m.type = (
                                        MessageType.GROUP_MESSAGE
                                        if not is_private_chat
                                        else MessageType.FRIEND_MESSAGE
                                    )
                                    if not is_private_chat:
                                        m.group_id = event.get_group_id()
                                    m.self_id = self_id
                                    m.session_id = getattr(
                                        event, "session_id", None
                                    ) or (
                                        event.get_sender_id()
                                        if is_private_chat
                                        else event.get_group_id()
                                    )
                                    raw_message_id = (
                                        msg.get("message_id")
                                        or msg.get("id")
                                        or msg.get("mid")
                                        or ""
                                    )
                                    m.message_id = (
                                        str(raw_message_id)
                                        or f"official_{idx}_{m.timestamp}"
                                    )
                                    if msg["role"] == "assistant":
                                        m.sender = MessageMember(
                                            user_id=self_id, nickname="AI"
                                        )
                                    else:
                                        sender_info = (
                                            msg.get("sender")
                                            if isinstance(msg.get("sender"), dict)
                                            else None
                                        )
                                        sender_id = None
                                        sender_name = None
                                        if sender_info:
                                            sender_id = (
                                                sender_info.get("user_id")
                                                or sender_info.get("id")
                                                or sender_info.get("uid")
                                                or sender_info.get("qq")
                                                or sender_info.get("uin")
                                            )
                                            sender_name = sender_info.get(
                                                "nickname"
                                            ) or sender_info.get("name")
                                        sender_id = (
                                            str(sender_id)
                                            if sender_id is not None
                                            else f"history_user_{idx}"
                                        )
                                        sender_name = sender_name or default_user_name
                                        m.sender = MessageMember(
                                            user_id=sender_id, nickname=sender_name
                                        )
                                    hist_msgs.append(m)
                            if hist_msgs:
                                if history_messages:
                                    existing_contents = set()
                                    for _existing in history_messages:
                                        content = None
                                        if isinstance(_existing, AstrBotMessage):
                                            content = getattr(
                                                _existing, "message_str", None
                                            )
                                        elif isinstance(_existing, dict):
                                            content = (
                                                ContextManager._make_content_hashable(
                                                    _existing.get("content")
                                                )
                                            )
                                        if content is not None:
                                            existing_contents.add(content)
                                    for hm in hist_msgs:
                                        if (
                                            hm.message_str
                                            and hm.message_str in existing_contents
                                        ):
                                            continue
                                        history_messages.append(hm)
                                        if hm.message_str:
                                            existing_contents.add(hm.message_str)
                                else:
                                    history_messages = hist_msgs
            except Exception:
                pass

        # 使用缓存管理器合并缓存消息
        if isinstance(max_context, int) and max_context == 0:
            if self.debug_mode:
                logger.info("  跳过缓存合并: max_context_messages=0")
        else:
            history_messages, cached_count, dedup_skipped = (
                self.cache_manager.merge_cache_to_history(
                    chat_id=state_chat_id,
                    history_messages=history_messages,
                    event=event,
                    current_message_id=None,
                )
            )
            if self.debug_mode and cached_count > 0:
                logger.info(f"  [缓存管理器] 已合并 {cached_count} 条缓存消息到历史")

        # 应用上下文限制（按时间保留最新的）
        if (
            history_messages
            and isinstance(max_context, int)
            and max_context > 0
            and len(history_messages) > max_context
        ):
            history_messages = history_messages[-max_context:]

        if self.debug_mode:
            logger.info(
                f"  最终历史消息: {len(history_messages) if history_messages else 0} 条"
            )

        # 获取窗口缓冲消息（Smart批次）
        window_buffered_msgs = self.cache_manager.get_window_buffered_messages(
            state_chat_id
        )

        # 格式化上下文
        bot_id = event.get_self_id()
        formatted_context = await self._format_ai_context(
            history_messages,
            message_text_for_ai,
            bot_id,
            window_msgs=window_buffered_msgs,
            poke_notice=_poke_notice_text,
        )
        pending_private_context = (
            self._private_conversation_state.build_pending_context(state_chat_id)
        )
        if pending_private_context:
            formatted_context += "\n\n" + pending_private_context

        if self.debug_mode:
            logger.info(f"  格式化后长度: {len(formatted_context)} 字符")

        return (
            True,
            original_message_text,
            processed_message,
            formatted_context,
            image_urls,
            history_messages,
            cached_message,
            emoji_marker_applied,
        )

    async def _prepare_reply_context(
        self,
        event: AstrMessageEvent,
        formatted_context: str,
        platform_name: str,
        is_private: bool,
        chat_id: str,
    ) -> tuple[str, str]:
        """Prepare memory-enriched context before formal reply generation.

        Args:
            event: Message event for memory retrieval.
            formatted_context: Base context containing history and current text.
            platform_name: Platform name used to scope memory state.
            is_private: Whether the conversation is private.
            chat_id: Raw platform chat identifier.

        Returns:
            A tuple containing the final prompt context and scoped chat key.
        """
        final_message = formatted_context
        state_chat_id = ProbabilityManager.get_chat_key(
            platform_name, is_private, chat_id
        )
        try:
            if (
                self.enable_memory_injection
                and self.memory_insertion_timing == "pre_decision"
                and hasattr(self, "_pre_decision_context_by_chat")
                and state_chat_id in self._pre_decision_context_by_chat
            ):
                final_message = self._pre_decision_context_by_chat.pop(
                    state_chat_id, formatted_context
                )
        except Exception:
            pass

        if (
            self.enable_memory_injection
            and self.memory_insertion_timing == "post_decision"
        ):
            if self.debug_mode:
                logger.info("Injecting memory before formal reply generation")
            memory_mode, memory_version = MemoryInjector.resolve_mode(
                self.context, self.memory_plugin_mode, self.livingmemory_version
            )
            if memory_mode is None:
                if self.debug_mode:
                    logger.info("No compatible memory plugin is available")
            elif MemoryInjector.check_memory_plugin_available(
                self.context, mode=memory_mode, version=memory_version
            ):
                memories = await MemoryInjector.get_memories(
                    self.context,
                    event,
                    mode=memory_mode,
                    top_k=self.livingmemory_top_k,
                    version=memory_version,
                    persona_compat_mode=self.livingmemory_persona_compat_mode,
                )
                if memories:
                    final_message = MemoryInjector.inject_memories_to_message(
                        final_message, memories
                    )
            elif self.debug_mode:
                logger.info("Memory plugin is unavailable; skipping memory injection")

        return final_message, state_chat_id

    async def _generate_speculative_private_reply(
        self,
        event: AstrMessageEvent,
        formatted_context: str,
        platform_name: str,
        chat_id: str,
        history_messages: list,
    ) -> str | None:
        """Generate a buffered direct private text reply without executing tools.

        Args:
            event: Anchor event whose response remains uncommitted.
            formatted_context: Anchor-only formal reply context.
            platform_name: Platform name for scoped memory lookup.
            chat_id: Raw private chat identifier.
            history_messages: History snapshot used while building the prompt.

        Returns:
            Plain reply text, or None when the fast path is unavailable.
        """
        final_message, _ = await self._prepare_reply_context(
            event, formatted_context, platform_name, True, chat_id
        )
        return await ReplyHandler.generate_speculative_reply(
            event=event,
            context=self.context,
            formatted_message=final_message,
            extra_prompt=self.reply_ai_extra_prompt,
            prompt_mode=self.reply_ai_prompt_mode,
            history_messages=history_messages,
        )

    # ============================================================
    # 生成并发送回复
    # ============================================================

    async def _generate_and_send_reply(
        self,
        event: AstrMessageEvent,
        formatted_context: str,
        message_text: str,
        platform_name: str,
        is_private: bool,
        chat_id: str,
        is_at_message: bool = False,
        has_trigger_keyword: bool = False,
        image_urls: list = None,
        history_messages: list = None,
        current_message_cache: dict = None,
        smart_batch_reply_hint: str = "",
        reply_context_hint: str = "",
        precomputed_reply_text: str | None = None,
    ):
        """
        生成并发送回复，保存历史

        Returns:
            生成器，用于yield回复
        """
        _process_start_time = time.time()

        if image_urls is None:
            image_urls = []

        if precomputed_reply_text is None:
            final_message, state_chat_id = await self._prepare_reply_context(
                event, formatted_context, platform_name, is_private, chat_id
            )
        else:
            final_message = formatted_context
            state_chat_id = ProbabilityManager.get_chat_key(
                platform_name, is_private, chat_id
            )

        # Generate the formal reply after context preparation.
        if self.debug_mode:
            logger.info("【步骤13】调用AI生成回复")
            logger.info(f"  最终消息长度: {len(final_message)} 字符")

        _start_time = time.time()

        ai_error_flag = False
        message_id_for_error = None
        try:
            message_id_for_error = self._get_processing_id(event)
        except Exception:
            message_id_for_error = None

        try:
            if precomputed_reply_text is not None:
                reply_result = event.plain_result(
                    precomputed_reply_text
                ).set_result_content_type(ResultContentType.LLM_RESULT)
                event.set_extra(
                    "_persona_precomputed_reply",
                    {
                        "message_id": message_id_for_error,
                        "text": precomputed_reply_text,
                    },
                )
            else:
                # 从 event extras 读取语音URL（视频/文件路径已内联到 final_message 中）
                _media_audio_urls = (
                    event.get_extra("_plugin_media_audio_urls", []) or []
                )

                reply_result = await ReplyHandler.generate_reply(
                    event,
                    self.context,
                    final_message,
                    self.reply_ai_extra_prompt,
                    self.reply_ai_prompt_mode,
                    image_urls,
                    audio_urls=_media_audio_urls,
                    include_sender_info=self.include_sender_info,
                    include_timestamp=self.include_timestamp,
                    history_messages=history_messages,
                    smart_batch_reply_hint=smart_batch_reply_hint,
                    reply_context_hint=reply_context_hint,
                )
        except Exception as e:
            ai_error_flag = True
            logger.error(f"生成AI回复时发生未捕获异常: {e}", exc_info=True)
            reply_result = event.plain_result(f"生成回复时发生错误: {str(e)}")

        try:
            ai_error_flag = ai_error_flag or bool(
                event.get_extra("_group_chat_plus_reply_error", False)
            )
        except Exception:
            pass

        if (
            not ai_error_flag
            and hasattr(reply_result, "is_llm_result")
            and hasattr(reply_result, "chain")
        ):
            try:
                if not reply_result.is_llm_result():
                    parts = []
                    for comp in getattr(reply_result, "chain", []) or []:
                        text = self._coerce_component_text(getattr(comp, "text", None))
                        if text:
                            parts.append(text)
                    err_text = "".join(parts)
                    if "生成回复时发生错误" in err_text:
                        ai_error_flag = True
            except Exception:
                pass

        if ai_error_flag:
            takeover_on_error = (
                self.takeover_private_reply if is_private else self.takeover_group_reply
            )
            if takeover_on_error:
                try:
                    event.stop_event()
                except Exception:
                    pass
        if ai_error_flag and message_id_for_error:
            try:
                self._ai_error_message_ids.add(message_id_for_error)
            except Exception:
                pass

        _elapsed = time.time() - _start_time
        if self.debug_mode:
            logger.info(f"【步骤13】AI回复生成完成，耗时: {_elapsed:.2f}秒")
        elif _elapsed > self.reply_generation_timeout_warning:
            logger.warning(
                f"⚠️ AI回复生成耗时异常: {_elapsed:.2f}秒（超过{self.reply_generation_timeout_warning}秒）"
            )

        # 保存用户消息（从缓存读取并添加元数据）
        if self.debug_mode:
            logger.info("【步骤14】保存用户消息")

        try:
            message_to_save = ""
            last_cached = current_message_cache

            if not last_cached:
                msg_id_for_lookup = self._get_processing_id(event)
                if state_chat_id in self.pending_messages_cache:
                    for cached_msg in reversed(
                        self.pending_messages_cache[state_chat_id]
                    ):
                        if (
                            isinstance(cached_msg, dict)
                            and cached_msg.get("message_id") == msg_id_for_lookup
                        ):
                            last_cached = cached_msg
                            break

            if (
                last_cached
                and isinstance(last_cached, dict)
                and "content" in last_cached
            ):
                raw_content = last_cached["content"]

                trigger_type = None
                if last_cached.get("has_trigger_keyword"):
                    trigger_type = "keyword"
                elif last_cached.get("is_at_message"):
                    trigger_type = "at"
                else:
                    trigger_type = "ai_decision"

                message_to_save = MessageProcessor.add_metadata_from_cache(
                    raw_content,
                    last_cached.get("sender_id", event.get_sender_id()),
                    last_cached.get("sender_name", event.get_sender_name()),
                    last_cached.get("message_timestamp")
                    or last_cached.get("timestamp"),
                    self.include_timestamp,
                    self.include_sender_info,
                    last_cached.get("mention_info"),
                    trigger_type,
                    last_cached.get("poke_info"),
                    last_cached.get("is_empty_at", False),
                    "",
                    last_cached.get("is_at_all_message", False),
                    persistent_poke_event_text=last_cached.get(
                        "persistent_poke_event_text", ""
                    ),
                )
                message_to_save = MessageCleaner.clean_message(message_to_save)

            # 如果从缓存获取失败，使用当前处理后的消息
            if not message_to_save:
                logger.warning("⚠️ 缓存中无消息，使用当前处理后的消息（这不应该发生！）")
                trigger_type = None
                if has_trigger_keyword:
                    trigger_type = "keyword"
                elif is_at_message:
                    trigger_type = "at"
                else:
                    trigger_type = "ai_decision"

                _fb_mention_info = (
                    last_cached.get("mention_info")
                    if isinstance(last_cached, dict)
                    else None
                )
                _fb_at_all = bool(
                    last_cached.get("is_at_all_message", False)
                    if isinstance(last_cached, dict)
                    else False
                )
                message_to_save = MessageProcessor.add_metadata_to_message(
                    event,
                    message_text,
                    self.include_timestamp,
                    self.include_sender_info,
                    _fb_mention_info,
                    trigger_type,
                    None,
                    False,
                    "",
                    "",
                    is_at_all_message=_fb_at_all,
                    persistent_poke_event_text=last_cached.get(
                        "persistent_poke_event_text", ""
                    )
                    if last_cached
                    else "",
                )
                message_to_save = MessageCleaner.clean_message(message_to_save)

            if self.debug_mode:
                logger.info(f"  准备保存的完整消息: {message_to_save[:300]}...")

            await ContextManager.save_user_message(event, message_to_save, self.context)
        except Exception as e:
            logger.error(f"保存用户消息时发生错误: {e}", exc_info=True)

        # 发送前过滤检查：防止直接转发用户消息和重复发送相同回复
        reply_text = ""
        is_provider_request = False
        if reply_result:
            is_provider_request = isinstance(reply_result, ProviderRequest)
            if isinstance(reply_result, str):
                reply_text = reply_result.strip()
            elif hasattr(reply_result, "chain"):
                reply_text = "".join(
                    self._coerce_component_text(getattr(comp, "text", None))
                    for comp in reply_result.chain
                ).strip()

        # 检查1: 回复是否与用户消息相同（防止直接转发）
        if reply_text and not is_provider_request:
            user_message_clean = message_text.strip()
            if reply_text == user_message_clean:
                logger.info("[消息过滤]回复与用户消息相同，已过滤")
                return

        # 检查2: 回复是否与最近发送的回复重复
        is_duplicate_blocked = False
        if reply_text and not is_provider_request and self.enable_duplicate_filter:
            if state_chat_id not in self.recent_replies_cache:
                self.recent_replies_cache[state_chat_id] = []

            current_time = time.time()
            if self.enable_duplicate_time_limit:
                time_limit = max(60, self.duplicate_filter_time_limit)
                self.recent_replies_cache[state_chat_id] = [
                    reply
                    for reply in self.recent_replies_cache[state_chat_id]
                    if current_time - reply.get("timestamp", 0) < time_limit
                ]

            check_count = max(1, self.duplicate_filter_check_count)
            for recent_reply in self.recent_replies_cache[state_chat_id][-check_count:]:
                recent_content = recent_reply.get("content", "")
                recent_timestamp = recent_reply.get("timestamp", 0)
                if self.enable_duplicate_time_limit:
                    time_limit = max(60, self.duplicate_filter_time_limit)
                    if current_time - recent_timestamp >= time_limit:
                        continue
                if recent_content and reply_text == recent_content.strip():
                    logger.info(
                        "[消息过滤]回复与最近发送的回复重复，已拦截发送（后续流程继续执行）"
                    )
                    is_duplicate_blocked = True
                    break

        # 发送回复
        if not is_duplicate_blocked:
            if reply_result is None:
                logger.error("❌ [发送失败] reply_result为None，无法发送回复")
                return

            if self.debug_mode:
                logger.info(
                    f"【步骤13.9】准备发送回复，类型: {type(reply_result).__name__}"
                )

            # 插件发起 LLM 请求时标记已调用 LLM，阻止框架对 @消息触发第二次默认 LLM 调用
            if (
                isinstance(reply_result, ProviderRequest)
                or precomputed_reply_text is not None
            ):
                event.call_llm = True
                if precomputed_reply_text is not None and message_id_for_error:
                    self._agent_done_flags.add(message_id_for_error)

            yield reply_result

            if not is_private and not ai_error_flag:
                reply_sender_id = str(
                    (
                        current_message_cache.get("sender_id")
                        if isinstance(current_message_cache, dict)
                        else None
                    )
                    or event.get_sender_id()
                    or ""
                ).strip()
                if reply_sender_id:
                    reply_chat_key = ProbabilityManager.get_chat_key(
                        event.get_platform_name(), is_private, chat_id
                    )
                    try:
                        reply_group_seq = int(
                            (
                                current_message_cache.get("group_seq", 0)
                                if isinstance(current_message_cache, dict)
                                else 0
                            )
                            or 0
                        )
                    except (TypeError, ValueError):
                        reply_group_seq = 0
                    previous_reply_context = self._last_bot_reply_context.get(
                        reply_chat_key
                    )
                    try:
                        previous_group_seq = int(
                            (previous_reply_context or {}).get("group_seq", -1)
                        )
                    except (TypeError, ValueError):
                        previous_group_seq = -1
                    if (
                        previous_reply_context is None
                        or reply_group_seq >= previous_group_seq
                    ):
                        self._last_bot_reply_context[reply_chat_key] = {
                            "sender_id": reply_sender_id,
                            "group_seq": reply_group_seq,
                        }

            # Fallback only when the completion hook did not mark the agent done.
            # Normal final LLM replies are saved by after_message_sent after this generator yields.
            message_id = self._get_processing_id(event)
            if (
                message_id in self._pending_bot_replies
                and self._pending_bot_replies[message_id]
                and message_id not in self._agent_done_flags
            ):
                logger.warning(
                    f"[安全兜底] 检测到 {len(self._pending_bot_replies[message_id])} 段未保存的累积回复"
                    "（未收到 agent 完成标志），执行兜底保存"
                )
                try:
                    await self._finalize_bot_reply_save(event, message_id)
                except Exception as fallback_err:
                    logger.error(
                        f"[安全兜底] 兜底保存失败: {fallback_err}", exc_info=True
                    )

            if self.debug_mode:
                logger.info("【步骤13.9】回复已通过yield发送")
        else:
            if self.debug_mode:
                logger.info("【步骤13.9】跳过发送回复（重复消息已拦截），继续后续流程")

        # 记录已发送的回复（用于后续去重检查）
        if reply_text and not is_provider_request and not is_duplicate_blocked:
            if state_chat_id not in self.recent_replies_cache:
                self.recent_replies_cache[state_chat_id] = []
            self.recent_replies_cache[state_chat_id].append(
                {"content": reply_text, "timestamp": time.time()}
            )
            max_cache_size = min(
                max(10, self.duplicate_filter_check_count * 2),
                self._DUPLICATE_CACHE_SIZE_LIMIT,
            )
            if len(self.recent_replies_cache[state_chat_id]) > max_cache_size:
                self.recent_replies_cache[state_chat_id] = self.recent_replies_cache[
                    state_chat_id
                ][-max_cache_size:]

        if self.debug_mode:
            logger.info("=" * 60)
            logger.info("✓ 消息处理流程完成")

        _process_total_time = time.time() - _process_start_time
        if _process_total_time > self.reply_timeout_warning_threshold:
            logger.warning(
                f"⚠️ 消息处理总耗时异常: {_process_total_time:.2f}秒"
                f"（超过{self.reply_timeout_warning_threshold}秒阈值）"
            )

        logger.debug("消息处理完成,已发送回复并保存历史")

        # 回复后戳一戳功能
        if self.poke_after_reply_enabled:
            replied_user_id = event.get_sender_id()
            await self._do_poke_after_reply(event, replied_user_id, is_private, chat_id)

    async def _refresh_history_after_wait(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        current_history: list,
        max_context: int,
    ) -> list | None:
        """Refresh sender-aware history after concurrent processing wait.

        Args:
            event: Current message event.
            chat_id: Current group or private-chat identifier.
            current_history: History used before the wait.
            max_context: Maximum number of history messages to retain.

        Returns:
            A refreshed history list when new messages were persisted; otherwise None.
        """
        state_chat_id = ProbabilityManager.get_chat_key(
            event.get_platform_name(), event.is_private_chat(), chat_id
        )
        try:
            try:
                max_context = int(max_context)
            except (TypeError, ValueError):
                max_context = -1

            if max_context == 0:
                return None

            refreshed_msgs = await ContextManager.get_history_messages_with_fallback(
                event=event,
                max_messages=max_context,
                context=self.context,
                cached_messages=[],
            )
            if not refreshed_msgs:
                return None
            refreshed_msgs, _, _ = self.cache_manager.merge_cache_to_history(
                chat_id=state_chat_id,
                history_messages=refreshed_msgs,
                event=event,
                current_message_id=None,
            )

            if (
                isinstance(max_context, int)
                and max_context > 0
                and len(refreshed_msgs) > max_context
            ):
                refreshed_msgs = refreshed_msgs[-max_context:]

            if len(refreshed_msgs) <= len(current_history):
                return None

            return refreshed_msgs

        except Exception as e:
            logger.warning(f"[并发刷新] 获取最新历史失败: {e}")
            return None

    # ============================================================
    # 黑名单与启用检查
    # ============================================================

    def _is_enabled(self, event: AstrMessageEvent) -> bool:
        """
        Check whether the current conversation is enabled.

        Args:
            event: Incoming message event.

        Returns:
            True when the event belongs to an enabled group or private user.
        """
        if event.is_private_chat():
            if not self.enable_private_chat:
                return False
            enabled_private_users = self.enabled_private_users
            if not enabled_private_users:
                return True
            sender_id = str(event.get_sender_id())
            return any(str(user_id) == sender_id for user_id in enabled_private_users)

        enabled_groups = self.enabled_groups

        if not enabled_groups or len(enabled_groups) == 0:
            return True

        group_id = event.get_group_id()
        if group_id in enabled_groups:
            return True
        else:
            if self.debug_mode:
                logger.info(f"群组 {group_id} 未在启用列表中")
            return False

    def _is_user_blacklisted(self, event: AstrMessageEvent) -> bool:
        """检测发送者是否在用户黑名单中。"""
        try:
            if not self.enable_user_blacklist:
                return False

            blacklist = self.blacklist_user_ids
            if not blacklist:
                return False

            sender_id = event.get_sender_id()
            sender_id_str = str(sender_id)

            is_blacklisted = (
                sender_id in blacklist
                or sender_id_str in blacklist
                or (
                    int(sender_id_str) in blacklist
                    if sender_id_str.isdigit()
                    else False
                )
            )

            if is_blacklisted:
                if self.debug_mode:
                    logger.info(
                        f"🚫 [用户黑名单] 用户 {sender_id} 在黑名单中，本插件跳过处理该消息"
                    )
                return True

            return False

        except Exception as e:
            logger.error(f"[用户黑名单检测] 发生错误: {e}", exc_info=True)
            return False

    # ============================================================
    # LLM 请求钩子（恢复插件内容，保留第三方注入）
    # ============================================================

    @filter.on_llm_request(priority=-1)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        LLM 请求钩子（priority=-1，最后执行）

        当检测到请求来自本插件（PLUGIN_REQUEST_MARKER）时：
        1. 把 req.prompt 从短消息（向量检索用）换回完整上下文（full_prompt）
        2. 恢复插件 contexts（[]），保留其他插件注入的 contexts
        3. 恢复插件图片/音频URL（合并第三方注入）
        4. 合并插件工具集到 req.func_tool（保留框架内置工具）
        5. 不再注入任何插件行为指令/情绪/工具提醒文本；工具集仍按请求合并
        """
        from .utils.reply_handler import (
            PLUGIN_CURRENT_MESSAGE,
            PLUGIN_CUSTOM_CONTEXTS,
            PLUGIN_CUSTOM_PROMPT,
            PLUGIN_CUSTOM_SYSTEM_PROMPT,
            PLUGIN_FUNC_TOOL,
            PLUGIN_IMAGE_URLS,
            PLUGIN_REQUEST_MARKER,
        )

        # 检查是否是来自本插件的请求
        is_plugin_request = event.get_extra(PLUGIN_REQUEST_MARKER, False)
        if not is_plugin_request:
            return

        try:
            plugin_contexts = event.get_extra(PLUGIN_CUSTOM_CONTEXTS, [])
            plugin_prompt = event.get_extra(PLUGIN_CUSTOM_PROMPT, "")
            plugin_image_urls = event.get_extra(PLUGIN_IMAGE_URLS, [])
            plugin_short_prompt = event.get_extra(PLUGIN_CURRENT_MESSAGE, "") or ""
            plugin_audio_urls = event.get_extra("_plugin_audio_urls", []) or []

            if self.debug_mode:
                logger.info(
                    "🔧 [on_llm_request] 检测到本插件的 LLM 请求，开始恢复内容..."
                )

            # 1. 恢复 prompt：保留框架前缀（如 prompt_prefix）与第三方后缀注入
            #    用短消息在快照中定位，前后部分原样保留
            if plugin_prompt:
                current = req.prompt or ""
                if plugin_short_prompt and plugin_short_prompt in current:
                    prefix, _, suffix = current.partition(plugin_short_prompt)
                    req.prompt = prefix + plugin_prompt + suffix
                else:
                    req.prompt = plugin_prompt

            # 2. 恢复 contexts：插件用 []，保留第三方注入的 contexts
            extra_contexts = []
            if isinstance(req.contexts, list) and req.contexts:
                if plugin_contexts:
                    extra_contexts = [
                        c for c in req.contexts if c not in plugin_contexts
                    ]
                else:
                    extra_contexts = list(req.contexts)
            req.contexts = list(plugin_contexts or []) + extra_contexts

            # 3. system_prompt 保持现状：人格已在请求时传入，
            #    框架（TOOL_CALL_PROMPT 等）与第三方插件只会追加，不会覆盖

            # 4. 图片/音频 URL 合并（保留第三方注入）
            # QQ 商城表情预览图为死链，绝不能进入最终视觉请求；
            # 否则核心在组装 payload 时下载404，会把异常文本直接当回复发出。
            _merged_image_urls = [
                _u
                for _u in list(plugin_image_urls or [])
                if not EmojiDetector.looks_like_market_face([_u])
            ]
            _seen_urls = set(_merged_image_urls)
            for _u in req.image_urls or []:
                if _u in _seen_urls:
                    continue
                if EmojiDetector.looks_like_market_face([_u]):
                    continue
                _seen_urls.add(_u)
                _merged_image_urls.append(_u)
            req.image_urls = _merged_image_urls

            _merged_audio_urls = list(plugin_audio_urls or [])
            _seen_audios = set(_merged_audio_urls)
            for _u in req.audio_urls or []:
                if _u not in _seen_audios:
                    _seen_audios.add(_u)
                    _merged_audio_urls.append(_u)
            if _merged_audio_urls:
                req.audio_urls = _merged_audio_urls

            # 5. 合并插件工具集与框架内置工具，而非直接替换
            plugin_tool_set = event.get_extra(PLUGIN_FUNC_TOOL)
            if plugin_tool_set is not None:
                try:
                    plugin_tools = getattr(plugin_tool_set, "tools", None)
                    if plugin_tools is None:
                        plugin_tools = getattr(plugin_tool_set, "func_list", None)
                    plugin_tools = list(plugin_tools or [])
                except Exception:
                    plugin_tools = []
                if req.func_tool is None:
                    req.func_tool = plugin_tool_set
                elif hasattr(req.func_tool, "merge") and hasattr(
                    plugin_tool_set, "tools"
                ):
                    req.func_tool.merge(plugin_tool_set)
                elif hasattr(req.func_tool, "add_tool"):
                    for tool in plugin_tools:
                        req.func_tool.add_tool(tool)
                elif hasattr(req.func_tool, "remove_func") and hasattr(
                    req.func_tool, "func_list"
                ):
                    for tool in plugin_tools:
                        req.func_tool.remove_func(tool.name)
                        req.func_tool.func_list.append(tool)
                else:
                    req.func_tool = plugin_tool_set

            # Skills are intentionally not appended to every persona reply.
            # The request keeps its tool set, while unrelated skill instructions stay out
            # of the persona system prompt.

            if self.debug_mode:
                logger.info("  ✅ 已恢复插件自定义上下文:")
                logger.info(f"    - contexts 数量: {len(req.contexts)}")
                logger.info(f"    - system_prompt 长度: {len(req.system_prompt or '')}")
                logger.info(f"    - prompt 长度: {len(req.prompt or '')}")
                logger.info(
                    f"    - image_urls 数量: {len(req.image_urls) if req.image_urls else 0}"
                )
        except Exception as e:
            logger.error(f"[on_llm_request] 恢复插件请求内容失败: {e}", exc_info=True)
        finally:
            # 处理完成后立即清理event.extra字段，防止event对象污染
            try:
                event.set_extra(PLUGIN_REQUEST_MARKER, None)
                event.set_extra(PLUGIN_CUSTOM_CONTEXTS, None)
                event.set_extra(PLUGIN_CUSTOM_SYSTEM_PROMPT, None)
                event.set_extra(PLUGIN_CUSTOM_PROMPT, None)
                event.set_extra(PLUGIN_IMAGE_URLS, None)
                event.set_extra("_plugin_audio_urls", None)
                event.set_extra("_plugin_media_audio_urls", None)
                event.set_extra(PLUGIN_FUNC_TOOL, None)
                event.set_extra(PLUGIN_CURRENT_MESSAGE, None)
                logger.info("[安全] 已清理LLM请求上下文缓存")
            except Exception as e:
                logger.warning(f"⚠️ 清理event.extra字段时发生错误: {e}")

    # ============================================================
    # LLM 响应 / 结果装饰 / 消息发送后
    # ============================================================

    @filter.on_llm_response(priority=-1)
    async def on_llm_response(self, event: AstrMessageEvent, response):
        """
        agent完成信号：当agent真正完成时（所有工具调用结束），
        设置完成标志，告知 after_message_sent 可以最终保存所有累积的回复。
        """
        try:
            message_id = self._get_processing_id(event)

            async with self.concurrent_lock:
                if message_id not in self.processing_sessions:
                    return

            self._agent_done_flags.add(message_id)

            if self.debug_mode:
                pending_count = len(self._pending_bot_replies.get(message_id, []))
                logger.info(
                    f"[on_llm_response] agent已完成，message_id={message_id[:30]}...，"
                    f"已累积 {pending_count} 段回复文本"
                )

            # 边界情况：agent完成但最终response没有文本，而之前有累积的中间文本
            has_final_text = bool(
                response
                and (
                    getattr(response, "completion_text", None)
                    or getattr(response, "result_chain", None)
                )
            )
            pending_texts = self._pending_bot_replies.get(message_id, [])

            if not has_final_text and pending_texts:
                logger.info(
                    f"[on_llm_response] agent完成但无最终文本，保存 {len(pending_texts)} 段累积文本"
                )
                await self._finalize_bot_reply_save(event, message_id)

        except Exception as e:
            logger.error(f"[on_llm_response] 处理失败: {e}", exc_info=True)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """
        在最终结果装饰阶段进行处理：
        - 仅处理由本插件标记的消息（processing_sessions）
        - 应用输出内容过滤（去除敏感词等）
        - 检查重复消息（若与最近回复重复，清空结果以跳过发送）
        - 记录睡眠边界或需要后续语义复核的私聊软提示
        """
        try:
            is_private = event.is_private_chat()
            chat_id = event.get_group_id() if not is_private else event.get_sender_id()
            state_chat_id = ProbabilityManager.get_chat_key(
                event.get_platform_name(), is_private, chat_id
            )

            message_id = self._get_processing_id(event)

            async with self.concurrent_lock:
                if message_id not in self.processing_sessions:
                    return

            result = event.get_result()
            if not result or not hasattr(result, "chain") or not result.chain:
                return

            if not result.is_llm_result():
                return

            reply_text = "".join(
                self._coerce_component_text(getattr(comp, "text", None))
                for comp in result.chain
            ).strip()
            if not reply_text:
                return

            if self.collapse_reply_newlines and "\n" in reply_text:
                has_structured_format = bool(
                    re.search(
                        r"\x60{3}|^\s*(?:[-*+]|\d+[.)])\s",
                        reply_text,
                        re.MULTILINE,
                    )
                )
                text_components = [
                    comp for comp in result.chain if hasattr(comp, "text")
                ]
                if (
                    not has_structured_format
                    and text_components
                    and len(text_components) == len(result.chain)
                ):
                    normalized_reply_text = re.sub(
                        r"[ \t]*\r?\n[ \t]*", " ", reply_text
                    ).strip()
                    if normalized_reply_text != reply_text:
                        text_components[0].text = normalized_reply_text
                        for comp in text_components[1:]:
                            comp.text = ""
                        reply_text = normalized_reply_text

            # Remove the mechanical "current phrase + particle" opener before
            # caching or sending so the same echo cannot re-enter conversation history.
            filtered_reply_text = reply_text
            if (
                not is_private
                and result.chain
                and all(hasattr(comp, "text") for comp in result.chain)
            ):
                filtered_reply_text = ReplyHandler.remove_echo_prefix(
                    reply_text, event.get_message_str() or ""
                )

            if filtered_reply_text != reply_text:
                logger.info(
                    f"[Output filter] Removed echo opener: {len(reply_text)} -> "
                    f"{len(filtered_reply_text)} characters"
                )
                text_components = [
                    comp for comp in result.chain if hasattr(comp, "text")
                ]
                text_components[0].text = filtered_reply_text
                for comp in text_components[1:]:
                    comp.text = ""
                reply_text = filtered_reply_text

            if not reply_text:
                if self.debug_mode:
                    logger.info("[输出过滤] 过滤后内容为空，跳过发送")
                event.clear_result()
                return

            self.raw_reply_cache[message_id] = reply_text

            # 多轮工具调用支持：累积已过滤的回复文本
            if message_id not in self._pending_bot_replies:
                self._pending_bot_replies[message_id] = []
            self._pending_bot_replies[message_id].append(reply_text)

            # 重复检测必须在任何装饰性修改之前，基于原始内容检测
            if self.enable_duplicate_filter:
                now_ts = time.time()
                if state_chat_id not in self.recent_replies_cache:
                    self.recent_replies_cache[state_chat_id] = []

                if self.enable_duplicate_time_limit:
                    time_limit = max(60, self.duplicate_filter_time_limit)
                    self.recent_replies_cache[state_chat_id] = [
                        r
                        for r in self.recent_replies_cache[state_chat_id]
                        if now_ts - r.get("timestamp", 0) < time_limit
                    ]

                check_count = max(1, self.duplicate_filter_check_count)
                for recent in self.recent_replies_cache[state_chat_id][-check_count:]:
                    recent_content = recent.get("content", "")
                    recent_timestamp = recent.get("timestamp", 0)
                    if self.enable_duplicate_time_limit:
                        time_limit = max(60, self.duplicate_filter_time_limit)
                        if now_ts - recent_timestamp >= time_limit:
                            continue
                    if recent_content and reply_text == recent_content.strip():
                        logger.warning(
                            "🚫 [装饰阶段过滤] 检测到与最近回复重复，跳过发送（后续流程继续执行）"
                        )
                        event.clear_result()
                        self._duplicate_blocked_messages[message_id] = True
                        if message_id in self.raw_reply_cache:
                            del self.raw_reply_cache[message_id]
                        if self.debug_mode:
                            logger.info(
                                f"[装饰阶段] 已标记消息为重复拦截: {message_id[:30]}...（将跳过AI消息保存，但保存用户消息）"
                            )

                        # 重复拦截后 after_message_sent 不会被框架调用，
                        # 在此处直接保存用户消息和缓存消息到官方对话系统
                        try:
                            await self._save_user_messages_on_duplicate_block(
                                event, message_id
                            )
                        except Exception as save_err:
                            logger.warning(
                                f"[装饰阶段] 重复拦截后保存用户消息失败: {save_err}"
                            )
                        return

            # 通过重复检测后立即写入缓存（修复并发竞态）
            if self.enable_duplicate_filter and reply_text:
                try:
                    if state_chat_id not in self.recent_replies_cache:
                        self.recent_replies_cache[state_chat_id] = []
                    self.recent_replies_cache[state_chat_id].append(
                        {"content": reply_text, "timestamp": time.time()}
                    )
                    max_cache_size = min(
                        max(10, self.duplicate_filter_check_count * 2),
                        self._DUPLICATE_CACHE_SIZE_LIMIT,
                    )
                    if len(self.recent_replies_cache[state_chat_id]) > max_cache_size:
                        self.recent_replies_cache[state_chat_id] = (
                            self.recent_replies_cache[state_chat_id][-max_cache_size:]
                        )
                except Exception:
                    pass

            late_night_candidate = event.get_extra(
                "_persona_late_night_session_candidate", ""
            )
            late_night_active_until = self._private_late_night_active_until.get(
                state_chat_id, 0.0
            )
            if is_private and (
                late_night_candidate == state_chat_id
                or late_night_active_until > time.time()
            ):
                self._private_late_night_active_until[
                    state_chat_id
                ] = time.time() + self._PRIVATE_LATE_NIGHT_ACTIVE_SECONDS
                event.set_extra("_persona_late_night_session_candidate", "")
                logger.info(
                    "[Private late-night] Reply succeeded; awake session extended "
                    f"for {self._PRIVATE_LATE_NIGHT_ACTIVE_SECONDS // 60} minutes"
                )

            if is_private:
                active_boundary = self._private_conversation_state.get(state_chat_id)
                if active_boundary is not None:
                    # Commit the accepted boundary transition only after the send hook.
                    event.set_extra(
                        "_persona_private_boundary_reply",
                        {
                            "chat_key": state_chat_id,
                            "boundary": active_boundary,
                        },
                    )
                else:
                    boundary = self._private_conversation_state.record_reply(
                        state_chat_id, reply_text
                    )
                    if boundary:
                        self._private_late_night_active_until.pop(state_chat_id, None)
                        self._schedule_private_wake(event, state_chat_id, boundary)
                        boundary_label = (
                            "dismissive_review_hint"
                            if boundary.kind == "dismissive"
                            else boundary.kind
                        )
                        logger.info(
                            "[Private boundary] recorded state=%s mode=%s "
                            "reopen_in=%ss wake_in=%ss expires_in=%ss",
                            boundary_label,
                            getattr(boundary, "sleep_mode", "none"),
                            boundary.seconds_until_reopen(),
                            boundary.seconds_until_wake(),
                            boundary.seconds_until_expiry(),
                        )

        except Exception as e:
            logger.error(f"[装饰阶段] 去重处理失败: {e}", exc_info=True)

    # ============================================================
    # Smart 并发批次提示
    # ============================================================

    def _summarize_smart_batch_messages(
        self,
        smart_batch_messages: list,
        anchor_sender_id: str,
        anchor_content: str = "",
    ) -> dict:
        """Summarize appended messages for the reply-stage batch hint.

        Args:
            smart_batch_messages: Messages appended after the anchor.
            anchor_sender_id: Sender ID of the anchor message.
            anchor_content: Text content of the anchor message.

        Returns:
            A summary containing sender counts, message labels, and exact-text
            duplicate groups.
        """
        summary = {
            "total_messages": 0,
            "other_sender_count": 0,
            "same_sender_count": 0,
            "has_other_senders": False,
            "has_same_sender_followups": False,
            "senders": [],
            "summary_lines": [],
            "duplicate_text_groups": [],
        }
        if not smart_batch_messages:
            return summary

        sender_map = OrderedDict()
        anchor_sender_id = str(anchor_sender_id) if anchor_sender_id is not None else ""

        for msg in smart_batch_messages:
            if not isinstance(msg, dict):
                continue
            sender_id = str(msg.get("sender_id") or "unknown")
            sender_name = msg.get("sender_name") or "未知用户"
            content = ContextManager._content_to_safe_text(
                msg.get("content", "")
            ).strip()
            if not content:
                content = "（无文本内容）"
            content = content.replace("\n", " ")
            is_same_sender = sender_id == anchor_sender_id and anchor_sender_id != ""

            item = sender_map.setdefault(
                sender_id,
                {
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "count": 0,
                    "latest_content": "",
                    "is_same_sender": is_same_sender,
                    "has_at": False,
                    "has_keyword": False,
                    "has_poke": False,
                },
            )
            item["count"] += 1
            item["latest_content"] = content[:120]
            item["has_at"] = item["has_at"] or bool(msg.get("is_at_message"))
            item["has_keyword"] = item["has_keyword"] or bool(
                msg.get("has_trigger_keyword")
            )
            item["has_poke"] = item["has_poke"] or bool(msg.get("poke_info"))

        senders = list(sender_map.values())
        summary["senders"] = senders
        summary["total_messages"] = sum(item["count"] for item in senders)
        summary["same_sender_count"] = sum(
            item["count"] for item in senders if item.get("is_same_sender")
        )
        summary["other_sender_count"] = sum(
            item["count"] for item in senders if not item.get("is_same_sender")
        )
        summary["has_other_senders"] = any(
            not item.get("is_same_sender") for item in senders
        )
        summary["has_same_sender_followups"] = summary["same_sender_count"] > 0

        duplicate_counts = OrderedDict()
        normalized_anchor = ContextManager._content_to_safe_text(anchor_content).strip()
        if normalized_anchor and anchor_sender_id:
            duplicate_counts[normalized_anchor] = 1
        for msg in smart_batch_messages:
            sender_id = str(msg.get("sender_id") or "unknown")
            if sender_id != anchor_sender_id:
                continue
            content = ContextManager._content_to_safe_text(
                msg.get("content", "")
            ).strip()
            if content:
                duplicate_counts[content] = duplicate_counts.get(content, 0) + 1
        summary["duplicate_text_groups"] = [
            {"content": content[:120], "count": count}
            for content, count in duplicate_counts.items()
            if count > 1
        ]

        summary_lines = []
        for item in senders:
            flags = []
            if item.get("is_same_sender"):
                flags.append("当前对象的追加消息")
            else:
                flags.append("其他用户插话")
            if item.get("has_at"):
                flags.append("@触发")
            if item.get("has_keyword"):
                flags.append("关键词触发")
            if item.get("has_poke"):
                flags.append("戳一戳相关")
            flag_text = f"（{'、'.join(flags)}）" if flags else ""
            count_text = f"{item['count']}条"
            summary_lines.append(
                f"- {item['sender_name']}(ID:{item['sender_id']}) {count_text}{flag_text}：{item['latest_content']}"
            )
        summary["summary_lines"] = summary_lines
        return summary

    def _build_smart_batch_reply_hint(
        self, event: AstrMessageEvent, smart_batch_summary: dict
    ) -> str:
        """Build a Smart batch hint for the formal reply.

        Args:
            event: Anchor message event for the batch.
            smart_batch_summary: Summary produced for appended messages.

        Returns:
            A prompt fragment describing the batch and any repeated content.
        """
        if not smart_batch_summary or not smart_batch_summary.get("summary_lines"):
            return ""

        sender_name = event.get_sender_name() or "当前对话对象"
        sender_id = event.get_sender_id()
        total_messages = smart_batch_summary.get("total_messages", 0)
        has_other_senders = smart_batch_summary.get("has_other_senders", False)
        has_same_sender_followups = smart_batch_summary.get(
            "has_same_sender_followups", False
        )

        scenario_parts = []
        if has_same_sender_followups:
            scenario_parts.append("同一位私聊对象连续补发了后续消息")
        if has_other_senders:
            scenario_parts.append("期间还有其他用户插话")
        if not scenario_parts:
            scenario_parts.append("当前消息后面还有紧接着的追加消息")
        scenario_text = "，".join(scenario_parts)

        summary_block = "\n".join(smart_batch_summary.get("summary_lines", []))
        duplicate_groups = smart_batch_summary.get("duplicate_text_groups", [])
        duplicate_block = ""
        if duplicate_groups:
            duplicate_lines = "\n".join(
                f"- 同一内容重复发送 {group['count']} 次：{group['content']}"
                for group in duplicate_groups
            )
            duplicate_block = (
                "重复消息提示：同一位用户在短时间内重复发送了相同内容。"
                "这不是多个独立问题，请只回复一次；请按当前 Persona 对重复和啰嗦的态度自然回应。\n"
                f"{duplicate_lines}\n"
            )
        if event.is_private_chat():
            return (
                "\n\n[系统提示-Smart私聊批次]\n"
                f"这是 {sender_name}(ID:{sender_id}) 在短时间内连续发送的一组消息，{scenario_text}。\n"
                f"追加消息摘要：\n{summary_block}\n"
                f"{duplicate_block}"
                "请把当前消息和追加消息视为同一轮输入，合并理解后只生成一条完整回复；"
                "可以选择性回应其中需要回应的内容，但不要逐条回复，也不要解释或分析批处理过程。\n"
            )
        return (
            "\n\n[系统提示-Smart并发]\n"
            f"你这次面对的是一个 Smart 并发批次：在 {sender_name}(ID:{sender_id}) 这条当前消息之后，"
            f"又紧接着出现了 {total_messages} 条追加消息。{scenario_text}。\n"
            "这些追加消息已按发送者名字和ID标出，帮你理解完整对话背景：\n"
            f"{summary_block}\n"
            f"{duplicate_block}"
            f"你只需回复 {sender_name}(ID:{sender_id}) 的当前消息。追加消息是背景参考，"
            f"直接自然说话即可，不要逐条回复、不要进行任何判断或分析。\n"
        )

    # ============================================================
    # 图片描述缓存辅助（省钱）
    # ============================================================

    # ============================================================
    # 通用辅助方法
    # ============================================================

    @staticmethod
    def _coerce_component_text(value: Any) -> str:
        """将消息组件文本强制转换为字符串。"""
        try:
            if value is None:
                return ""
            if isinstance(value, str):
                return value
            if isinstance(value, (list, tuple)):
                parts = []
                for item in value:
                    text = getattr(item, "text", None)
                    if text is not None:
                        parts.append(str(text))
                    elif isinstance(item, str):
                        parts.append(item)
                return "".join(parts)
            return str(value)
        except Exception:
            return ""

    def _build_source_event_id(self, event: AstrMessageEvent) -> str:
        """构建平台重复推送识别ID；尽量稳定，但不误伤用户主动重复发言。"""
        try:
            cached = getattr(event, "_plugin_source_event_id", None)
            if cached:
                return cached

            result_id = ""
            if hasattr(event, "message_obj") and hasattr(
                event.message_obj, "message_id"
            ):
                platform_msg_id = str(event.message_obj.message_id or "").strip()
                if platform_msg_id:
                    result_id = f"{event.get_platform_name()}_{platform_msg_id}"

            if not result_id:
                msg_ts = None
                if hasattr(event, "message_obj") and hasattr(
                    event.message_obj, "timestamp"
                ):
                    msg_ts = getattr(event.message_obj, "timestamp", None)
                sender_id = event.get_sender_id() or ""
                group_id = (
                    event.get_group_id() if not event.is_private_chat() else "private"
                )
                content_outline = (
                    event.get_message_outline() or event.get_message_str() or ""
                )
                content_outline = content_outline[:160]
                hash_input = (
                    f"{event.get_platform_name()}|{sender_id}|{group_id}|{msg_ts}|{content_outline}"
                ).encode("utf-8", errors="ignore")
                result_id = (
                    f"fallback_source_{hashlib.md5(hash_input).hexdigest()[:20]}"
                )

            try:
                event._plugin_source_event_id = result_id
            except AttributeError:
                pass
            return result_id
        except Exception as e:
            return (
                f"fallback_source_error_{hashlib.md5(str(e).encode()).hexdigest()[:12]}"
            )

    def _get_processing_id(self, event: AstrMessageEvent) -> str:
        """获取插件内部处理实例ID；与平台重复推送识别ID解耦。"""
        try:
            cached = getattr(event, "_plugin_processing_id", None)
            if cached:
                return cached

            source_event_id = self._build_source_event_id(event)
            result_id = f"proc_{source_event_id}_{id(event)}"
            try:
                event._plugin_processing_id = result_id
            except AttributeError:
                pass
            return result_id
        except Exception as e:
            return f"proc_fallback_{int(time.time() * 1000)}_{hashlib.md5(str(e).encode()).hexdigest()[:8]}"

    def _ensure_arrival_metadata(self, event: AstrMessageEvent) -> tuple:
        """为当前 event 分配稳定的到达序号与单调时间。"""
        try:
            arrival_seq = getattr(event, "_plugin_arrival_seq", None)
            arrival_monotonic = getattr(event, "_plugin_arrival_monotonic", None)
            if arrival_seq and arrival_monotonic:
                return arrival_seq, arrival_monotonic

            self._arrival_seq_counter += 1
            arrival_seq = self._arrival_seq_counter
            arrival_monotonic = time.monotonic()

            try:
                event._plugin_arrival_seq = arrival_seq
                event._plugin_arrival_monotonic = arrival_monotonic
            except AttributeError:
                pass
            return arrival_seq, arrival_monotonic
        except Exception:
            return 0, time.monotonic()
