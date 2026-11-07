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
版本: 1.9.8（Persona Presence 参与判断重构）
"""

import asyncio
import copy
import hashlib
import json
import math
import os
import random
import re
import time
import types
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import filter
from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.provider.entities import ProviderRequest

# 导入保留的工具模块
from .utils import (
    EMOJI_MARKER,
    AIResponseFilter,
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
from .utils.cross_group_context import CrossGroupSight
from .utils.image_description_cache import ImageDescriptionCache
from .utils.life_sight import LifeSight
from .utils.jargon import (
    build_extract_prompt,
    build_meaning_prompts,
    candidate_terms,
    parse_json_block,
    search_web as search_jargon_web,
)
from .utils.meme_bridge import select_meme_image
from .utils.message_cache_manager import MessageCacheManager
from .utils.modules.base import FeatureContext, Proposal
from .utils.modules.curiosity import CuriosityModule
from .utils.modules.curiosity_store import CuriosityStore
from .utils.modules.image_archive import (
    ImageArchive,
    send_block_reason,
)
from .utils.modules.notebook import Notebook
from .utils.modules.proactive import ProactiveModule, ProactiveSettings
from .utils.participation import ParticipationDecision, ParticipationThrottle
from .utils.private_conversation_state import PrivateConversationState

PLUGIN_VERSION = "1.9.8"


@register(
    "astrbot_plugin_persona_presence",
    "Sihnbaobao",
    "让当前 Persona 按兴趣、关系和当下意愿选择是否参与对话的增强插件",
    PLUGIN_VERSION,
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
        self.reply_ellipsis_style = self._cfg_choice(
            "reply_ellipsis_style", "keep", {"keep", "ascii", "chinese"}
        )

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
        self.image_temp_retention_hours = self._cfg_int(
            "image_temp_retention_hours", 24, minimum=0, maximum=720
        )
        self.enable_image_archive = self._cfg_bool("enable_image_archive", True)
        self.image_archive_scope = self._cfg_choice(
            "image_archive_scope", "replied", {"replied", "all"}
        )
        self.image_archive_min_kb = self._cfg_int(
            "image_archive_min_kb", 8, minimum=0, maximum=1024
        )
        self.image_archive_retry_minutes = self._cfg_int(
            "image_archive_retry_minutes", 10, minimum=1, maximum=1440
        )
        self.image_archive_max_analysis_retries = self._cfg_int(
            "image_archive_max_analysis_retries", 1, minimum=0, maximum=10
        )
        self.image_context_window_minutes = self._cfg_int(
            "image_context_window_minutes", 5, minimum=1, maximum=120
        )
        self.image_context_max_images = self._cfg_int(
            "image_context_max_images", 3, minimum=1, maximum=10
        )
        self.image_archive_max_mb = self._cfg_int(
            "image_archive_max_mb", 500, minimum=50, maximum=5000
        )
        self.image_archive_ordinary_days = self._cfg_int(
            "image_archive_ordinary_days", 7, minimum=1, maximum=365
        )
        self.image_archive_review_days = self._cfg_int(
            "image_archive_review_days", 30, minimum=1, maximum=365
        )
        self.image_archive_retrieval_days = self._cfg_int(
            "image_archive_retrieval_days", 30, minimum=1, maximum=365
        )
        self.image_archive_min_score = self._cfg_float(
            "image_archive_min_score", 0.6, minimum=0.0, maximum=1.0
        )
        self.archive_sensitive_send_enabled = self._cfg_bool(
            "archive_sensitive_send_enabled", False
        )
        self.archive_sensitive_allowlist = self._cfg_list(
            "archive_sensitive_allowlist", []
        )
        self.enable_image_description_cache = self._cfg_bool(
            "enable_image_description_cache", False
        )
        self.image_description_cache_max_entries = self._cfg_int(
            "image_description_cache_max_entries", 500, minimum=10, maximum=10_000
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
        self.enable_repeat_guard = self._cfg_bool("enable_repeat_guard", True)
        self.repeat_guard_count = self._cfg_int(
            "repeat_guard_count", 5, minimum=1, maximum=10
        )
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
            "private_batch_wait_ms", 3500, minimum=0, maximum=600_000
        )
        self.private_batch_quiet_ms = self._cfg_int(
            "private_batch_quiet_ms", 1200, minimum=100, maximum=60_000
        )
        self.private_batch_max_size = self._cfg_int(
            "private_batch_max_size", 10, minimum=1, maximum=1000
        )

        # ========== 私聊生成方式（预生成 / 多气泡） ==========
        self.private_generation_mode = self._cfg_choice(
            "private_generation_mode", "precomputed", {"precomputed", "followup"}
        )
        self.enable_followup_reply = self.private_generation_mode == "followup"
        self.enable_private_speculative_reply = (
            self.private_generation_mode == "precomputed"
        )
        self.followup_scope = self._cfg_choice(
            "followup_scope", "private", {"private", "group", "both"}
        )
        self.followup_max_messages = self._cfg_int(
            "followup_max_messages", 1, minimum=1, maximum=2
        )
        self.followup_min_delay_ms = self._cfg_int(
            "followup_min_delay_ms", 700, minimum=0, maximum=10_000
        )
        self.followup_max_delay_ms = self._cfg_int(
            "followup_max_delay_ms", 2200, minimum=0, maximum=10_000
        )

        # ========== 表情桥接（可选，依赖 meme_manager） ==========
        self.enable_meme_bridge = self._cfg_bool("enable_meme_bridge", False)
        self.meme_bridge_scope = self._cfg_choice(
            "meme_bridge_scope", "private", {"private", "group", "both"}
        )
        self.meme_bridge_min_score = self._cfg_float(
            "meme_bridge_min_score", 0.25, minimum=0.0, maximum=1.0
        )
        self.meme_bridge_max_per_turn = self._cfg_int(
            "meme_bridge_max_per_turn", 1, minimum=1, maximum=2
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
            data_dir = Path(self.context.get_data_dir()) / "persona_presence"
            legacy_data_dir = Path(self.context.get_data_dir()) / "group_chat_plus"
        except Exception:
            data_dir = Path.cwd() / "data" / "persona_presence"
            legacy_data_dir = Path.cwd() / "data" / "group_chat_plus"
        if not data_dir.exists() and legacy_data_dir.exists():
            # 旧目录沿用 Group Chat Plus 的名字，整体搬到新目录，见闻本/记忆本不丢。
            legacy_data_dir.rename(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.plugin_data_dir = str(data_dir)
        self._cleanup_old_temp_images()

        # ========== 后台任务容器 ==========
        # These must exist before _apply_cross_group_config() builds the DLCs,
        # because a DLC may start a background task during construction.
        self._feature_tasks: set[asyncio.Task] = set()
        self._curiosity_task: asyncio.Task | None = None
        self.image_archive: ImageArchive | None = None
        self._build_image_archive()

        # ========== 跨群见闻（Cross-group sight） ==========
        # 依赖上面的 plugin_data_dir，见闻缓冲会落盘以便重启后仍记得最近的事。
        self._apply_cross_group_config()

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
        # Recent private images, kept so a later text message can still
        # reference images that were sent as separate messages.
        self._recent_private_images: dict[str, list[tuple[float, str]]] = {}
        self._avatar_cache: dict[str, tuple[float, str]] = {}
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
        # Base-owned send timestamps used by feature-pack proposal gates.
        self._last_bot_reply_at: dict[str, float] = {}
        # Deduplicate repeated real-state notes per conversation.
        self._life_sight_last_note: dict[str, tuple[str, float]] = {}
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
        logger.info(f"Persona Presence 已加载 - {PLUGIN_VERSION}（人格自主参与）")
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
            # 其它插件可能在我们之后才注册工具，延迟一点再看。
            asyncio.create_task(self._log_tools_later())
        except Exception:
            if session_created:
                await self._close_http_session()
            raise

    async def _log_tools_later(self) -> None:
        """Log the tool set once other plugins finished registering."""
        await asyncio.sleep(15)
        self._log_available_tools()

    def _log_available_tools(self) -> None:
        """Log which LLM tools the reply path can actually use."""
        try:
            manager = self.context.get_llm_tool_manager()
            names = [tool.name for tool in getattr(manager, "func_list", []) or []]
            recall = [n for n in names if "bili" in n.lower()]
            logger.info(
                f"[工具] 可用 {len(names)} 个，B站相关 {len(recall)} 个: "
                + (", ".join(recall) if recall else "（无）")
            )
        except Exception as e:
            logger.warning(f"[工具] 读取失败: {e}")

    async def terminate(self) -> None:
        """Release runtime resources when the plugin is disabled or reloaded."""
        try:
            self.cross_group_sight.flush()
        except Exception as e:
            logger.warning(f"[跨群见闻] 落盘失败: {e}")

        proactive = getattr(self, "proactive_module", None)
        if proactive is not None:
            try:
                await proactive.close()
            except Exception as e:
                logger.warning(f"[主动发言] 落盘失败: {e}")

        await self._close_http_session()
        self._web_apis_registered = False

        wake_tasks = list(self._private_wake_tasks.values())
        for wake_task in wake_tasks:
            wake_task.cancel()
        if wake_tasks:
            await asyncio.gather(*wake_tasks, return_exceptions=True)
        self._private_wake_tasks.clear()

        if self._curiosity_task is not None:
            self._curiosity_task.cancel()
            self._curiosity_task = None
        feature_tasks = list(self._feature_tasks)
        for feature_task in feature_tasks:
            feature_task.cancel()
        if feature_tasks:
            await asyncio.gather(*feature_tasks, return_exceptions=True)
        self._feature_tasks.clear()
        self._last_bot_reply_at.clear()
        self._life_sight_last_note.clear()

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

    def _apply_cross_group_config(self) -> None:
        """Rebuild the cross-group sight layer from the current configuration."""
        self.enable_cross_group_sight = self._cfg_bool(
            "enable_cross_group_sight", False
        )
        self.enable_cross_group_memory_recall = self._cfg_bool(
            "enable_cross_group_memory_recall", False
        )
        self.cross_group_sight = CrossGroupSight(
            self.context,
            enabled=self.enable_cross_group_sight,
            enabled_groups=self.enabled_groups,
            buffer_size=self._cfg_int(
                "cross_group_sight_buffer_size", 200, minimum=10, maximum=2000
            ),
            window_seconds=self._cfg_int(
                "cross_group_sight_window_minutes",
                1440,
                minimum=1,
                maximum=10080,
            )
            * 60,
            store_path=(
                Path(self.plugin_data_dir) / "cross_group_sight.json"
                if getattr(self, "plugin_data_dir", None)
                else None
            ),
            max_groups=self._cfg_int(
                "cross_group_sight_max_groups", 3, minimum=1, maximum=6
            ),
            always_on=self._cfg_bool("cross_group_sight_always_on", True),
            baseline_per_group=self._cfg_int(
                "cross_group_sight_baseline_per_group", 2, minimum=1, maximum=10
            ),
            max_per_group=self._cfg_int(
                "cross_group_sight_max_per_group", 5, minimum=1, maximum=20
            ),
            denylist=self._cfg_list("cross_group_sight_denylist", []),
        )

        # ========== 真实状态（Life sight） ==========
        # 只读伴侣插件写下的真实记录，读不到就写"最近没什么动静"，不做编造。
        self._build_life_sight()

        self._apply_dlc_config()

    def _build_life_sight(self) -> None:
        """Build the read-only companion-state source, or disable it."""
        self.life_sight = None
        if not self._cfg_bool("enable_life_state", False):
            return
        configured = self._cfg_str("life_state_data_dir", "")
        if configured:
            data_dir = Path(configured).expanduser()
        else:
            try:
                data_dir = (
                    Path(self.context.get_data_dir())
                    / "plugin_data"
                    / "astrbot_plugin_bili_scroll"
                )
            except Exception:
                return
        if not data_dir.exists():
            logger.info(f"[真实状态] 目录不存在: {data_dir}")
            return
        watch_log = data_dir / "watch_log.json"
        try:
            self.life_sight = LifeSight(
                watch_log if watch_log.exists() else None,
                enabled=True,
                data_dir=data_dir,
            )
            logger.info(f"[真实状态] 已启用，读取: {data_dir}")
        except Exception as exc:
            logger.warning(f"[真实状态] 初始化失败: {exc}")

    def _apply_dlc_config(self) -> None:
        """（重新）按当前配置构建三个资料片对象。

        页面上的开关保存后会调用这里，保证内存里的对象和配置一致。
        """
        self._build_notebook()
        self._build_curiosity()
        self._build_proactive()

    def _cleanup_old_temp_images(self) -> None:
        """Remove stale images downloaded into AstrBot's temp directory.

        The plugin itself does not archive images; vision and platform upload
        may leave ``media_image_*`` files in the framework temp directory.
        """
        hours = int(getattr(self, "image_temp_retention_hours", 0) or 0)
        if hours <= 0:
            return
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

            root = Path(get_astrbot_temp_path())
            cutoff = time.time() - hours * 3600
            removed = 0
            for path in root.glob("media_image_*"):
                try:
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                except OSError:
                    continue
            if removed:
                logger.info(
                    f"[图片缓存] 清理了 {removed} 张超过 {hours} 小时的临时图片"
                )
        except Exception as exc:
            logger.debug(f"[图片缓存] 清理临时图片失败: {exc}")

    def _build_image_archive(self) -> None:
        """Create the optional, bounded image archive and review worker."""
        if not getattr(self, "enable_image_archive", False):
            return
        try:
            self.image_archive = ImageArchive(
                self.plugin_data_dir,
                enabled=True,
                max_mb=self.image_archive_max_mb,
                ordinary_days=self.image_archive_ordinary_days,
                review_days=self.image_archive_review_days,
                retrieval_days=self.image_archive_retrieval_days,
                min_score=self.image_archive_min_score,
            )
            stats = self.image_archive.stats()
            logger.info(
                f"[图片库] 已启用：{stats['count']} 张，"
                f"{stats['bytes'] // 1024 // 1024}MB / "
                f"{stats['max_bytes'] // 1024 // 1024}MB"
            )
            task = asyncio.create_task(self._image_archive_worker())
            self._feature_tasks.add(task)
        except Exception as exc:
            self.image_archive = None
            logger.warning(f"[图片库] 初始化失败: {exc}")

    async def _image_archive_worker(self) -> None:
        """Retry failed analyses, prune expired images and review favorites."""
        while True:
            await asyncio.sleep(max(60, self.image_archive_retry_minutes * 60))
            archive = getattr(self, "image_archive", None)
            if archive is None:
                return
            try:
                archive.prune()
            except Exception as exc:
                logger.debug(f"[图片库] 定期清理失败: {exc}")
                continue
            try:
                provider = await self.context.get_using_provider_async()
            except Exception:
                provider = None
            for entry in archive.pending_analysis(
                3,
                retry_seconds=self.image_archive_retry_minutes * 60,
                max_attempts=self.image_archive_max_analysis_retries,
            ):
                path = archive.root / entry.file
                if not path.is_file():
                    archive.remove(entry.id)
                    continue
                persona_prompt = ""
                try:
                    if entry.umo:
                        persona_prompt = await self._resolve_persona_prompt(entry.umo)
                except Exception:
                    pass
                rating = await self._rate_archive_image(
                    provider, str(path), persona_prompt
                )
                if str(rating.get("description") or "").strip():
                    archive.update_analysis(
                        entry.id,
                        description=str(rating.get("description") or ""),
                        tags=list(rating.get("tags") or []),
                        score=float(rating.get("score") or 0.0),
                        favorite=rating.get("favorite"),
                    )
                    logger.info(f"[图片库] 重试识图成功 {entry.id}")
                else:
                    error = str(rating.get("error") or "")
                    attempts = archive.mark_analysis_failed(entry.id, reason=error)
                    logger.warning(
                        f"[图片库] 重试识图失败 {entry.id}（第 {attempts} 次）"
                        + (f"：{error}" if error else "")
                    )
            for entry in archive.due_for_review(3):
                keep = await self._review_archive_entry(entry)
                if keep is not None:
                    archive.review(entry.id, keep)

    async def _review_archive_entry(self, entry) -> bool | None:
        """Ask the model whether an old favorite is still worth keeping."""
        try:
            provider = await self.context.get_using_provider_async()
            if provider is None:
                return None
            path = self.image_archive.root / entry.file
            if not path.is_file():
                self.image_archive.remove(entry.id)
                return None
            persona_prompt = ""
            try:
                if entry.umo:
                    persona_prompt = await self._resolve_persona_prompt(entry.umo)
            except Exception:
                pass
            persona_block = (
                f"<persona>\n{persona_prompt.strip()[:1800]}\n</persona>\n"
                if persona_prompt.strip()
                else ""
            )
            prompt = (
                f"{persona_block}"
                "这是你以前按自己的眼光保存的一张图片。\n"
                f"当时的描述：{entry.description or '（没有描述）'}\n"
                f"标签：{', '.join(entry.tags) or '（无）'}\n"
                "你现在还喜欢它、觉得值得继续留着吗？"
                "不要因为已经保存过就勉强保留。\n"
                '只输出 JSON：{"keep": true/false, "reason": "一句话"}'
            )
            response = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                image_urls=[str(path)],
                func_tool=None,
                system_prompt="",
                session_id="",
            )
            data = parse_json_block(str(getattr(response, "completion_text", "") or ""))
            if not isinstance(data, dict) or "keep" not in data:
                return None
            return bool(data.get("keep"))
        except Exception as exc:
            logger.debug(f"[图片库] 回看失败: {exc}")
            return None

    def _schedule_image_archive(
        self,
        event,
        image_urls: list,
        chat_id: str,
        is_private: bool,
    ) -> None:
        """Archive inbound images in the background without blocking replies."""
        archive = getattr(self, "image_archive", None)
        if archive is None or not image_urls:
            return
        # Always use the canonical archive key, not a raw private/group id.
        canonical = self._archive_chat_key(event)
        if canonical:
            chat_id = canonical
        try:
            task = asyncio.create_task(
                self._archive_message_images(
                    event, list(image_urls), chat_id, is_private
                )
            )
            self._feature_tasks.add(task)
        except Exception as exc:
            logger.debug(f"[图片库] 启动归档失败: {exc}")

    async def _archive_message_images(
        self, event, image_urls: list, chat_id: str, is_private: bool
    ) -> None:
        """Copy and rate the images attached to one incoming message."""
        archive = getattr(self, "image_archive", None)
        if archive is None:
            return
        try:
            provider = (
                self.context.get_provider_by_id(self.image_to_text_provider_id)
                if self.image_to_text_provider_id
                else await self.context.get_using_provider_async()
            )
        except Exception:
            provider = None
        persona_prompt = ""
        try:
            persona_prompt = await self._resolve_active_persona_prompt(event)
        except Exception:
            pass
        sender_id = ""
        sender_name = ""
        message_id = ""
        timestamp = time.time()
        try:
            sender_id = str(event.get_sender_id() or "")
            sender_name = str(event.get_sender_name() or "")
            message_id = self._get_processing_id(event)
            message_obj = getattr(event, "message_obj", None)
            timestamp = float(getattr(message_obj, "timestamp", 0) or time.time())
        except Exception:
            pass

        for ref in image_urls[: self.max_images_per_message]:
            ref = str(ref or "").strip()
            if not ref:
                continue
            # Do not re-archive a picture that already came from the archive.
            if archive.root in Path(ref).parents:
                continue
            local_path = None
            try:
                component = (
                    Image.fromURL(ref)
                    if ref.startswith(("http://", "https://"))
                    else Image(file=ref)
                )
                local_path = await component.convert_to_file_path()
                try:
                    size_kb = Path(local_path).stat().st_size // 1024
                except OSError:
                    size_kb = 0
                if self.image_archive_min_kb and size_kb < self.image_archive_min_kb:
                    logger.debug(
                        f"[图片库] 跳过 {size_kb}KB 小图（阈值 {self.image_archive_min_kb}KB）"
                    )
                    continue
                existing = archive.find_by_hash(local_path)
                if existing is not None:
                    bound = archive.bind_chat(existing, str(chat_id))
                    logger.info(
                        f"[图片库] 重复图片 {existing.id} 已存在"
                        + ("，绑定到当前会话" if bound else "，当前会话已绑定")
                    )
                    continue
                rating = await self._rate_archive_image(
                    provider, local_path, persona_prompt
                )
                entry = archive.ingest(
                    local_path,
                    chat_id=str(chat_id),
                    is_group=not is_private,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    message_id=message_id,
                    umo=str(getattr(event, "unified_msg_origin", "") or ""),
                    description=str(rating.get("description") or ""),
                    tags=list(rating.get("tags") or []),
                    score=float(rating.get("score") or 0.0),
                    favorite=rating.get("favorite"),
                    now=timestamp,
                )
                if entry and entry.analysis_pending:
                    error = str(rating.get("error") or "")
                    if error and not self._analysis_retryable(error):
                        archive.mark_analysis_failed(entry.id, reason=error)
                        logger.warning(
                            f"[图片库] 图片 {entry.id} 识图失败且不可重试：{error}"
                        )
                    else:
                        logger.warning(f"[图片库] 图片 {entry.id} 已入库，等待重试识图")
            except Exception as exc:
                logger.debug(f"[图片库] 归档图片失败: {exc}")
            finally:
                # The archive owns its copy; remove a transient download if it
                # is not the original inbound file.
                try:
                    if (
                        local_path
                        and local_path != ref
                        and str(local_path).startswith(
                            str(Path(self.plugin_data_dir).parent / "temp")
                        )
                    ):
                        Path(local_path).unlink(missing_ok=True)
                except Exception:
                    pass

    @staticmethod
    def _archive_chat_key(event) -> str:
        """Build the same conversation key used by archive ingest."""
        try:
            platform = str(event.get_platform_name() or "unknown")
            group_id = str(event.get_group_id() or "")
            if group_id:
                return f"{platform}_group_{group_id}"
            return f"{platform}_private_{event.get_sender_id()}"
        except Exception:
            return ""

    def _archive_context_images(
        self, chat_id: str, text: str, current_images: list
    ) -> list:
        """Add a recently saved image as visual context for a follow-up."""
        archive = getattr(self, "image_archive", None)
        if archive is None or current_images or not chat_id:
            return []
        cutoff = time.time() - self.image_context_window_minutes * 60
        try:
            entries = archive.search(
                chat_id,
                str(text or ""),
                days=1,
                limit=48,
            )
        except Exception as exc:
            logger.debug(f"[图片库] 补充上下文失败: {exc}")
            return []
        recent = sorted(
            (entry for entry in entries if entry.created_at >= cutoff),
            key=lambda entry: entry.created_at,
        )
        paths = []
        for entry in recent[-self.image_context_max_images :]:
            path = archive.root / entry.file
            if path.is_file():
                paths.append(str(path))
        if paths:
            logger.info(f"[图片库] 为当前问题补充 {len(paths)} 张最近图片")
        return paths

    def _archive_memory_block(self, event, chat_id: str) -> str:
        """Describe the persona's own image library when it is relevant.

        Args:
            event: Current message event.
            chat_id: Scoped conversation key used by the archive.

        Returns:
            A short prompt block, or an empty string when not relevant.
        """
        archive = getattr(self, "image_archive", None)
        if archive is None or not chat_id:
            return ""
        text = str(event.get_message_str() or "")
        has_images = bool(event.get_extra("_persona_turn_image_urls"))
        if not has_images and not re.search(
            r"图库|收藏|保存|评分|表情包|图片|那张图|旧图", text
        ):
            return ""
        entries = [
            entry
            for entry in archive.entries
            if entry.status == "active" and archive.matches_entry(entry, chat_id)
        ]
        if not entries:
            return ""
        entries.sort(key=lambda entry: entry.created_at, reverse=True)
        lines = [
            "[我保存过的图片]",
            "这些是我自己收进图库的，评分也是我当时给的。",
        ]
        for entry in entries[:3]:
            tags = "、".join(entry.tags[:6]) or "无标签"
            lines.append(
                f"- 我的评分 {entry.score:.2f}"
                f"{'（收藏）' if entry.favorite else ''}｜{tags}｜"
                f"{entry.description or '（无描述）'}"
            )
        return "\n".join(lines)

    def _lookup_tools(self) -> list:
        """Build a web lookup tool for uncertain names and terms."""
        from astrbot.core.agent.tool import FunctionTool

        if not self._resolve_tavily_key():
            return []

        async def web_lookup(event, query: str = "", max_results: int = 3) -> str:
            question = str(query or "").strip()
            if not question:
                return "没有提供要查的词。"
            key = self._resolve_tavily_key()
            if not key:
                return "没有配置搜索，查不了。"
            payload = {
                "api_key": key,
                "query": question,
                "max_results": max(1, min(int(max_results or 3), 5)),
                "include_answer": True,
                "search_depth": "basic",
            }
            try:
                timeout = aiohttp.ClientTimeout(total=20)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        "https://api.tavily.com/search", json=payload
                    ) as response:
                        if response.status != 200:
                            logger.warning(f"[联网查证] 搜索返回 {response.status}")
                            return "搜索暂时失败。"
                        data = await response.json()
            except Exception as exc:
                logger.warning(f"[联网查证] 搜索失败: {exc}")
                return "搜索暂时失败。"
            parts = []
            answer = str(data.get("answer") or "").strip()
            if answer:
                parts.append(f"搜索摘要：{answer}")
            for item in (data.get("results") or [])[:5]:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                content = str(item.get("content") or "").strip()
                if title or content:
                    parts.append(f"{title}：{content}"[:400])
            logger.info(f"[联网查证] 查询「{question}」：{len(parts)} 条素材")
            return "\n".join(parts) or "没有查到明确信息。"

        return [
            FunctionTool(
                name="web_lookup",
                description=(
                    "搜索作品、角色、梗或专有名词，返回搜索摘要和来源片段。"
                    "query 可以带上作品名或上下文。搜索结果只是资料。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索词"},
                        "max_results": {
                            "type": "integer",
                            "description": "返回几条结果，默认 3",
                        },
                    },
                    "required": ["query"],
                },
                handler=web_lookup,
            )
        ]

    def _followup_enabled(self, is_private: bool) -> bool:
        """Tell whether follow-up bubbles are enabled for this scope.

        Args:
            is_private: Whether the current turn is a private conversation.

        Returns:
            True when the configured scope includes this conversation.
        """
        if not self.enable_followup_reply:
            return False
        if self.followup_scope == "private":
            return is_private
        if self.followup_scope == "group":
            return not is_private
        return True

    def _followup_tools(self, is_private: bool) -> list:
        """Build the optional follow-up queue tool for multi-bubble replies.

        Args:
            is_private: Whether the current turn is a private conversation.

        Returns:
            A one-item tool list when follow-ups are enabled for this scope.
        """
        from astrbot.core.agent.tool import FunctionTool

        if not self._followup_enabled(is_private):
            return []

        async def queue_followup(event, text: str = "") -> str:
            cleaned = str(text or "").strip()
            if not cleaned:
                return "没有内容。"
            pending = list(event.get_extra("_persona_followups") or [])
            if len(pending) >= self.followup_max_messages:
                return "当前回复后已经有补充了。"
            if cleaned in pending:
                return "这句已经排过了。"
            pending.append(cleaned[:160])
            event.set_extra("_persona_followups", pending)
            logger.info(f"[多气泡] 已排队：{cleaned[:60]}")
            return "已排在当前回复后面。"

        return [
            FunctionTool(
                name="queue_followup",
                description=(
                    "把一句补充排到当前回复之后；主回复发出后会自动接着发送。"
                    "不会单独生成，也不会立即发送。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "要接在后面发的一句短话",
                        }
                    },
                    "required": ["text"],
                },
                handler=queue_followup,
            )
        ]

    def _archive_entry_send_error(self, event, entry) -> str:
        """Return why an archive entry cannot be sent, or an empty string.

        Args:
            event: Current conversation event.
            entry: Archive entry to check.

        Returns:
            User-facing rejection reason, or an empty string when allowed.
        """
        if not bool(getattr(entry, "sendable", False)):
            return "这张图还没有标记为可发送。"
        blocked = send_block_reason(getattr(entry, "tags", []))
        if not blocked:
            return ""
        if not self.archive_sensitive_send_enabled:
            return f"这张图带有敏感标签「{blocked}」，当前未开启敏感图发送。"
        allowlist = {str(item).strip() for item in self.archive_sensitive_allowlist}
        if not allowlist:
            return f"这张图带有敏感标签「{blocked}」，但敏感发送名单为空。"
        chat_ids = {str(event.get_sender_id() or "").strip()}
        try:
            if not event.is_private_chat():
                chat_ids.add(str(event.get_group_id() or "").strip())
        except Exception:
            pass
        chat_ids.discard("")
        if not (chat_ids & allowlist):
            return f"这张图带有敏感标签「{blocked}」，当前会话不在敏感发送名单内。"
        return ""

    def _meme_bridge_enabled(self, is_private: bool) -> bool:
        """Tell whether the optional meme bridge applies to this scope.

        Args:
            is_private: Whether the current turn is a private conversation.

        Returns:
            True when enabled and the configured scope includes this chat.
        """
        if not self.enable_meme_bridge:
            return False
        if self.meme_bridge_scope == "private":
            return is_private
        if self.meme_bridge_scope == "group":
            return not is_private
        return True

    def _meme_bridge_tools(self, is_private: bool) -> list:
        """Build the optional same-message meme selection tool.

        Args:
            is_private: Whether the current turn is a private conversation.

        Returns:
            A one-item tool list when the bridge applies to this chat.
        """
        from astrbot.core.agent.tool import FunctionTool

        if not self._meme_bridge_enabled(is_private):
            return []

        async def queue_meme(event, query: str = "") -> str:
            cleaned = str(query or "").strip()
            if not cleaned:
                return "没有描述想表达的意思。"
            pending = list(event.get_extra("_persona_queued_memes") or [])
            if len(pending) >= self.meme_bridge_max_per_turn:
                return "当前回复里已经有表情了。"
            path = await select_meme_image(
                self.context, event, cleaned, self.meme_bridge_min_score
            )
            if not path:
                return "没有找到合适的表情图。"
            pending.append(path)
            event.set_extra("_persona_queued_memes", pending)
            logger.info(f"[表情桥接] 已选图：{Path(path).name}")
            return "已排进当前回复。"

        return [
            FunctionTool(
                name="queue_meme",
                description=(
                    "按自然语言描述从表情图库选一张具体的图，排进当前回复；"
                    "不立即发送。只想发表情时可以不写正文。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "这张图要表达的意思、情绪或态度",
                        }
                    },
                    "required": ["query"],
                },
                handler=queue_meme,
            )
        ]

    def _image_tools(self) -> list:
        """Build callable tools for searching and sending saved images."""
        from astrbot.core.agent.tool import FunctionTool

        archive = getattr(self, "image_archive", None)
        if archive is None:
            return []

        async def search_saved_images(
            event, query: str = "", days: int = 30, limit: int = 5
        ) -> str:
            entries = archive.search(
                self._archive_chat_key(event),
                str(query or ""),
                days=max(1, int(days or 30)),
                limit=max(1, min(int(limit or 5), 10)),
            )
            if not entries:
                logger.info(f"[图片工具] 搜索「{query}」：没有结果")
                return "没有找到符合条件的已保存图片。"
            logger.info(f"[图片工具] 搜索「{query}」：{len(entries)} 张")
            lines = ["你自己保存和评分的图片："]
            for entry in entries:
                lines.append(
                    f"- image_id={entry.id} | {entry.description or '（无描述）'} | "
                    f"标签：{', '.join(entry.tags) or '无'} | "
                    f"我的评分：{entry.score:.2f} | "
                    f"可发送：{'是' if entry.sendable else '否'}"
                    f"{('（敏感标签：' + send_block_reason(entry.tags) + '）') if send_block_reason(entry.tags) else ''} | "
                    f"时间：{datetime.fromtimestamp(entry.created_at).strftime('%m-%d %H:%M')}"
                )
            return "\n".join(lines)

        async def send_saved_image(event, image_id: str) -> str:
            entry = next(
                (
                    item
                    for item in archive.entries
                    if item.id == str(image_id).strip()
                    and item.status == "active"
                    and archive.matches_entry(item, self._archive_chat_key(event))
                ),
                None,
            )
            if entry is None:
                return "没有找到这张图，或它不属于当前会话。"
            reason = self._archive_entry_send_error(event, entry)
            if reason:
                return reason
            path = archive.root / entry.file
            if not path.is_file():
                return "图片文件已经不在了。"
            await self.context.send_message(
                event.unified_msg_origin, MessageChain([Image(file=str(path))])
            )
            logger.info(f"[图片工具] 发送存档图 {entry.id}")
            return f"已把图片 {entry.id} 发到当前会话。"

        async def queue_saved_image(event, image_id: str) -> str:
            entry = next(
                (
                    item
                    for item in archive.entries
                    if item.id == str(image_id).strip()
                    and item.status == "active"
                    and archive.matches_entry(item, self._archive_chat_key(event))
                ),
                None,
            )
            if entry is None:
                return "没有找到这张图，或它不属于当前会话。"
            reason = self._archive_entry_send_error(event, entry)
            if reason:
                return reason
            path = archive.root / entry.file
            if not path.is_file():
                return "图片文件已经不在了。"
            pending = list(event.get_extra("_persona_queued_memes") or [])
            if len(pending) >= self.meme_bridge_max_per_turn:
                return "当前回复里已经有表情了。"
            pending.append(str(path))
            event.set_extra("_persona_queued_memes", pending)
            logger.info(f"[图片工具] 排队存档图 {entry.id}")
            return "已排进当前回复。"

        async def send_current_image(event, index: int = 1) -> str:
            refs = []
            for component in (
                getattr(getattr(event, "message_obj", None), "message", []) or []
            ):
                if isinstance(component, Image):
                    ref = str(
                        getattr(component, "url", "")
                        or getattr(component, "file", "")
                        or ""
                    ).strip()
                    if ref:
                        refs.append(ref)
            if not refs:
                refs = list(event.get_extra("_persona_turn_image_urls", []) or [])
            pos = max(1, int(index or 1)) - 1
            if not 0 <= pos < len(refs):
                return "当前消息没有这个序号的图片。"
            ref = refs[pos]
            component = (
                Image.fromURL(ref)
                if ref.startswith(("http://", "https://"))
                else Image(file=ref)
            )
            await self.context.send_message(
                event.unified_msg_origin, MessageChain([component])
            )
            logger.info(f"[图片工具] 发送当前消息图片 #{pos + 1}")
            return "已把当前消息里的图片发回当前会话。"

        async def see_avatar(event, user_id: str = "") -> str:
            target = str(user_id or event.get_sender_id() or "").strip()
            if target.lower() in {"self", "me", "你", "自己"}:
                target = str(event.get_self_id() or "").strip()
            if not target:
                return "没有可看的对象。"
            allowed = {
                str(event.get_sender_id() or "").strip(),
                str(event.get_self_id() or "").strip(),
            }
            try:
                mention_info = await self._check_mention_others(event)
                for item in (mention_info or {}).get("other_mentions", []) or []:
                    mentioned = str((item or {}).get("user_id") or "").strip()
                    if mentioned:
                        allowed.add(mentioned)
            except Exception:
                pass
            allowed.discard("")
            if target not in allowed:
                return "只能看当前说话的人或被@到的人的头像。"
            now = time.time()
            cached = self._avatar_cache.get(target)
            if cached and now - cached[0] < 21600:
                return f"头像：{cached[1]}"
            url = f"https://q1.qlogo.cn/g?b=qq&nk={target}&s=640"
            try:
                described = await ImageHandler.describe_image_urls(
                    self.context,
                    [url],
                    self.image_to_text_provider_id,
                    "用两三句客观描述这个QQ头像：主体、颜色、表情和构图。"
                    "不要猜身份，不要评价美丑。",
                    timeout=max(10, self.image_to_text_timeout),
                    session_id=str(getattr(event, "session_id", "") or ""),
                )
                description = str(described.get(url) or "").strip()
            except Exception as exc:
                logger.debug(f"[头像] 识图失败: {exc}")
                description = ""
            if not description:
                return "这次没拿到清晰的头像画面。"
            self._avatar_cache[target] = (now, description[:300])
            return f"头像：{description[:300]}"

        return [
            FunctionTool(
                name="see_avatar",
                description=(
                    "当前说话者、你自己或被@者的QQ头像画面；仅限当前会话里出现的人。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "user_id": {
                            "type": "string",
                            "description": "要看的QQ号或 self；省略则看当前说话者。",
                        }
                    },
                },
                handler=see_avatar,
            ),
            FunctionTool(
                name="search_saved_images",
                description=(
                    "Search images you saved and rated yourself in this "
                    "conversation. Returns your scores, tags and image_id. "
                    "Does not send."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Description or tag keywords; empty means recent images.",
                            "default": "",
                        },
                        "days": {
                            "type": "integer",
                            "description": "How many days back to search.",
                            "default": 30,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum results, 1-10.",
                            "default": 5,
                        },
                    },
                },
                handler=search_saved_images,
            ),
            FunctionTool(
                name="send_saved_image",
                description=(
                    "Send one of your own saved images to the current chat. "
                    "image_id must come from search_saved_images."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "image_id": {
                            "type": "string",
                            "description": "The image_id from search_saved_images.",
                        }
                    },
                    "required": ["image_id"],
                },
                handler=send_saved_image,
            ),
            FunctionTool(
                name="queue_saved_image",
                description=(
                    "Queue one of your own saved images into the current reply "
                    "instead of sending it immediately. image_id must come from "
                    "search_saved_images, and the image must be marked sendable."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "image_id": {
                            "type": "string",
                            "description": "The image_id from search_saved_images.",
                        }
                    },
                    "required": ["image_id"],
                },
                handler=queue_saved_image,
            ),
            FunctionTool(
                name="send_current_image",
                description=(
                    "Send an image from the current turn by 1-based index. "
                    "The turn may include a recent image even if it is not "
                    "repeated in this text message."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": "1-based image index in the current message.",
                            "default": 1,
                        }
                    },
                },
                handler=send_current_image,
            ),
        ]

    @staticmethod
    def _analysis_retryable(error: str) -> bool:
        """Tell whether a failed vision call is worth one automatic retry."""
        text = str(error or "").lower()
        if not text:
            return True
        permanent = (
            "does not support image",
            "doesn't support image",
            "not support image",
            "invalid api key",
            "authentication",
            "permission",
            "unauthorized",
            "401",
            "403",
            "model not found",
            "no available",
            "没有可用",
        )
        if any(marker in text for marker in permanent):
            return False
        # Everything else gets at most one automatic retry; the retry budget
        # prevents a broken provider from looping forever.
        return True

    async def _rate_archive_image(
        self, provider, image_path: str, persona_prompt: str = ""
    ) -> dict:
        """Ask the vision model to describe and rate one image for curation."""
        if provider is None:
            logger.warning("[图片库] 识图评分跳过：没有可用的聊天模型")
            return {"error": "没有可用的聊天模型"}
        persona_block = (
            f"<persona>\n{persona_prompt.strip()[:1800]}\n</persona>\n"
            "以上是你的人格。请用这个人格的兴趣、审美和关注点来判断，"
            "而不是用通用的“好看/有用”标准。\n"
            if persona_prompt.strip()
            else ""
        )
        prompt = (
            f"{persona_block}"
            "以你自己的兴趣、审美、关系和当下感觉打分，不用大众标准。\n"
            "0.90-1.00 = 本命级，会主动保存、反复看；\n"
            "0.70-0.89 = 喜欢，愿意留；\n"
            "0.40-0.69 = 一般，可留可不留；\n"
            "0.15-0.39 = 不太想留；\n"
            "0.00-0.14 = 反感或完全无价值。\n"
            "favorite 和 score 一致：0.60 及以上收藏，低于 0.60 不收藏。\n"
            "description：一到两句自然中文，写清主体、动作/表情、画面文字或梗、"
            "以及这张图可能被拿来做什么；不要只写“一个女孩”。\n"
            "tags：6 到 10 个细粒度中文标签，覆盖主体、动作/表情、文字或梗、"
            "风格/用途和情绪；按你的关注点写，不要只堆“二次元/插画”。\n"
            "另外按画面加通用用途标签，有就加、没有就不加：表情包、截图、"
            "聊天记录、二维码/证件、真人照片、R18/性暗示、血腥/暴力。\n"
            '只输出 JSON：{"description":"一到两句描述",'
            '"tags":["6到10个标签"],"score":0.0,'
            '"favorite":true/false}'
        )
        try:
            response = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                image_urls=[image_path],
                func_tool=None,
                system_prompt="",
                session_id="",
            )
            raw = str(getattr(response, "completion_text", "") or "").strip()
            raw = raw.replace("\ufeff", "").replace("\u200b", "").strip()
            if raw.startswith("```"):
                raw = raw.strip("`").strip()
                if raw.lower().startswith("json"):
                    raw = raw[4:].strip()
            data = None
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = parse_json_block(raw)
            if not isinstance(data, dict):
                if raw:
                    logger.warning(
                        f"[图片库] 识图返回的不是 JSON，改用原文做描述: {raw[:120]}"
                    )
                    return {
                        "description": raw[:300],
                        "tags": [],
                        "score": 0.2,
                        "favorite": False,
                    }
                logger.warning("[图片库] 识图返回空内容")
                return {"error": "识图返回空内容"}
            tags = data.get("tags")
            if not isinstance(tags, list):
                tags = []
            try:
                score = float(data.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            favorite = data.get("favorite")
            return {
                "description": str(data.get("description") or "").strip(),
                "tags": [str(tag).strip() for tag in tags if str(tag).strip()][:10],
                "score": max(0.0, min(1.0, score)),
                "favorite": bool(favorite) if isinstance(favorite, bool) else None,
            }
        except Exception as exc:
            logger.warning(f"[图片库] 识图评分失败: {exc}")
            return {"error": str(exc)}

    def _build_notebook(self) -> None:
        """构建记忆本（可能为 None）。"""
        # ========== 资料片：记忆本 ==========
        # 关掉时这里连对象都不建，回复流程也不会碰它。
        self.notebook = None
        if self._cfg_bool("enable_notebook", False):
            self.notebook = Notebook(
                self.plugin_data_dir,
                enabled=True,
                max_inject=self._cfg_int(
                    "notebook_max_inject", 3, minimum=1, maximum=10
                ),
                decay_days=self._cfg_int(
                    "notebook_decay_days", 30, minimum=1, maximum=365
                ),
                jargon_forgetting=self._cfg_bool("notebook_jargon_forgetting", True),
                jargon_cycle_days=self._cfg_int(
                    "notebook_jargon_cycle_days", 7, minimum=1, maximum=90
                ),
                jargon_grace_days=self._cfg_int(
                    "notebook_jargon_grace_days", 14, minimum=0, maximum=365
                ),
                protect_general_jargon=self._cfg_bool(
                    "notebook_jargon_protect_general", True
                ),
            )
            self.notebook_learn_enabled = self._cfg_bool("notebook_learn_enabled", True)
            self.notebook_learn_min_messages = self._cfg_int(
                "notebook_learn_min_messages", 20, minimum=5, maximum=200
            )
            self.notebook_learn_interval_hours = self._cfg_int(
                "notebook_learn_interval_hours", 12, minimum=1, maximum=168
            )
            self.notebook_min_confidence = self._cfg_float(
                "notebook_min_confidence", 0.6, minimum=0.0, maximum=1.0
            )
            self._notebook_last_reflect = 0.0
            # 黑话学习：从这个群的日常对话里归纳只有群里人才懂的说法。
            self.notebook_jargon_enabled = self._cfg_bool("notebook_learn_jargon", True)
            self.notebook_cross_group_jargon = self._cfg_bool(
                "notebook_cross_group_jargon", False
            )
            self.notebook_jargon_min_messages = self._cfg_int(
                "notebook_jargon_min_messages", 30, minimum=10, maximum=200
            )
            self.notebook_jargon_min_count = self._cfg_int(
                "notebook_jargon_min_count", 4, minimum=2, maximum=10
            )
            self._jargon_buffers = {}
            self._jargon_last_run = 0.0
            for raw in self._cfg_list("notebook_entries", []):
                text = str(raw or "").strip()
                if not text:
                    continue
                kind, _, value = text.partition("|")
                if value:
                    self.notebook.add(value.strip(), kind.strip() or "like")
                else:
                    self.notebook.add(text)
            logger.info(f"[记忆本] 已启用，共 {len(self.notebook.entries)} 条")

    def _build_curiosity(self) -> None:
        """构建好奇心（独立知识库，记忆本只提供兴趣）。"""
        # ========== 资料片：好奇心 ==========
        self.curiosity_module = None
        if self._curiosity_task is not None:
            self._curiosity_task.cancel()
            self._curiosity_task = None
        if self._cfg_bool("enable_curiosity", False):
            notebook = getattr(self, "notebook", None)
            store = CuriosityStore(
                self.plugin_data_dir,
                pending_ttl_days=self._cfg_int(
                    "curiosity_pending_ttl_days", 7, minimum=0, maximum=365
                ),
                knowledge_ttl_days=self._cfg_int(
                    "curiosity_knowledge_ttl_days", 0, minimum=0, maximum=365
                ),
                forgetting_cycle_days=self._cfg_int(
                    "curiosity_forgetting_cycle_days", 7, minimum=1, maximum=90
                ),
                forgetting_grace_days=self._cfg_int(
                    "curiosity_forgetting_grace_days", 14, minimum=0, maximum=365
                ),
            )
            if notebook is not None:
                # Migrate old curiosity/knows entries out of the memory network.
                for entry in notebook.extract_kinds({"curious", "knows"}):
                    if entry.kind == "curious":
                        store.add(
                            entry.text,
                            source=entry.source or "curiosity",
                            query=entry.query or entry.text,
                        )
                        continue
                    topic = entry.text
                    summary = entry.text
                    if "：" in entry.text:
                        topic, _, summary = entry.text.partition("：")
                    topic = topic.strip()
                    if topic and store.add(
                        topic, source="curiosity", query=entry.query or topic
                    ):
                        store.resolve(topic, summary.strip())
            self.curiosity_module = CuriosityModule(
                self.context,
                notebook,
                types.SimpleNamespace(
                    daily_cap=self._cfg_int(
                        "curiosity_daily_cap", 5, minimum=1, maximum=20
                    ),
                    min_gap_minutes=self._cfg_int(
                        "curiosity_min_gap_minutes", 10, minimum=0, maximum=120
                    ),
                    new_topics_per_day=self._cfg_int(
                        "curiosity_new_topics_per_day", 5, minimum=1, maximum=100
                    ),
                    pending_soft_limit=self._cfg_int(
                        "curiosity_pending_soft_limit", 12, minimum=1, maximum=200
                    ),
                    persona_id=self._cfg_str("curiosity_persona_id", ""),
                    quiet_hours=self._cfg_str("curiosity_quiet_hours", "01:00-07:00"),
                    results=3,
                    max_chars=self._cfg_int(
                        "curiosity_max_chars", 400, minimum=20, maximum=2000
                    ),
                    provider_id=self._cfg_str("curiosity_provider_id", ""),
                    tavily_key=self._resolve_tavily_key(),
                ),
                store=store,
            )
            removed = self.curiosity_module.cleanup_pending()
            if removed:
                logger.info(f"[好奇心] 清理了 {removed} 条无效待查")
            try:
                self._curiosity_task = asyncio.create_task(self._curiosity_worker())
                self._feature_tasks.add(self._curiosity_task)
            except Exception as exc:
                logger.warning(f"[好奇心] 后台查询启动失败: {exc}")
            logger.info("[好奇心] 已启用")

    def _curiosity_state_lines(self) -> list[str]:
        """Return recent curiosity knowledge as a proactive prompt block.

        Only fresh lookups become a social impulse; older knowledge is still
        available to the reply path through its own background block.

        Returns:
            Up to three short topic/summary lines.
        """
        module = getattr(self, "curiosity_module", None)
        if module is None:
            return []
        now = time.time()
        lines: list[str] = []
        for entry in module.store.knowledge(5):
            if now - (entry.resolved_at or entry.created_at) > 6 * 3600:
                continue
            if not entry.summary:
                continue
            module.store.touch(entry.text)
            lines.append(f"{entry.text}：{entry.summary[:120]}")
        return lines[:3]

    def _build_proactive(self) -> None:
        """构建主动发言。"""
        # ========== 资料片：主动发言 ==========
        # 整套能力。关掉时这里连对象都不建，主流程也不会碰它。
        self.proactive_module = None
        if self._cfg_bool("enable_proactive", False):
            configured_groups = [
                str(item).strip()
                for item in self._cfg_list("proactive_groups", [])
                if str(item).strip()
            ]
            all_groups = not configured_groups and not self.enabled_groups
            settings = ProactiveSettings(
                groups=set(configured_groups or self.enabled_groups),
                all_groups=all_groups,
                cooldown_minutes=self._cfg_int(
                    "proactive_cooldown_minutes", 10, minimum=1, maximum=240
                ),
                min_gap_seconds=self._cfg_int(
                    "proactive_min_gap_seconds", 90, minimum=10, maximum=3600
                ),
                daily_cap=self._cfg_int(
                    "proactive_daily_cap", 0, minimum=0, maximum=200
                ),
                history_ttl_days=self._cfg_int(
                    "proactive_history_ttl_days", 3, minimum=0, maximum=90
                ),
                global_cooldown_minutes=self._cfg_int(
                    "proactive_global_cooldown_minutes", 30, minimum=0, maximum=240
                ),
                min_lull_seconds=self._cfg_int(
                    "proactive_min_lull_seconds", 90, minimum=0, maximum=3600
                ),
                max_lull_seconds=self._cfg_int(
                    "proactive_max_lull_seconds", 900, minimum=0, maximum=86400
                ),
                quiet_hours=self._cfg_str("proactive_quiet_hours", "01:00-07:00"),
                min_messages=self._cfg_int(
                    "proactive_min_messages", 3, minimum=1, maximum=20
                ),
                base_probability=self._cfg_float(
                    "proactive_base_probability", 0.35, minimum=0.0, maximum=1.0
                ),
                max_chars=self._cfg_int(
                    "proactive_max_chars", 30, minimum=1, maximum=200
                ),
                provider_id=self._cfg_str("proactive_provider_id", ""),
            )
            notebook_for_proactive = getattr(self, "notebook", None)
            self.proactive_module = ProactiveModule(
                self.context,
                settings,
                persona_provider=self._resolve_persona_prompt,
                # 它在意什么，是"想不想说"的依据。
                interest_provider=(
                    notebook_for_proactive.all_lines
                    if notebook_for_proactive is not None
                    else None
                ),
                state_provider=(
                    self.life_sight.proactive_lines
                    if getattr(self, "life_sight", None) is not None
                    else None
                ),
                # 刚查到的东西会留在脑子里，聊到相关话题时可能顺口说。
                knowledge_provider=self._curiosity_state_lines,
                data_dir=self.plugin_data_dir,
            )
            logger.info(
                "[主动发言] 已启用，群: "
                + (
                    ", ".join(sorted(settings.groups))
                    or ("全部（跟随启用群）" if all_groups else "（无）")
                )
            )

    # ============================================================
    # Feature-pack contract: base-owned identity, time and delivery
    # ============================================================

    def _feature_now(self) -> datetime:
        """Resolve current time with AstrBot's configured time zone.

        Returns:
            A time-zone-aware datetime; local time when no zone is configured.
        """
        tz_name = ""
        try:
            config = self.context.get_config()
            tz_name = str(config.get("timezone") or "").strip()
        except Exception:
            tz_name = ""
        if tz_name:
            try:
                return datetime.now(ZoneInfo(tz_name))
            except Exception:
                pass
        return datetime.now().astimezone()

    async def _resolve_active_persona_prompt(self, event: AstrMessageEvent) -> str:
        """Resolve the persona actually selected for one conversation.

        This mirrors the formal reply resolver: forced session persona first,
        then session selection, then the default persona.

        Args:
            event: Message event whose conversation should be resolved.

        Returns:
            The resolved persona prompt, or an empty string on failure.
        """
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return ""

        conversation = None
        try:
            conversation_manager = getattr(self.context, "conversation_manager", None)
            if conversation_manager is not None:
                conversation_id = await conversation_manager.get_curr_conversation_id(
                    event.unified_msg_origin
                )
                if conversation_id:
                    conversation = await conversation_manager.get_conversation(
                        event.unified_msg_origin, conversation_id
                    )
        except Exception:
            conversation = None

        try:
            if hasattr(manager, "resolve_selected_persona"):
                config = self.context.get_config(umo=event.unified_msg_origin) or {}
                _, persona, _, _ = await manager.resolve_selected_persona(
                    umo=event.unified_msg_origin,
                    conversation_persona_id=getattr(conversation, "persona_id", None),
                    platform_name=event.get_platform_name(),
                    provider_settings=config.get("provider_settings", {}),
                )
                if persona:
                    return str(persona.get("prompt", "") or "")
        except Exception as exc:
            logger.debug(f"[Feature] Persona resolution failed: {exc}")

        try:
            persona = await manager.get_default_persona_v3(event.unified_msg_origin)
            return str(persona.get("prompt", "") or "")
        except Exception:
            return ""

    async def _resolve_feature_provider_id(self, event: AstrMessageEvent) -> str:
        """Resolve the chat provider for one conversation.

        Args:
            event: Message event whose session provider should be used.

        Returns:
            A provider id, or an empty string when none is available.
        """
        try:
            provider = await self.context.get_using_provider_async(
                event.unified_msg_origin
            )
            return str(provider.meta().id) if provider else ""
        except Exception:
            return ""

    async def _build_feature_context(
        self,
        event: AstrMessageEvent,
        is_private: bool,
        raw_chat_id: str,
        *,
        full: bool = False,
    ) -> FeatureContext:
        """Build the read-only context handed to feature packs.

        Args:
            event: Current message event.
            is_private: Whether the event is a private conversation.
            raw_chat_id: Platform chat identifier.
            full: Resolve persona and provider when True.

        Returns:
            The normalised feature context.
        """
        chat_key = ProbabilityManager.get_chat_key(
            event.get_platform_name(), is_private, raw_chat_id
        )
        persona = ""
        provider_id = ""
        if full:
            persona = await self._resolve_active_persona_prompt(event)
            provider_id = await self._resolve_feature_provider_id(event)
        return FeatureContext(
            event=event,
            umo=str(event.unified_msg_origin),
            group_id="" if is_private else str(raw_chat_id or ""),
            is_private=is_private,
            chat_key=chat_key,
            persona=persona,
            provider_id=provider_id,
            now=self._feature_now(),
        )

    async def _observe_feature_event(
        self,
        event: AstrMessageEvent,
        is_private: bool,
        raw_chat_id: str,
    ) -> None:
        """Let feature packs observe one turn without side effects.

        Args:
            event: Current message event.
            is_private: Whether the event is a private conversation.
            raw_chat_id: Platform chat identifier.
        """
        module = getattr(self, "proactive_module", None)
        if module is None or is_private:
            return
        try:
            ctx = await self._build_feature_context(event, is_private, raw_chat_id)
            await module.on_event(ctx)
        except Exception as exc:
            logger.debug(f"[Feature] Observation failed: {exc}")

    async def _maybe_propose_feature(
        self,
        event: AstrMessageEvent,
        is_private: bool,
        raw_chat_id: str,
        *,
        decision_ai_failed: bool = False,
        decision_source: str = "ai",
    ) -> bool:
        """Offer feature packs a turn after the base decided to stay silent.

        A pack may only add a turn when the Persona itself declined. Hard local
        policies (throttle, turn-taking guard, fatigue, addressed mode) set
        source=policy and must not be bypassed by a pack.

        Args:
            event: Current message event.
            is_private: Whether the event is a private conversation.
            raw_chat_id: Platform chat identifier.
            decision_ai_failed: Whether the participation check failed.
            decision_source: Source of the silence decision.

        Returns:
            True when a proposal was accepted and delivered.
        """
        module = getattr(self, "proactive_module", None)
        if module is None or is_private or decision_ai_failed:
            return False
        if decision_source not in {"ai", "legacy"}:
            return False
        if not self.takeover_group_reply or self.group_reply_scope != "ambient":
            return False
        try:
            cheap_ctx = await self._build_feature_context(
                event, is_private, raw_chat_id
            )
            can_propose = getattr(module, "can_propose", None)
            if callable(can_propose) and not can_propose(cheap_ctx):
                return False
            ctx = await self._build_feature_context(
                event, is_private, raw_chat_id, full=True
            )
            proposal = await module.propose(ctx)
        except Exception as exc:
            logger.warning(f"[Feature] Proposal failed: {exc}")
            return False
        if proposal is None:
            return False
        return await self._send_feature_proposal(ctx, proposal, module)

    async def _send_feature_proposal(
        self,
        ctx: FeatureContext,
        proposal: Proposal,
        module: Any,
    ) -> bool:
        """Apply the base send gate and deliver one feature proposal.

        Args:
            ctx: Read-only turn context resolved by the base.
            proposal: Line the feature pack wants to add.
            module: Pack that produced the proposal, for acknowledgement.

        Returns:
            True when the line was delivered.
        """

        def note(reason: str) -> None:
            fn = getattr(module, "note_skip", None)
            if callable(fn):
                fn(ctx.group_id, reason)

        text = AIResponseFilter.sanitize_reply_text(str(proposal.text or "")).strip()
        text = AIResponseFilter.strip_leaked_system_tags(text).strip()
        if not text:
            note("空内容")
            return False

        now_ts = time.time()
        try:
            async with self.concurrent_lock:
                if any(
                    chat_key == ctx.chat_key
                    for chat_key in self.processing_sessions.values()
                ):
                    logger.info("[Feature] Skipped proposal: a base reply is in flight")
                    note("正式回复进行中")
                    return False
                owner = self._chat_flow_owners.get(ctx.chat_key)
                if owner and owner.get("owner") == "normal":
                    logger.info("[Feature] Skipped proposal: chat flow is busy")
                    note("会话正忙")
                    return False

            if now_ts - self._last_bot_reply_at.get(ctx.chat_key, 0.0) < 20.0:
                logger.info("[Feature] Skipped proposal: the base spoke recently")
                note("正式回复刚说过话")
                return False

            if self.enable_duplicate_filter:
                recent = self.recent_replies_cache.setdefault(ctx.chat_key, [])
                limit = max(60, self.duplicate_filter_time_limit)
                recent[:] = [
                    item
                    for item in recent
                    if now_ts - float(item.get("timestamp", 0) or 0) < limit
                ]
                check_count = max(1, self.duplicate_filter_check_count)
                for item in recent[-check_count:]:
                    if text == str(item.get("content", "")).strip():
                        logger.info("[Feature] Skipped proposal: duplicate line")
                        note("重复内容")
                        return False

            delivered = await self.context.send_message(
                ctx.umo,
                MessageChain([Plain(text)]),
            )
            if delivered is False:
                logger.warning("[Feature] Proposal not delivered by the platform")
                note("平台未送达")
                return False
        except Exception as exc:
            logger.warning(f"[Feature] Proposal send failed: {exc}")
            note(f"发送失败: {str(exc)[:80]}")
            return False

        self._last_bot_reply_at[ctx.chat_key] = now_ts
        try:
            recent = self.recent_replies_cache.setdefault(ctx.chat_key, [])
            recent.append({"content": text, "timestamp": now_ts})
            max_cache_size = min(
                max(10, self.duplicate_filter_check_count * 2),
                self._DUPLICATE_CACHE_SIZE_LIMIT,
            )
            if len(recent) > max_cache_size:
                recent[:] = recent[-max_cache_size:]
        except Exception:
            pass

        try:
            await ContextManager.save_bot_message(ctx.event, text, self.context)
        except Exception as exc:
            logger.warning(f"[Feature] Proposal history save failed: {exc}")

        try:
            await module.on_proposal_sent(ctx, proposal)
        except Exception as exc:
            logger.warning(f"[Feature] Proposal acknowledgement failed: {exc}")

        logger.info(f"[Feature] Sent {proposal.source} proposal: {text[:60]}")
        return True

    def _notebook_observe(self, event, reply_text: str) -> None:
        """Queue one real exchange for the persona's notebook.

        Args:
            event: The message that was answered.
            reply_text: What the persona actually said.
        """
        notebook = getattr(self, "notebook", None)
        if notebook is None:
            return
        try:
            user_text = getattr(event, "message_str", "") or ""
            notebook.observe(user_text, reply_text)
            self._maybe_reflect_notebook(event)
        except Exception as e:
            logger.warning(f"[记忆本] 记录失败: {e}")

    def _collect_jargon_line(self, event, text: str) -> None:
        """Keep a rolling window of group chatter for slang mining.

        Args:
            event: The group message.
            text: Its text content.
        """
        if not getattr(self, "notebook_jargon_enabled", False) or not text:
            return
        group_id = str(event.get_group_id() or "")
        if not group_id:
            return
        buffer = self._jargon_buffers.setdefault(group_id, [])
        buffer.append(text[:120])
        if len(buffer) > 40:
            del buffer[:-40]
        if len(buffer) < getattr(self, "notebook_jargon_min_messages", 30):
            return
        if time.time() - getattr(self, "_jargon_last_run", 0.0) < 2 * 3600:
            return
        self._jargon_last_run = time.time()
        chunk = list(buffer)
        buffer.clear()
        try:
            asyncio.create_task(self._learn_jargon(group_id, chunk))
        except Exception as e:
            logger.warning(f"[黑话] 调度失败: {e}")

    async def _learn_jargon(self, group_id: str, lines: list[str]) -> None:
        """Mine group slang: prefilter, validate, then infer its meaning.

        Args:
            group_id: Platform group id the slang belongs to.
            lines: Recent messages used as evidence.
        """
        notebook = getattr(self, "notebook", None)
        if notebook is None:
            return

        candidates = candidate_terms(
            lines, min_count=getattr(self, "notebook_jargon_min_count", 4)
        )
        if not candidates:
            logger.info(f"[黑话] 群 {group_id} 看了 {len(lines)} 条消息，没抽到候选词")
            return

        provider = await self.context.get_using_provider_async()
        provider_id = str(provider.meta().id) if provider else ""
        if not provider_id:
            return

        async def ask(prompt: str, max_tokens: int = 400) -> str:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id, prompt=prompt, max_tokens=max_tokens
            )
            return str(getattr(response, "completion_text", "") or "")

        try:
            picked = parse_json_block(
                await ask(build_extract_prompt(candidates, lines))
            )
        except Exception as e:
            logger.warning(f"[黑话] 候选校验失败: {e}")
            return
        if not isinstance(picked, list):
            return

        added = 0
        for item in picked[:2]:
            if not isinstance(item, dict):
                continue
            term = str(item.get("term") or "").strip()
            sample = str(item.get("sample") or "").strip()
            if len(term) < 2:
                continue
            web = ""
            if self._cfg_bool("notebook_jargon_web_search", True):
                web = await search_jargon_web(term, self._resolve_tavily_key(), 3)
                if web:
                    logger.info(f"[黑话] 联网核对「{term}」：{len(web)} 字符")
            ctx_prompt, only_prompt, compare_tpl = build_meaning_prompts(
                term, sample, lines, web=web
            )
            try:
                with_ctx = parse_json_block(await ask(ctx_prompt, 260))
                if isinstance(with_ctx, dict) and with_ctx.get("no_info"):
                    continue
                meaning_a = str((with_ctx or {}).get("meaning") or "").strip()
                alt = str((with_ctx or {}).get("alt") or "").strip()
                only = parse_json_block(await ask(only_prompt, 260))
                if isinstance(only, dict) and not only.get("no_info"):
                    meaning_b = str(only.get("meaning") or "").strip()
                else:
                    meaning_b = ""
                verdict = parse_json_block(
                    await ask(
                        compare_tpl.format(
                            term=term,
                            a=meaning_a or "（无）",
                            b=meaning_b or "（无）",
                            web=web or "（没有搜到可用资料）",
                        ),
                        260,
                    )
                )
            except Exception as e:
                logger.warning(f"[黑话] 推断「{term}」失败: {e}")
                continue

            if not isinstance(verdict, dict) or verdict.get("no_info"):
                continue
            if not bool(verdict.get("is_jargon")):
                continue
            meaning = str(verdict.get("meaning") or "").strip()
            try:
                confidence = float(verdict.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            if not meaning or confidence < 0.5:
                continue
            scope = str(verdict.get("scope") or "").strip().lower()
            cross_group = scope not in {"group", "person"}
            if notebook.add_jargon(
                term,
                meaning,
                group=group_id,
                alt=alt,
                cross_group=cross_group,
            ):
                added += 1
                logger.info(
                    f"[黑话] 群 {group_id} 学到「{term}」，范围={scope or 'unknown'}"
                )

        if added:
            logger.info(f"[黑话] 群 {group_id} 学到 {added} 条")
        else:
            logger.info(
                f"[黑话] 群 {group_id} 候选 {len(candidates)} 个"
                f"（{', '.join(str(item[0] if isinstance(item, tuple) else item) for item in candidates[:5])}），本次没有确认的新词"
            )

    def _maybe_reflect_notebook(self, event) -> None:
        """Start a reflection batch when enough real exchanges piled up.

        Args:
            event: A message event used to resolve the persona prompt.
        """
        if not getattr(self, "notebook_learn_enabled", False):
            return
        notebook = getattr(self, "notebook", None)
        if notebook is None:
            return
        if len(notebook.pending) < getattr(self, "notebook_learn_min_messages", 20):
            return
        interval = getattr(self, "notebook_learn_interval_hours", 12) * 3600
        if time.time() - getattr(self, "_notebook_last_reflect", 0.0) < interval:
            return
        self._notebook_last_reflect = time.time()
        try:
            asyncio.create_task(self._reflect_notebook(event))
        except Exception as e:
            logger.warning(f"[记忆本] 整理调度失败: {e}")

    async def _reflect_notebook(self, event) -> None:
        """Ask the model what the persona revealed about itself, then merge it.

        Args:
            event: A message event used to resolve the persona prompt.
        """
        notebook = getattr(self, "notebook", None)
        if notebook is None or not notebook.pending:
            return
        try:
            persona = await self._resolve_active_persona_prompt(event)
            prompt = notebook.build_reflect_prompt(persona)
            provider = await self.context.get_using_provider_async()
            provider_id = str(provider.meta().id) if provider else ""
            if not provider_id:
                return
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                max_tokens=600,
            )
            text = str(getattr(response, "completion_text", "") or "")
            new_count, strengthened = notebook.absorb_reflection(
                text, min_confidence=getattr(self, "notebook_min_confidence", 0.6)
            )
            logger.info(
                f"[记忆本] 整理完成：新增 {new_count} 条，强化 {strengthened} 条"
            )
        except Exception as e:
            logger.warning(f"[记忆本] 整理失败: {e}")

    def _schedule_curiosity(self, event) -> None:
        """Hand one group message to the curiosity pack.

        Args:
            event: The group message to inspect.
        """
        module = getattr(self, "curiosity_module", None)
        if module is None:
            return
        try:
            asyncio.create_task(self._curiosity_step(event, module))
        except Exception as exc:
            logger.debug(f"[好奇心] 调度失败: {exc}")

    async def _curiosity_step(self, event, module) -> None:
        """Record unknown words, then look one of them up in the background.

        Args:
            event: The group message to inspect.
            module: The curiosity pack.
        """
        try:
            group_id = str(event.get_group_id() or "")
            needs_full = bool(getattr(module, "needs_attention", lambda: False)())
            ctx = await self._build_feature_context(
                event, False, group_id, full=needs_full
            )
            await module.on_message(event, False, ctx=ctx)
            if module.store.pending(1):
                if not ctx.persona or not ctx.provider_id:
                    ctx = await self._build_feature_context(
                        event, False, group_id, full=True
                    )
                await module.research(ctx=ctx)
        except Exception as exc:
            logger.debug(f"[好奇心] 执行失败: {exc}")

    async def _curiosity_worker(self) -> None:
        """Drain the curiosity backlog in the background, within its budget.

        The worker only asks :class:`CuriosityModule` to look at one pending
        topic at a time; daily cap, quiet hours and the minimum gap remain
        enforced inside ``research()``.
        """
        while True:
            await asyncio.sleep(60)
            module = getattr(self, "curiosity_module", None)
            if module is None:
                return
            if getattr(module, "_running", False):
                continue
            if not module.store.pending(1):
                continue
            try:
                await module.research()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"[好奇心] 后台查询失败: {exc}")

    def _resolve_tavily_key(self) -> str:
        """Read the Tavily key from AstrBot's provider settings.

        Returns:
            The API key, or an empty string when web search is not configured.
        """
        try:
            config = self.context.get_config()
        except Exception:
            return ""
        try:
            settings = config.get("provider_settings") or {}
        except Exception:
            return ""
        keys = settings.get("websearch_tavily_key") or []
        if isinstance(keys, list):
            return str(keys[0]) if keys else ""
        return str(keys or "")

    async def _resolve_persona_prompt(self, umo: str) -> str:
        """Read the persona prompt for one session.

        Args:
            umo: Unified message origin of the session.

        Returns:
            The persona prompt, or an empty string.
        """
        try:
            persona = await self.context.persona_manager.get_default_persona_v3(umo)
            return str(persona.get("prompt") or "")
        except Exception:
            return ""

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
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/jargon/run",
                self._api_jargon_run,
                ["POST"],
                "手动跑一次黑话挖掘",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/curiosity/run",
                self._api_curiosity_run,
                ["POST"],
                "手动跑一次好奇心查询",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/proactive",
                self._api_proactive,
                ["GET"],
                "主动发言状态与活动记录",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/proactive/dryrun",
                self._api_proactive_dryrun,
                ["POST"],
                "主动发言试跑（不发送）",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/curiosity",
                self._api_curiosity,
                ["GET"],
                "好奇心队列与知识库",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/curiosity/delete",
                self._api_curiosity_delete,
                ["POST"],
                "删除一条好奇心话题或知识",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/curiosity/update",
                self._api_curiosity_update,
                ["POST"],
                "编辑一条好奇心话题或知识",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/personas",
                self._api_personas,
                ["GET"],
                "可选人格列表",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/image_archive",
                self._api_image_archive,
                ["GET"],
                "长期图片库列表与容量",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/image_archive/file",
                self._api_image_archive_file,
                ["GET"],
                "读取图片库原图",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/image_archive/preview",
                self._api_image_archive_preview,
                ["GET"],
                "查看图片大图",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/image_archive/rate",
                self._api_image_archive_rate,
                ["POST"],
                "重新识图一张图片",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/image_archive/delete",
                self._api_image_archive_delete,
                ["POST"],
                "删除一张图片库图片",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/image_archive/sendable",
                self._api_image_archive_sendable,
                ["POST"],
                "标记图片库图片是否可发送",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/notebook",
                self._api_notebook,
                ["GET"],
                "记忆本网络（条目、关联、待查问题）",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/notebook/delete",
                self._api_notebook_delete,
                ["POST"],
                "删除一条记忆本条目",
            )
            self.context.register_web_api(
                f"/{self._PLUGIN_NAME}/notebook/update",
                self._api_notebook_update,
                ["POST"],
                "改写一条记忆本条目",
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
                "version": PLUGIN_VERSION,
                "values": values,
                "groups": groups,
                "runtime": runtime,
            }
        )

    async def _api_jargon_run(self):
        """手动跑一次黑话挖掘，只用于验证效果。"""
        from astrbot.api.web import error_response, json_response, request

        if getattr(self, "notebook", None) is None:
            return error_response("记忆本未启用")
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        raw = str((payload or {}).get("text") or "").strip()
        if not raw:
            return error_response("请提供一段聊天记录，用换行分隔")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        before = len(getattr(self.notebook, "entries", []))
        await self._learn_jargon("manual", lines)
        added = [
            entry.text
            for entry in self.notebook.entries[before:]
            if entry.kind == "jargon"
        ]
        return json_response({"added": added, "lines": len(lines)})

    async def _api_curiosity_run(self):
        """真实跑一次好奇心查询，用于手动验证。"""
        from astrbot.api.web import error_response, json_response, request

        module = getattr(self, "curiosity_module", None)
        if module is None:
            return error_response("好奇心未启用")
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        term = str((payload or {}).get("term") or "").strip()
        if not term:
            return error_response("请提供一个要查的词")
        try:
            summary = await module.research(force=True, term=term)
        except TypeError:
            # 兼容旧签名
            module.store.add(term, source="manual", query=term)
            summary = await module.research(force=True)
        except Exception as e:
            return error_response(f"查询失败: {e}")
        if not summary:
            return json_response({"ok": False, "term": term, "summary": ""})
        return json_response({"ok": True, "term": term, "summary": summary})

    async def _api_curiosity(self):
        """Return pending topics and looked-up knowledge for the page."""
        from astrbot.api.web import json_response

        module = getattr(self, "curiosity_module", None)
        if module is None:
            return json_response(
                {
                    "enabled": False,
                    "pending": [],
                    "knowledge": [],
                    "today_count": 0,
                    "daily_cap": 0,
                }
            )

        def stamp(ts: float) -> str:
            try:
                return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
            except (TypeError, ValueError, OSError):
                return ""

        def pack(entry) -> dict:
            return {
                "text": entry.text,
                "query": entry.query,
                "source": entry.source,
                "why": entry.why,
                "summary": entry.summary,
                "created": stamp(entry.created_at),
                "resolved": stamp(entry.resolved_at) if entry.resolved_at else "",
                "forget_stage": int(entry.forget_stage or 0),
                "use_count": int(entry.use_count or 0),
                "last_used": (stamp(entry.last_used_at) if entry.last_used_at else ""),
            }

        module.store.prune()
        pending = [pack(entry) for entry in module.store.pending(100)]
        knowledge = [pack(entry) for entry in module.store.knowledge(100)]
        return json_response(
            {
                "enabled": True,
                "pending": pending,
                "knowledge": knowledge,
                "today_count": module._today_count(datetime.now()),
                "daily_cap": int(getattr(module.settings, "daily_cap", 0) or 0),
                "today_added": module._today_add_count(),
                "new_topics_per_day": int(
                    getattr(module.settings, "new_topics_per_day", 0) or 0
                ),
                "pending_soft_limit": int(
                    getattr(module.settings, "pending_soft_limit", 0) or 0
                ),
                "quiet_hours": str(getattr(module.settings, "quiet_hours", "") or ""),
            }
        )

    async def _api_curiosity_delete(self):
        """Delete one pending topic or knowledge entry."""
        from astrbot.api.web import error_response, json_response, request

        module = getattr(self, "curiosity_module", None)
        if module is None:
            return error_response("好奇心未启用")
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        text = str((payload or {}).get("text") or "").strip()
        if not text:
            return error_response("缺少 text")
        if not module.store.remove(text):
            return error_response("没有找到这条记录")
        return json_response({"ok": True, "text": text})

    async def _api_curiosity_update(self):
        """Edit the query, reason, summary or topic of one entry."""
        from astrbot.api.web import error_response, json_response, request

        module = getattr(self, "curiosity_module", None)
        if module is None:
            return error_response("好奇心未启用")
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        data = payload or {}
        text = str(data.get("text") or "").strip()
        if not text:
            return error_response("缺少 text")
        changed = module.store.update(
            text,
            query=data.get("query") if "query" in data else None,
            summary=data.get("summary") if "summary" in data else None,
            why=data.get("why") if "why" in data else None,
            new_text=data.get("new_text") if "new_text" in data else None,
        )
        if not changed:
            return error_response("没有找到这条记录，或新话题重名")
        return json_response({"ok": True, "text": text})

    async def _api_proactive(self):
        """Return proactive gates, recent activity and group buffers."""
        from astrbot.api.web import json_response

        module = getattr(self, "proactive_module", None)
        if module is None:
            return json_response(
                {
                    "enabled": False,
                    "groups": [],
                    "history": [],
                    "today_count": 0,
                    "daily_cap": 0,
                }
            )
        return json_response({"enabled": True, **module.snapshot(self._feature_now())})

    async def _api_proactive_dryrun(self):
        """让主动发言试跑一次：只生成不发送，用来观察它想说什么。"""
        from astrbot.api.web import error_response, json_response, request

        module = getattr(self, "proactive_module", None)
        if module is None:
            return error_response("主动发言未启用")
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        group = str((payload or {}).get("group") or "").strip()
        if not group:
            groups = sorted(getattr(module.settings, "groups", set()) or [])
            group = groups[0] if groups else ""
        if not group:
            return error_response("没有可用的群")
        extra = str((payload or {}).get("text") or "").strip()
        try:
            result = await module.dry_run(group, extra_context=extra)
        except Exception as e:
            return error_response(f"试跑失败: {e}")
        return json_response(result)

    async def _api_personas(self):
        """Return selectable persona names for fixed-persona features."""
        from astrbot.api.web import json_response

        manager = getattr(self.context, "persona_manager", None)
        items = []
        for persona in getattr(manager, "personas_v3", []) or []:
            if not isinstance(persona, dict):
                continue
            name = str(persona.get("name") or "").strip()
            if not name:
                continue
            items.append({"id": name, "name": name})
        items.sort(key=lambda item: item["name"])
        return json_response({"ok": True, "personas": items})

    async def _api_image_archive(self):
        """List archived images and their storage statistics."""
        from astrbot.api.web import error_response, json_response, request

        archive = getattr(self, "image_archive", None)
        if archive is None:
            return error_response("图片库未启用")
        archive.prune()
        try:
            query = str(request.query.get("q", "") or "").strip()
            filter_value = str(request.query.get("filter", "all") or "all").strip()
            offset = max(0, int(request.query.get("offset", 0) or 0))
            limit = max(1, min(48, int(request.query.get("limit", 12) or 12)))
        except Exception:
            query = ""
            filter_value = "all"
            offset = 0
            limit = 12
        entries = [entry for entry in archive.entries if entry.status == "active"]
        if query:
            entries = [
                entry
                for entry in entries
                if query in entry.description or query in " ".join(entry.tags)
            ]
        if filter_value == "sendable":
            entries = [entry for entry in entries if entry.sendable]
        elif filter_value == "unsendable":
            entries = [entry for entry in entries if not entry.sendable]
        elif filter_value == "favorite":
            entries = [entry for entry in entries if entry.favorite]
        elif filter_value.startswith("tag:"):
            tag = filter_value[4:].strip().lower()
            if tag:
                if tag in {"r18", "敏感"}:
                    entries = [
                        entry for entry in entries if send_block_reason(entry.tags)
                    ]
                else:
                    entries = [
                        entry
                        for entry in entries
                        if any(tag in str(item).lower() for item in (entry.tags or []))
                    ]
        entries.sort(
            key=lambda entry: (entry.favorite, entry.score, entry.created_at),
            reverse=True,
        )
        total = len(entries)
        payload = []
        for entry in entries[offset : offset + limit]:
            payload.append(
                {
                    "thumb": archive.thumb_data_url(entry),
                    "id": entry.id,
                    "description": entry.description,
                    "tags": entry.tags,
                    "score": round(entry.score, 2),
                    "favorite": entry.favorite,
                    "sendable": bool(entry.sendable),
                    "blocked_tag": send_block_reason(entry.tags),
                    "analysis_pending": entry.analysis_pending,
                    "analysis_attempts": entry.analysis_attempts,
                    "analysis_error": entry.analysis_error,
                    "analysis_max_retries": self.image_archive_max_analysis_retries,
                    "animated": str(entry.file).lower().endswith(".gif"),
                    "size_kb": max(1, entry.size // 1024),
                    "created": datetime.fromtimestamp(entry.created_at).strftime(
                        "%m-%d %H:%M"
                    ),
                    "chat": ("群 " if entry.is_group else "私聊 ")
                    + " / ".join(entry.chat_ids or [entry.chat_id]),
                    "sender": entry.sender_name or entry.sender_id,
                }
            )
        return json_response(
            {
                "ok": True,
                "stats": archive.stats(),
                "entries": payload,
                "total": total,
                "offset": offset,
                "limit": limit,
                "query": query,
            }
        )

    async def _api_image_archive_file(self):
        """Serve one archived image to the authenticated plugin page."""
        import mimetypes

        from astrbot.api.web import error_response, file_response, request

        archive = getattr(self, "image_archive", None)
        if archive is None:
            return error_response("图片库未启用")
        try:
            entry_id = str(request.query.get("id", "") or "").strip()
        except Exception:
            entry_id = ""
        entry = next(
            (
                item
                for item in archive.entries
                if item.id == entry_id and item.status == "active"
            ),
            None,
        )
        if entry is None:
            return error_response("没有找到这张图片")
        path = (archive.root / entry.file).resolve()
        try:
            path.relative_to(archive.root.resolve())
        except ValueError:
            return error_response("图片路径不合法")
        if not path.is_file():
            return error_response("图片文件不存在")
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return file_response(str(path), content_type=content_type)

    async def _api_image_archive_preview(self):
        """Return one enlarged image for the plugin-page lightbox."""
        from astrbot.api.web import error_response, json_response, request

        archive = getattr(self, "image_archive", None)
        if archive is None:
            return error_response("图片库未启用")
        entry_id = str(request.query.get("id", "") or "").strip()
        entry = next(
            (
                item
                for item in archive.entries
                if item.id == entry_id and item.status == "active"
            ),
            None,
        )
        if entry is None:
            return error_response("没有找到这张图片")
        animated = str(request.query.get("animated", "") or "").strip() in {
            "1",
            "true",
            "yes",
        }
        preview = archive.preview_data_url(entry, animated=animated)
        if not preview:
            return error_response("图片文件不存在或无法预览")
        return json_response(
            {
                "image": preview,
                "description": entry.description,
                "tags": entry.tags,
                "created": datetime.fromtimestamp(entry.created_at).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                "sender": entry.sender_name,
                "chat": entry.chat_id,
            }
        )

    async def _api_image_archive_rate(self):
        """Retry vision analysis for one archived image."""
        from astrbot.api.web import error_response, json_response, request

        archive = getattr(self, "image_archive", None)
        if archive is None:
            return error_response("图片库未启用")
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        entry_id = str((payload or {}).get("id") or "").strip()
        entry = next(
            (
                item
                for item in archive.entries
                if item.id == entry_id and item.status == "active"
            ),
            None,
        )
        if entry is None:
            return error_response("没有找到这张图片")
        path = archive.root / entry.file
        if not path.is_file():
            return error_response("图片文件不存在")
        try:
            provider = await self.context.get_using_provider_async()
        except Exception:
            provider = None
        persona_prompt = ""
        try:
            if entry.umo:
                persona_prompt = await self._resolve_persona_prompt(entry.umo)
        except Exception:
            pass
        rating = await self._rate_archive_image(provider, str(path), persona_prompt)
        if not str(rating.get("description") or "").strip():
            error = str(rating.get("error") or "")
            archive.mark_analysis_failed(entry.id, reason=error)
            return error_response(
                "识图失败，已留在图库等待手动重试" + (f"：{error}" if error else "")
            )
        archive.update_analysis(
            entry.id,
            description=str(rating.get("description") or ""),
            tags=list(rating.get("tags") or []),
            score=float(rating.get("score") or 0.0),
            favorite=rating.get("favorite"),
        )
        return json_response({"ok": True, "description": rating.get("description")})

    async def _api_image_archive_delete(self):
        """Delete one archived image by id."""
        from astrbot.api.web import error_response, json_response, request

        archive = getattr(self, "image_archive", None)
        if archive is None:
            return error_response("图片库未启用")
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        entry_id = str((payload or {}).get("id") or "").strip()
        if not entry_id:
            return error_response("缺少 id")
        if not archive.remove(entry_id):
            return error_response("没有找到这张图片")
        return json_response({"ok": True})

    async def _api_image_archive_sendable(self):
        """Allow or forbid sending one archived image as a meme."""
        from astrbot.api.web import error_response, json_response, request

        archive = getattr(self, "image_archive", None)
        if archive is None:
            return error_response("图片库未启用")
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        payload = payload or {}
        entry_id = str(payload.get("id") or "").strip()
        sendable = bool(payload.get("sendable"))
        if not entry_id:
            return error_response("缺少 id")
        entry = next(
            (
                item
                for item in archive.entries
                if item.id == entry_id and item.status == "active"
            ),
            None,
        )
        if entry is None:
            return error_response("没有找到这张图片")
        if not archive.set_sendable(entry_id, sendable):
            return error_response("更新失败")
        return json_response({"ok": True, "sendable": sendable})

    async def _api_notebook(self):
        """Return the persona's notebook as a node/edge network."""
        from astrbot.api.web import json_response

        empty = {"enabled": False, "total": 0, "nodes": [], "edges": [], "counts": {}}
        notebook = getattr(self, "notebook", None)
        curiosity = getattr(self, "curiosity_module", None)
        if notebook is None:
            if curiosity is not None:
                empty["pending_curiosity"] = len(curiosity.store.pending(100))
                empty["curiosity_known"] = len(curiosity.store.knowledge(100))
            return json_response(empty)
        try:
            payload = notebook.graph()
            if curiosity is not None:
                payload["pending_curiosity"] = len(curiosity.store.pending(100))
                payload["curiosity_known"] = len(curiosity.store.knowledge(100))
            return json_response(payload)
        except Exception as e:
            logger.warning(f"[记忆本] 网络数据生成失败: {e}")
            return json_response(empty)

    async def _api_notebook_delete(self):
        """Delete one notebook entry."""
        from astrbot.api.web import error_response, json_response, request

        notebook = getattr(self, "notebook", None)
        if notebook is None:
            return error_response("记忆本未启用")
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        text = str((payload or {}).get("text") or "").strip()
        if not text:
            return error_response("缺少 text")
        if not notebook.remove(text):
            return error_response("没有找到这条记忆")

        # Manual entries are replayed from config on every rebuild. Removing
        # only notebook.json would make them come back on the next reload.
        try:
            kept = []
            changed = False
            for raw in self._cfg_list("notebook_entries", []):
                value = str(raw or "").strip()
                _kind, _, content = value.partition("|")
                content = content.strip() if content else value
                if content == text:
                    changed = True
                    continue
                kept.append(value)
            if changed:
                self._set_cfg("notebook_entries", kept)
                await asyncio.to_thread(self.config.save_config)
        except Exception as exc:
            logger.warning(f"[记忆本] 同步手动条目删除失败: {exc}")
        return json_response({"ok": True})

    async def _api_notebook_update(self):
        """Rewrite one notebook entry."""
        from astrbot.api.web import error_response, json_response, request

        notebook = getattr(self, "notebook", None)
        if notebook is None:
            return error_response("记忆本未启用")
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        text = str((payload or {}).get("text") or "").strip()
        new_text = str((payload or {}).get("new_text") or "").strip()
        if not text or not new_text:
            return error_response("缺少 text 或 new_text")
        if not notebook.rename(text, new_text):
            return error_response("没有找到这条记忆")

        # Keep a manual entry's config source in sync with the rename; the
        # rebuild path would otherwise re-add the old text next to the new one.
        try:
            updated = []
            changed = False
            for raw in self._cfg_list("notebook_entries", []):
                value = str(raw or "").strip()
                kind, _, content = value.partition("|")
                content = content.strip() if content else value
                if content == text:
                    kind = kind.strip()
                    updated.append(f"{kind}|{new_text}" if kind else new_text)
                    changed = True
                else:
                    updated.append(value)
            if changed:
                self._set_cfg("notebook_entries", updated)
                await asyncio.to_thread(self.config.save_config)
        except Exception as exc:
            logger.warning(f"[记忆本] 同步手动条目改名失败: {exc}")
        return json_response({"ok": True})

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

        # 跨群见闻参数需在保存后立即重建，否则要重载插件才生效
        try:
            self._apply_cross_group_config()
        except Exception as e:
            logger.warning(f"[跨群见闻] 重建配置失败: {e}")

        # 资料片同理：开关一变就重建，页面状态和实际行为才对得上
        try:
            self._apply_dlc_config()
        except Exception as e:
            logger.warning(f"[资料片] 重建失败: {e}")

        logger.info(f"[插件页] 配置已保存 applied={applied} skipped={skipped}")
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
                    self._last_bot_reply_at.pop(_reset_chat_key, None)
                    self._life_sight_last_note.clear()
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

            # Commands are owned by AstrBot command handlers or other plugins.
            # Running this before participation/DLC hooks keeps the advertised
            # command filter from becoming a dead configuration option.
            if (
                not is_private_chat
                and self.enable_command_filter
                and self._is_command_message(event)
            ):
                if self.debug_mode:
                    logger.info("[指令过滤] 命令消息跳过参与判断")
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

            # 跨群见闻：先记录这条消息，供其他群回复时参考（不改变本群处理流程）
            if not is_private_chat:
                await self.cross_group_sight.observe(event)

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

    async def _get_official_group_context_records(
        self, event: AstrMessageEvent
    ) -> list[str]:
        """Read buffered group messages from AstrBot's built-in context.

        Args:
            event: Current group message event whose record marks the boundary.

        Returns:
            Messages buffered before the current event, or an empty list when the
            built-in group context is unavailable or not enabled.
        """
        try:
            star_metadata = self.context.get_registered_star("astrbot")
            builtin_main = getattr(star_metadata, "star_cls", None)
            group_context = getattr(builtin_main, "group_chat_context", None)
            if group_context is None:
                return []

            umo = event.unified_msg_origin
            lock_getter = getattr(group_context, "_get_lock", None)
            if not callable(lock_getter):
                return []

            async with lock_getter(umo):
                raw_records = getattr(group_context, "raw_records", None)
                record_ids_by_umo = getattr(group_context, "_record_ids", None)
                if not hasattr(raw_records, "get") or not hasattr(
                    record_ids_by_umo, "get"
                ):
                    return []

                records = list(raw_records.get(umo, ()))
                record_ids = list(record_ids_by_umo.get(umo, ()))
                if not records:
                    return []

                record_id = event.get_extra("_group_context_record_id", None)
                prompt_index = event.get_extra("_group_context_raw_idx", -1)
                if isinstance(record_id, str) and record_id in record_ids:
                    prompt_index = record_ids.index(record_id)
                if not isinstance(prompt_index, int) or not (
                    0 < prompt_index <= len(records)
                ):
                    return []
                # 只取紧邻当前消息的一小段：判断阶段不需要整份群聊缓冲，
                # 取太多会和平台历史重复，还稀释对当前消息的注意力。
                return records[max(0, prompt_index - 20) : prompt_index]
        except Exception as error:
            if self.debug_mode:
                logger.debug(f"[DecisionAI] 读取 AstrBot 官方群聊上下文失败: {error}")
            return []

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
        if is_private:
            try:
                _image_components = [
                    comp
                    for comp in (
                        getattr(getattr(event, "message_obj", None), "message", [])
                        or []
                    )
                    if isinstance(comp, Image)
                ]
                if _image_components:
                    _refs = await ImageHandler._extract_image_urls(
                        _image_components, resolve_paths=False
                    )
                    if _refs:
                        _now = time.time()
                        _kept = [
                            (_ts, _url)
                            for _ts, _url in self._recent_private_images.get(
                                chat_id, []
                            )
                            if _now - _ts <= 180
                        ]
                        _kept.extend((_now, str(_url)) for _url in _refs if _url)
                        self._recent_private_images[chat_id] = _kept[-6:]
            except Exception as _img_err:
                logger.debug(f"[私聊图片] 记录失败: {_img_err}")
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
        bot_id = str(event.get_self_id() or "")
        reply_target_id = MessageProcessor.get_reply_target_id(event)
        is_reply_to_bot = bool(bot_id and reply_target_id and reply_target_id == bot_id)
        is_reply_to_other = bool(reply_target_id and reply_target_id != bot_id)
        # 戳一戳不是命令，按普通消息处理，由参与判断自己决定理不理。
        is_explicitly_addressed = bool(is_at_message or is_reply_to_bot)
        # Keyword matches enter the ambient decision flow but are not proof that
        # the sender is talking to the bot.
        is_direct_address = bool(is_explicitly_addressed or has_trigger_keyword)
        unaddressed_group_message = not is_private and not is_direct_address

        # 资料片：好奇心不区分有没有被叫到，看到不懂的词就记下来。
        if not is_private:
            self._schedule_curiosity(event)

        # DLC observation only: the proposal itself is offered after the base
        # decides to stay silent, so it can never race a formal reply.
        if unaddressed_group_message:
            await self._observe_feature_event(event, is_private, raw_chat_id)
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

        if is_private and not image_urls:
            _now = time.time()
            _recent = [
                _url
                for _ts, _url in self._recent_private_images.get(chat_id, [])
                if _now - _ts <= 180 and _url
            ]
            if _recent:
                image_urls = list(dict.fromkeys(_recent))[
                    -self.image_context_max_images :
                ]
                logger.info(f"[私聊图片] 补充 {len(image_urls)} 张最近图片")
        if image_urls and self.image_archive_scope == "all":
            self._schedule_image_archive(event, image_urls, chat_id, is_private)

        # 黑话要从群里的全部消息里攒，而不是只从它回过的那几条里攒。
        if not is_private:
            self._collect_jargon_line(event, message_text)

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
                        if not _urls:
                            continue
                        # In private chat, a pure image often gets no reply and
                        # is marked observed. Keep that image available for a
                        # short follow-up such as "把我刚发的那张转给我".
                        try:
                            _ts = float(
                                _cached.get("message_timestamp")
                                or _cached.get("timestamp")
                                or 0.0
                            )
                        except (TypeError, ValueError):
                            _ts = 0.0
                        _recent_context = bool(
                            not image_urls
                            and _ts
                            and time.time() - _ts
                            <= self.image_context_window_minutes * 60
                        )
                        if (
                            _cached.get("decision_state") != "observed"
                            or _recent_context
                        ):
                            merged_image_urls.extend(_urls)
                if merged_image_urls:
                    _seen_urls = set()
                    _dedup_urls = []
                    for _u in merged_image_urls:
                        if _u and _u not in _seen_urls:
                            _seen_urls.add(_u)
                            _dedup_urls.append(_u)
                    merged_image_urls = _dedup_urls
                    if (
                        not image_urls
                        and len(merged_image_urls) > self.image_context_max_images
                    ):
                        merged_image_urls = merged_image_urls[
                            -self.image_context_max_images :
                        ]
        except Exception as e:
            logger.warning(f"[图片缓存] 合并图片URL失败: {e}")

        try:
            _archive_context = self._archive_context_images(
                self._archive_chat_key(event), message_text, image_urls
            )
            if _archive_context:
                merged_image_urls = list(
                    dict.fromkeys([*merged_image_urls, *_archive_context])
                )
        except Exception as e:
            logger.debug(f"[图片库] 补充上下文失败: {e}")

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
            and self.enable_private_speculative_reply
            and not self._followup_enabled(True)
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
                        merged_image_urls,
                        self._image_tools()
                        + self._followup_tools(is_private=True)
                        + self._meme_bridge_tools(is_private=True),
                    )
                )
                logger.info(
                    "[Private Speculative] Started buffered formal reply before the batch window"
                )

        # Smart 模式下先按 arrival_seq 等待更早消息，确保只有 anchor 进入 AI 决策
        smart_claim = None
        if is_private:
            if use_smart_batch:
                # Adaptive window: reset the quiet timer on every new arrival,
                # but never extend past the configured hard deadline.
                await self._wait_private_adaptive_window(chat_id, processing_id)
            smart_wait_loops = 2
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

            claim_delay = 0.0 if is_private else self.smart_concurrent_claim_delay
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
            # 判断只需要最近的一段对话：完整历史留给回复阶段，
            # 避免每次判断都把几百条记录塞进 prompt（稀释注意力且烧 token）。
            if not is_private and history_messages and len(history_messages) > 30:
                try:
                    decision_context = await self._format_ai_context(
                        history_messages[-30:],
                        current_message_for_ai,
                        event.get_self_id(),
                    )
                except Exception as _trim_err:
                    logger.debug(
                        f"[决策AI] 短上下文构建失败，保留完整上下文: {_trim_err}"
                    )
            if use_smart_batch and smart_batch_messages:
                try:
                    decision_context = await self._format_ai_context(
                        history_messages[-30:] if not is_private else history_messages,
                        current_message_for_ai,
                        event.get_self_id(),
                        window_msgs=smart_batch_messages,
                        poke_notice=poke_notice_text,
                    )
                except Exception as smart_ctx_err:
                    logger.warning(
                        f"[Smart并发] 决策阶段重建批次上下文失败，回退原上下文: {smart_ctx_err}"
                    )

            if not is_private:
                official_group_context_records = (
                    await self._get_official_group_context_records(event)
                )
                if official_group_context_records:
                    decision_context = (
                        "[AstrBot 官方群聊上下文]\n"
                        "以下是当前消息之前的官方群聊消息，只用于理解当前消息的语境；"
                        "不要因为这些消息本身而回复，也不要把它们当成当前消息。\n"
                        + "\n".join(official_group_context_records)
                        + "\n\n"
                        + decision_context
                    )
                    if self.debug_mode:
                        logger.info(
                            f"[DecisionAI] 已加入 {len(official_group_context_records)} 条官方群聊上下文"
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
            if not is_private and history_messages:
                # 群里没有@和引用时，每条消息都像"公开话题"，模型只能靠猜谁在跟谁说话。
                # 这里把可计算的话轮事实摆到它面前：发言顺序、这段像谁在跟谁聊、它自己刚说过什么。
                try:
                    _bot_id = str(event.get_self_id() or "")
                    _recent = [
                        _m
                        for _m in history_messages[-8:]
                        if getattr(_m, "sender", None) is not None
                    ]
                    if _recent:
                        _seq: list[str] = []
                        _is_bot_seq: list[bool] = []
                        _bot_lines: list[str] = []
                        for _m in _recent:
                            _sid = str(getattr(_m.sender, "user_id", "") or "")
                            _ts = getattr(_m, "timestamp", None)
                            if _sid and _sid == _bot_id:
                                _seq.append("你")
                                _is_bot_seq.append(True)
                                _clock = (
                                    time.strftime("%H:%M", time.localtime(_ts))
                                    if _ts
                                    else ""
                                )
                                _text = (
                                    str(getattr(_m, "message_str", "") or "")
                                    .strip()
                                    .replace("\n", " ")[:18]
                                )
                                if _text:
                                    _bot_lines.append(f"{_clock}「{_text}」".strip())
                            else:
                                _name = (
                                    str(getattr(_m.sender, "nickname", "") or "")
                                    or "群友"
                                )[:8]
                                _seq.append(_name)
                                _is_bot_seq.append(False)
                        _humans = [
                            _name
                            for _name, _flag in zip(_seq, _is_bot_seq)
                            if not _flag
                        ]
                        _uniq = list(dict.fromkeys(_humans))
                        _switches = sum(1 for _a, _b in zip(_seq, _seq[1:]) if _a != _b)
                        if len(_uniq) == 2 and _switches >= len(_seq) - 2:
                            _shape = f"{_uniq[0]} 和 {_uniq[1]} 在互相接话"
                        elif len(_uniq) == 1 and any(_is_bot_seq):
                            _shape = f"刚才你和 {_uniq[0]} 说过话，现在是他自己在说"
                        elif len(_uniq) == 1:
                            _shape = f"只有 {_uniq[0]} 在说，你没参与"
                        elif len(_uniq) == 0:
                            _shape = "这几条都是你自己说的"
                        else:
                            _shape = "多个群友在聊，没有固定对象"
                        _floor = (
                            f"\n\n<话轮事实>\n最近 {len(_recent)} 条顺序："
                            + " → ".join(_seq)
                            + f"\n这段像：{_shape}"
                        )
                        if _bot_lines:
                            _floor += "；你最后说过：" + " / ".join(_bot_lines[-2:])
                        else:
                            _floor += "；你最近没发过言"
                        decision_context_for_ai += _floor + "\n</话轮事实>"
                except Exception as _floor_err:
                    logger.debug(f"[话轮事实] 构造失败: {_floor_err}")
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
            # 话轮节制硬底线：最近几条里已经说过话、又没被点名，就不再主动插进别人的对话。
            # 提示词只能做到"多数时候遵守"，这里按事实兜底，去掉"有时候"。
            if (
                not is_private
                and decision_result.reply
                and decision_result.participation in {"open", "side"}
                and not is_explicitly_addressed
                and history_messages
            ):
                _self_id_str = str(event.get_self_id() or "")
                _spoke_recently = any(
                    str(getattr(getattr(_m, "sender", None), "user_id", "") or "")
                    == _self_id_str
                    for _m in history_messages[-3:]
                )
                # 公开提问值得接，不该被话轮护栏误伤；只有普通聊天才让话头。
                _looks_like_question = bool(
                    re.search(
                        r"[?？]|吗|什么|怎么|哪个|哪儿|多少|有没有|是不是",
                        original_message_text or "",
                    )
                )
                if _spoke_recently and not _looks_like_question:
                    decision_result = decision_result.with_reply(
                        False, source="policy", reason_code="none"
                    )
                    logger.info(
                        "[话轮节制] 最近 3 条内已经发过言且没被点名，放弃主动参与"
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
            if not is_private and unaddressed_group_message:
                try:
                    await self._maybe_propose_feature(
                        event,
                        is_private,
                        raw_chat_id,
                        decision_ai_failed=decision_ai_failed,
                        decision_source=decision_result.source,
                    )
                except Exception as exc:
                    logger.warning(f"[Feature] Proposal gate failed: {exc}")
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

    async def _build_background_block(
        self, event: AstrMessageEvent, is_private: bool
    ) -> str:
        """Assemble what the persona knows besides this conversation.

        Runs inside the shared context preparation so the buffered speculative
        draft and the formal reply see exactly the same background.

        Args:
            event: Message event being answered.
            is_private: Whether the conversation is private.

        Returns:
            The assembled block, or an empty string when nothing applies.
        """
        blocks: list[str] = []

        # 记忆本：只有当前消息碰到它在意的东西时才带。群内黑话默认只在
        # 本群可见；开启跨群引用后也要带“来自其他群”的出处标记。
        try:
            notebook = getattr(self, "notebook", None)
            if notebook is not None:
                group_id = "" if is_private else str(event.get_group_id() or "")
                include_other_groups = (
                    bool(getattr(self, "notebook_cross_group_jargon", False))
                    and not is_private
                )
                notebook_block = notebook.block_for(
                    event.get_message_str() or "",
                    group_id,
                    include_other_groups=include_other_groups,
                )
                if notebook_block:
                    blocks.append(notebook_block)
        except Exception as e:
            logger.warning(f"[记忆本] 注入失败: {e}")

        # 好奇心知识：独立于记忆网络，只在当前消息相关时作背景。
        try:
            curiosity = getattr(self, "curiosity_module", None)
            if curiosity is not None:
                curiosity_block = curiosity.knowledge_block(
                    event.get_message_str() or ""
                )
                if curiosity_block:
                    blocks.append(curiosity_block)
        except Exception as e:
            logger.warning(f"[好奇心] 知识注入失败: {e}")

        # 真实状态：只在对方问“最近在做什么/看什么”时带，且只作背景。
        try:
            life_sight = getattr(self, "life_sight", None)
            message_text = event.get_message_str() or ""
            if life_sight is not None and life_sight.looks_like_activity_question(
                message_text
            ):
                content_question = life_sight.looks_like_content_question(message_text)
                lines = [
                    line
                    for line in life_sight.state_lines(
                        include_title=content_question,
                        fresh_only=not content_question,
                    )
                    if not line.startswith("· 现在是")
                ]
                note_key = str(event.unified_msg_origin)
                signature = "\n".join(lines)
                now_ts = time.time()
                last_note = self._life_sight_last_note.get(note_key)
                if (
                    not content_question
                    and last_note is not None
                    and last_note[0] == signature
                    and now_ts - last_note[1] < 600
                ):
                    lines = []
                if lines:
                    blocks.append(
                        "[真实状态 · 只作背景]\n"
                        + "\n".join(lines)
                        + "\n（只用于回答当前问题；对方没追问具体内容就不要主动报视频标题；"
                        "同一段见闻不要反复提。）"
                    )
                    self._life_sight_last_note[note_key] = (signature, now_ts)
        except Exception as e:
            logger.warning(f"[真实状态] 注入失败: {e}")

        # 跨群见闻：只对群聊生效。
        if not is_private:
            try:
                sightings = await self.cross_group_sight.collect(event)
                if sightings:
                    blocks.append(self.cross_group_sight.render(sightings))
                    if self.enable_cross_group_memory_recall:
                        mode, version = MemoryInjector.resolve_mode(
                            self.context,
                            self.memory_plugin_mode,
                            self.livingmemory_version,
                        )
                        if mode:
                            memory_text = await self.cross_group_sight.recall_memories(
                                event,
                                sightings,
                                mode=mode,
                                version=version,
                                top_k=self.livingmemory_top_k,
                                persona_compat_mode=(
                                    self.livingmemory_persona_compat_mode
                                ),
                            )
                            if memory_text:
                                blocks.append(memory_text)
            except Exception as e:
                logger.warning(f"[跨群见闻] 注入失败: {e}")

        return "\n\n".join(blocks)

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

        try:
            background = await self._build_background_block(event, is_private)
            if background:
                final_message = f"{final_message}\n\n{background}"
                logger.info(f"[背景] 已注入 {len(background)} 字符")
                if self.debug_mode:
                    logger.info(f"[背景] 内容:\n{background}")
        except Exception as e:
            logger.warning(f"[背景注入] 失败: {e}")

        try:
            archive_memory = self._archive_memory_block(event, chat_id)
            if archive_memory:
                final_message = f"{final_message}\n\n{archive_memory}"
                logger.info("[图片库] 已注入自身图片记忆")
        except Exception as e:
            logger.debug(f"[图片库] 自身图片记忆注入失败: {e}")

        # 人格里某个设定容易被说成口头禅（发呆/别吵/笨蛋），把最近几次摆出来。
        if getattr(self, "enable_repeat_guard", False):
            try:
                limit = max(1, getattr(self, "repeat_guard_count", 5))
                recent = [
                    str(item.get("content") or "").strip()
                    for item in self.recent_replies_cache.get(state_chat_id, [])[
                        -limit:
                    ]
                ]
                recent = [text for text in recent if text]
                logger.info(
                    "[口头禅抑制] 缓存 %d 条：%s",
                    len(recent),
                    " | ".join(recent)[:120],
                )
                if len(recent) >= 2:
                    listed = "\n".join(f"- {text}" for text in recent)
                    final_message = (
                        f"{final_message}\n\n[你最近几次的回复]\n{listed}\n"
                        "（上面是你最近几次的回复。如果这次要表达的意思和其中某句接近，"
                        "换一种说法，不要沿用同样的措辞；同一个词不要连着反复出现。）"
                    )
            except Exception as e:
                logger.warning(f"[口头禅抑制] 失败: {e}")

        return final_message, state_chat_id

    async def _generate_speculative_private_reply(
        self,
        event: AstrMessageEvent,
        formatted_context: str,
        platform_name: str,
        chat_id: str,
        history_messages: list,
        image_urls: list | None = None,
        extra_tools: list | None = None,
    ) -> str | None:
        """Generate a buffered direct private text reply without executing tools.

        Args:
            event: Anchor event whose response remains uncommitted.
            formatted_context: Anchor-only formal reply context.
            platform_name: Platform name for scoped memory lookup.
            chat_id: Raw private chat identifier.
            history_messages: History snapshot used while building the prompt.
            image_urls: Images attached to this private turn.
            extra_tools: Additional callable tools for this draft.

        Returns:
            Plain reply text, or None when the fast path is unavailable.
        """
        if image_urls:
            event.set_extra("_persona_turn_image_urls", list(image_urls))
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
            image_urls=image_urls,
            extra_tools=extra_tools,
        )

    # ============================================================
    # 生成并发送回复
    # ============================================================

    async def _wait_private_adaptive_window(
        self, chat_id: str, processing_id: str
    ) -> None:
        """Wait for a private burst to go quiet, bounded by a hard deadline.

        Args:
            chat_id: Scoped private conversation key.
            processing_id: Processing ID of the anchor message.
        """
        started = time.monotonic()
        max_wait = max(0.0, float(self.private_batch_wait_ms) / 1000)
        quiet_wait = max(0.1, float(self.private_batch_quiet_ms) / 1000)
        last_arrival = await SmartConcurrentManager.latest_arrival(chat_id)
        quiet_until = started + quiet_wait

        while True:
            if await SmartConcurrentManager.is_consumed(processing_id):
                return
            if await SmartConcurrentManager.has_earlier_pending(chat_id, processing_id):
                await asyncio.sleep(0.1)
                continue

            now = time.monotonic()
            latest = await SmartConcurrentManager.latest_arrival(chat_id)
            if latest > last_arrival:
                last_arrival = latest
                quiet_until = now + quiet_wait

            if now >= quiet_until or now - started >= max_wait:
                return
            await asyncio.sleep(
                max(0.02, min(0.1, quiet_until - now, started + max_wait - now))
            )

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

        if image_urls:
            event.set_extra("_persona_turn_image_urls", list(image_urls))
        if image_urls and self.image_archive_scope == "replied":
            self._schedule_image_archive(event, image_urls, chat_id, is_private)

        if precomputed_reply_text is None:
            final_message, state_chat_id = await self._prepare_reply_context(
                event, formatted_context, platform_name, is_private, chat_id
            )
        else:
            final_message = formatted_context
            state_chat_id = ProbabilityManager.get_chat_key(
                platform_name, is_private, chat_id
            )

        if self._followup_enabled(is_private):
            final_message += (
                "\n\n[回复节奏] 一句话说完就够了就正常结束；"
                "如果确实还有半句自然想补的，可以交给 queue_followup，"
                "等主回复发出后再接着发。不要为了凑第二条硬拆一句话。"
            )
        if self._meme_bridge_enabled(is_private):
            final_message += (
                "\n\n[表情] 需要配图时可以用 queue_meme 选一张具体的图，"
                "它会并进当前回复；如果只想发表情，可以只调用 queue_meme，"
                "不要再写正文。"
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

                # 背景（状态/生活/跨群）已在 _prepare_reply_context 里统一拼好，
                # 这里不再重复注入。
                cross_group_hint = ""

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
                    cross_group_hint=cross_group_hint,
                    extra_tools=self._image_tools()
                    + self._lookup_tools()
                    + self._followup_tools(is_private)
                    + self._meme_bridge_tools(is_private),
                )
        except Exception as e:
            ai_error_flag = True
            logger.error(f"生成AI回复时发生未捕获异常: {e}", exc_info=True)
            reply_result = event.plain_result(f"生成回复时发生错误: {str(e)}")

        try:
            ai_error_flag = ai_error_flag or bool(
                event.get_extra("_persona_presence_reply_error", False)
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

            followups = list(event.get_extra("_persona_followups") or [])
            if (
                not followups
                and self._followup_enabled(is_private)
                and not ai_error_flag
                and reply_result is not None
                and hasattr(reply_result, "chain")
            ):
                text_components = [
                    comp for comp in reply_result.chain if hasattr(comp, "text")
                ]
                if len(text_components) == len(reply_result.chain) == 1:
                    parts = self._split_natural_bubbles(
                        reply_text, max_extra=self.followup_max_messages
                    )
                    if len(parts) > 1:
                        text_components[0].text = parts[0]
                        reply_text = parts[0]
                        followups = parts[1:]
                        logger.info(f"[多气泡] 按自然停顿拆成 {len(parts)} 条")
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

            if followups and not ai_error_flag:
                asyncio.create_task(
                    self._deliver_followup_messages(event, followups, reply_text)
                )

            if self.debug_mode:
                logger.info("【步骤13.9】回复已通过yield发送")
        else:
            if self.debug_mode:
                logger.info("【步骤13.9】跳过发送回复（重复消息已拦截），继续后续流程")

        # 记录已发送的回复（用于后续去重检查）
        if reply_text and not is_provider_request and not is_duplicate_blocked:
            self._notebook_observe(event, reply_text)
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

    @staticmethod
    def _split_natural_bubbles(text: str, max_extra: int = 1) -> list[str]:
        """Split a text reply at natural sentence boundaries.

        Args:
            text: Full reply text.
            max_extra: Maximum number of follow-up bubbles after the first.

        Returns:
            One or more bubbles, with the tail merged when it exceeds the cap.
        """
        clean = str(text or "").strip()
        if not clean or max_extra <= 0:
            return [clean] if clean else []
        raw_parts = re.split(r"(?<=[。！？!?])", clean)
        parts = [part.strip() for part in raw_parts if part.strip()]
        if len(parts) <= 1:
            return [clean]
        head = parts[:1]
        tail = parts[1:]
        if len(tail) > max_extra:
            tail = [" ".join(tail[: len(tail) - max_extra + 1])] + tail[
                len(tail) - max_extra + 1 :
            ]
        return head + tail

    async def _deliver_followup_messages(
        self, event: AstrMessageEvent, texts: list[str], main_text: str = ""
    ) -> None:
        """Send queued follow-up bubbles after the main reply.

        Args:
            event: The turn that produced the follow-ups.
            texts: Ordered follow-up texts.
            main_text: Main reply text, used to drop accidental duplicates.
        """
        low = min(self.followup_min_delay_ms, self.followup_max_delay_ms)
        high = max(self.followup_min_delay_ms, self.followup_max_delay_ms)
        main_clean = str(main_text or "").strip()
        for text in texts:
            cleaned = str(text or "").strip()
            if not cleaned or cleaned == main_clean:
                continue
            delay = random.randint(low, high) / 1000 if high > 0 else 0
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await self.context.send_message(
                    event.unified_msg_origin, MessageChain([Plain(cleaned)])
                )
                logger.info(f"[多气泡] 补充发送：{cleaned[:60]}")
                try:
                    await ContextManager.save_bot_message(event, cleaned, self.context)
                except Exception as save_err:
                    logger.warning(f"[多气泡] 补充保存失败: {save_err}")
            except Exception as send_err:
                logger.warning(f"[多气泡] 补充发送失败: {send_err}")

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
    # 消息发送后钩子（保存本轮对话）
    # ============================================================

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """消息发送后把本轮对话写入官方历史。

        必须定义在 main.py：AstrBot 只把 handler_module_path 等于插件主模块的
        handler 绑定到插件实例并派发；写在 utils 模块里的同名钩子会被
        「未激活插件」过滤掉，导致 AI 回复永远不入库。

        Args:
            event: 已完成发送的消息事件。
        """
        await SaveMixin.after_message_sent(self, event)

    @filter.command("persona_clear_image_cache")
    async def persona_clear_image_cache(self, event: AstrMessageEvent):
        """清除本地图片描述缓存并重启 AstrBot。

        与 after_message_sent 同理，指令必须注册在 main.py 才能被 AstrBot 派发。

        Args:
            event: 触发指令的消息事件。

        Yields:
            执行过程中的提示消息。
        """
        async for result in CommandMixin.persona_clear_image_cache(self, event):
            yield result

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
            #    框架（TOOL_CALL_PROMPT 等）与第三方插件只会追加，不会覆盖。
            #    场景是事实上下文，放在 system 层比放在用户消息末尾更难被忽略。
            try:
                if event.is_private_chat():
                    scene = "（私聊。对面只有对方一个人。）"
                else:
                    sender = str(event.get_sender_name() or "对方")
                    scene = f"（群聊。刚才说话的是「{sender}」，周围还有人。）"
                req.system_prompt = (req.system_prompt or "").rstrip() + "\n\n" + scene
            except Exception as scene_err:
                logger.debug(f"[场景] 注入 system prompt 失败: {scene_err}")

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

            if self._meme_bridge_enabled(event.is_private_chat()):
                remove_tool = getattr(req.func_tool, "remove_tool", None)
                if callable(remove_tool):
                    for tool_name in ("send_meme", "search_memes"):
                        try:
                            remove_tool(tool_name)
                        except Exception:
                            pass

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
        # 模型偶尔会把 <reply> 标签、&nbsp; 或代码块带进正文，发出去就是乱码。
        try:
            raw = str(getattr(response, "completion_text", "") or "")
            if raw:
                cleaned = AIResponseFilter.sanitize_reply_text(raw)
                if self.reply_ellipsis_style != "keep":
                    cleaned = AIResponseFilter.normalize_ellipsis(
                        cleaned, self.reply_ellipsis_style
                    )
                if cleaned != raw:
                    # completion_text 的 setter 会同步替换 result_chain 里的纯文本段
                    response.completion_text = cleaned
                    logger.info("[回复清洗] 去掉了模型带出的标签或转义符")
        except Exception as e:
            logger.warning(f"[回复清洗] 失败: {e}")

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
            queued_meme_paths = list(event.get_extra("_persona_queued_memes") or [])
            if queued_meme_paths:
                if result is None:
                    result = event.make_result()
                    event.set_result(result)
                if not hasattr(result, "chain"):
                    return
                chain = list(result.chain or [])
                text_only = "".join(
                    self._coerce_component_text(getattr(comp, "text", None))
                    for comp in chain
                ).strip()
                if not text_only or not re.sub(
                    r"[\s.。…·!！?？,，、~～\-—]+", "", text_only
                ):
                    chain = [comp for comp in chain if not hasattr(comp, "text")]
                for path in queued_meme_paths:
                    try:
                        chain.append(Image.fromFileSystem(path))
                    except Exception as image_err:
                        logger.warning(f"[表情桥接] 图片加载失败: {image_err}")
                result.chain = chain
                event.set_extra("_persona_queued_memes", None)
                logger.info("[表情桥接] 已把选中的表情并入当前回复")
            if not result or not hasattr(result, "chain") or not result.chain:
                return

            reply_text = "".join(
                self._coerce_component_text(getattr(comp, "text", None))
                for comp in result.chain
            ).strip()

            # Short replies sometimes get emitted as the same sentence twice.
            # Collapse only an exact whole-text repetition; normal emphasis or
            # a short "哈哈" is left alone by the six-character minimum.
            text_components = [comp for comp in result.chain if hasattr(comp, "text")]
            repeated = re.fullmatch(r"(.{6,})\1", reply_text, re.DOTALL)
            collapsed_text = ""
            if repeated:
                collapsed_text = repeated.group(1).strip()
            else:
                normalized = re.sub(r"[\s.。…·!！?？,，、~～\-—]+", "", reply_text)
                half = len(normalized) // 2
                if (
                    len(normalized) >= 12
                    and len(normalized) % 2 == 0
                    and normalized[:half] == normalized[half:]
                ):
                    counted = 0
                    for index, char in enumerate(reply_text):
                        if not re.match(r"[\s.。…·!！?？,，、~～\-—]", char):
                            counted += 1
                        if counted == half:
                            collapsed_text = reply_text[: index + 1].rstrip(
                                " \n.。…·!！?？,，、~～-—"
                            )
                            break
            if collapsed_text and len(text_components) == 1:
                reply_text = collapsed_text
                text_components[0].text = reply_text
                logger.info("[回复清洗] 折叠重复的整句")

            # meme_manager's direct send_meme tool already delivered an image.
            # Models often finish the tool loop with a punctuation-only "...";
            # drop that empty closing so a pure meme stays a pure meme. This
            # guard lives here, not in the third-party plugin, so updating the
            # meme plugin cannot erase it.
            if (
                reply_text
                and event.get_extra("meme_manager_direct_send_attempted")
                and result.chain
                and all(hasattr(comp, "text") for comp in result.chain)
                and not re.sub(r"[\s.。…·!！?？,，、~～\-—]+", "", reply_text)
            ):
                logger.info("[表情包] 已主动发图，忽略仅标点的收尾正文")
                event.clear_result()
                return

            # 上游模型不可用时，框架会把错误说明当成回复发出去（而且是普通
            # 结果而不是 LLM 结果），所以这一步必须放在 is_llm_result 之前。
            if re.search(
                r"LLM 响应错误|All chat models failed|RateLimitError|prefill_cap_refused"
                r"|Error code: 4\d\d|Error code: 5\d\d",
                reply_text,
            ):
                logger.warning(
                    f"[上游错误] 拦截异常回复文本（{len(reply_text)} 字）: "
                    f"{reply_text[:80]}"
                )
                event.clear_result()
                return

            if not result.is_llm_result():
                return

            if not reply_text:
                return

            # Models occasionally echo the request scaffolding back into the
            # reply. AstrBot injects <system_reminder> blocks carrying user,
            # group and datetime info, and components use system_internal style
            # wrappers, so a leaked block must never reach the chat; drop it
            # (and any dangling closing tag) before anything else.
            stripped_reply_text = AIResponseFilter.strip_leaked_system_tags(reply_text)
            if stripped_reply_text != reply_text:
                logger.info(
                    f"[Output filter] Removed leaked system tag: "
                    f"{len(reply_text)} -> {len(stripped_reply_text)} characters"
                )
                text_components = [
                    comp for comp in result.chain if hasattr(comp, "text")
                ]
                if not stripped_reply_text:
                    event.clear_result()
                    return
                text_components[0].text = stripped_reply_text
                for comp in text_components[1:]:
                    comp.text = ""
                reply_text = stripped_reply_text

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
                        r"\s*\r?\n+\s*", " ", reply_text
                    ).strip()
                    # Chinese chat text must not keep the filler space at a line
                    # join: "……\n发呆" would otherwise reach the user as
                    # "……  发呆" with one space per collapsed line break.
                    normalized_reply_text = re.sub(
                        r"((?:\.{2,}|[。，、！？；：…~～]))\s+(?=\S)",
                        r"\1",
                        normalized_reply_text,
                    )
                    normalized_reply_text = re.sub(
                        r"(?<=[\u4e00-\u9fff）】」』”])"
                        r"\s+(?=[\u4e00-\u9fff（【「『“])",
                        "",
                        normalized_reply_text,
                    )
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

            # Mark a formal reply as about to be sent so feature proposals
            # cannot interleave with it in the same chat.
            self._last_bot_reply_at[state_chat_id] = time.time()

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
                self._private_late_night_active_until[state_chat_id] = (
                    time.time() + self._PRIVATE_LATE_NIGHT_ACTIVE_SECONDS
                )
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
