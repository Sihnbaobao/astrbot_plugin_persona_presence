"""
概率管理器模块
负责管理和动态调整参与判断概率

v1.1.0 更新：
- 🆕 支持临时概率提升（主动对话后的等待回应状态）
- 🆕 支持动态时间段概率调整（模拟人类作息）
- 🆕 支持概率硬性限制（一键简化功能，强制限制概率范围）
- 临时提升优先级高于常规提升
- 时间调整与其他功能自动配合，不冲突
- 硬性限制在所有调整的最末尾应用

作者: Sihnbaobao（重构）
版本: V1.2.3.hotfix.2
"""

import asyncio
import copy
import time
from typing import TYPE_CHECKING, Any

from astrbot.api.all import *

if TYPE_CHECKING:
    pass

# 详细日志开关（与 main.py 同款方式：单独用 if 控制）
DEBUG_MODE: bool = False


class ProbabilityManager:
    """
    概率管理器

    主要功能：
    1. 管理每个会话的参与判断概率
    2. AI回复后临时提升概率
    3. 🆕 v1.1.0: 支持主动对话后的临时概率提升
    4. 🆕 v1.1.0: 支持动态时间段概率调整
    5. 🆕 v1.1.0: 支持概率硬性限制（一键简化功能）
    6. 超时后自动恢复初始概率

    优先级顺序（从高到低）：
    1. 临时概率提升（主动对话后）
    2. 常规概率提升（回复后）
    3. 动态时间段调整
    4. 基础概率（initial_probability）
    5. 概率硬性限制（最末尾强制限制，覆盖所有调整结果）
    """

    # 使用字典保存每个聊天的概率状态
    # 格式:
    # {
    #   chat_key: {
    #       "base_probability": float,
    #       "base_until": timestamp,
    #       "base_source": str,
    #       "reply_boost_probability": float,
    #       "reply_boost_until": timestamp,
    #       "reply_boost_source": str,
    #   }
    # }
    _probability_status: dict[str, dict[str, Any]] = {}
    _lock: asyncio.Lock | None = None
    _lock_loop: asyncio.AbstractEventLoop | None = None

    # 🆕 v1.1.0: 插件配置引用（用于动态时间调整）
    _plugin_config: dict | None = None

    # ========== 🔧 配置参数集中提取（避免运行时多次读取） ==========
    # 动态时间段调整配置
    _enable_dynamic_reply_probability: bool = False
    _reply_time_periods: str = "[]"
    _reply_time_transition_minutes: int = 30
    _reply_time_min_factor: float = 0.1
    _reply_time_max_factor: float = 2.0
    _reply_time_use_smooth_curve: bool = True

    @staticmethod
    def _get_lock() -> asyncio.Lock:
        """Return the lock owned by the currently running event loop."""
        loop = asyncio.get_running_loop()
        if (
            ProbabilityManager._lock is None
            or ProbabilityManager._lock_loop is not loop
        ):
            ProbabilityManager._lock = asyncio.Lock()
            ProbabilityManager._lock_loop = loop
        return ProbabilityManager._lock

    @staticmethod
    def initialize(config: dict):
        """
        🆕 v1.1.0: 初始化概率管理器

        说明：配置由 main.py 统一提取后传入，此处直接使用传入的值，
        不再提供默认值（避免 AstrBot 平台多次读取配置的问题）

        Args:
            config: 插件配置字典（由 main.py 统一提取）
        """
        ProbabilityManager._plugin_config = config

        # ========== 🔧 直接使用传入的配置值 ==========
        # 动态时间段调整配置（重构后功能已移除，保留默认值避免 KeyError）
        ProbabilityManager._enable_dynamic_reply_probability = config.get(
            "enable_dynamic_reply_probability", False
        )
        ProbabilityManager._reply_time_periods = config.get("reply_time_periods", "")
        ProbabilityManager._reply_time_transition_minutes = config.get(
            "reply_time_transition_minutes", 0
        )
        ProbabilityManager._reply_time_min_factor = config.get(
            "reply_time_min_factor", 1.0
        )
        ProbabilityManager._reply_time_max_factor = config.get(
            "reply_time_max_factor", 1.0
        )
        ProbabilityManager._reply_time_use_smooth_curve = config.get(
            "reply_time_use_smooth_curve", False
        )

        if DEBUG_MODE:
            logger.debug("[概率管理器] 已初始化")

    @staticmethod
    async def reset() -> None:
        """Clear shared probability state during plugin shutdown."""
        async with ProbabilityManager._get_lock():
            ProbabilityManager._probability_status.clear()

    @staticmethod
    def get_chat_key(platform_name: str, is_private: bool, chat_id: str) -> str:
        """
        获取聊天的唯一标识

        Args:
            platform_name: 平台名称（如aiocqhttp, gewechat等）
            is_private: 是否私聊
            chat_id: 聊天ID（群号或用户ID）

        Returns:
            唯一标识键
        """
        chat_type = "private" if is_private else "group"
        return f"{platform_name}_{chat_type}_{chat_id}"

    @staticmethod
    def _clamp_probability(value: Any, fallback: float, label: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            logger.warning(
                f"[概率管理器] {label} 值 '{value}' 无法转换为浮点数，已回退为 {fallback:.2f}"
            )
            return fallback

        if parsed < 0.0 or parsed > 1.0:
            clamped = max(0.0, min(1.0, parsed))
            logger.warning(
                f"[概率管理器] {label} 值 {parsed} 超出范围[0,1]，已矫正为 {clamped:.2f}"
            )
            return clamped

        return parsed

    @staticmethod
    def _normalize_duration(duration: Any, fallback: int, label: str) -> int:
        try:
            parsed = int(duration)
        except (TypeError, ValueError):
            logger.warning(
                f"[概率管理器] {label} 值 '{duration}' 无法转换为整数，已回退为 {fallback} 秒"
            )
            return fallback

        if parsed <= 0:
            logger.warning(
                f"[概率管理器] {label} 值 {parsed} 小于等于 0，已回退为 {fallback} 秒"
            )
            return fallback

        return parsed

    @staticmethod
    def _migrate_legacy_status(status: dict[str, Any], chat_key: str) -> dict[str, Any]:
        migrated = copy.deepcopy(status)
        legacy_probability = migrated.get("probability")
        legacy_until = migrated.get("boosted_until")

        if legacy_probability is not None or legacy_until is not None:
            logger.warning(
                f"[概率管理器] 会话 {chat_key} 检测到旧版概率状态结构，已自动迁移为新结构"
            )
            if "base_probability" not in migrated and legacy_probability is not None:
                migrated["base_probability"] = legacy_probability
            if "base_until" not in migrated and legacy_until is not None:
                migrated["base_until"] = legacy_until
            migrated.setdefault("base_source", "legacy_probability_state")
            migrated.pop("probability", None)
            migrated.pop("boosted_until", None)

        return migrated

    @staticmethod
    def _compact_status(status: dict[str, Any]) -> dict[str, Any] | None:
        compacted = {
            key: value
            for key, value in status.items()
            if value is not None and value != ""
        }
        return compacted or None

    @staticmethod
    async def get_probability_status_snapshot(chat_key: str) -> dict[str, Any]:
        async with ProbabilityManager._get_lock():
            status = ProbabilityManager._probability_status.get(chat_key)
            if not status:
                return {}
            migrated = ProbabilityManager._migrate_legacy_status(status, chat_key)
            if migrated != status:
                ProbabilityManager._probability_status[chat_key] = migrated
            return copy.deepcopy(migrated)

    @staticmethod
    async def get_current_probability(
        platform_name: str, is_private: bool, chat_id: str, initial_probability: float
    ) -> float:
        """
        获取当前聊天的参与判断概率

        🆕 v1.1.0: 支持动态时间段概率调整
        🆕 v1.1.0: 支持临时概率提升（主动对话后的等待回应状态）
        🆕 v1.1.0: 支持概率硬性限制（一键简化功能）

        优先级顺序（从高到低）：
        1. 临时概率提升（主动对话后）- 叠加到基础概率上
        2. 常规概率提升（回复后）- 完全覆盖基础概率
        3. 动态时间段调整 - 作为系数应用到基础概率
        4. 基础概率（initial_probability）
        5. 概率硬性限制 - 强制限制最终概率范围（最末尾应用）

        Args:
            platform_name: 平台名称
            is_private: 是否私聊
            chat_id: 聊天ID
            initial_probability: 初始概率（配置值）

        Returns:
            当前概率值（已应用所有调整和限制）
        """
        chat_key = ProbabilityManager.get_chat_key(platform_name, is_private, chat_id)
        current_time = time.time()

        # ========== 第一步：获取基础概率（考虑基础覆盖 + 传统回复后提升） ==========
        base_probability = ProbabilityManager._clamp_probability(
            initial_probability, 0.0, f"会话 {chat_key} 的 initial_probability"
        )
        base_source = "initial_probability"

        async with ProbabilityManager._get_lock():
            status = ProbabilityManager._probability_status.get(chat_key)
            if status:
                migrated = ProbabilityManager._migrate_legacy_status(status, chat_key)
                if migrated != status:
                    ProbabilityManager._probability_status[chat_key] = migrated
                status = migrated

                base_until = status.get("base_until")
                if base_until is not None:
                    try:
                        base_until = float(base_until)
                    except (TypeError, ValueError):
                        logger.warning(
                            f"[概率管理器] 会话 {chat_key} 的基础概率到期时间无效，已清理该状态"
                        )
                        status.pop("base_probability", None)
                        status.pop("base_until", None)
                        status.pop("base_source", None)
                        base_until = None

                if base_until is not None:
                    if base_until < 0:
                        logger.warning(
                            f"[概率管理器] 会话 {chat_key} 的基础概率到期时间为负值，已清理该状态"
                        )
                        status.pop("base_probability", None)
                        status.pop("base_until", None)
                        status.pop("base_source", None)
                    elif current_time < base_until:
                        base_probability = ProbabilityManager._clamp_probability(
                            status.get("base_probability", base_probability),
                            base_probability,
                            f"会话 {chat_key} 的基础概率状态",
                        )
                        base_source = status.get("base_source", "base_probability")
                        if DEBUG_MODE:
                            logger.debug(
                                f"会话 {chat_key} 使用基础概率覆盖: {base_probability:.2f} (来源: {base_source})"
                            )
                    else:
                        status.pop("base_probability", None)
                        status.pop("base_until", None)
                        status.pop("base_source", None)
                        if DEBUG_MODE:
                            logger.debug(
                                f"会话 {chat_key} 的基础概率覆盖已超时，恢复为初始概率: {base_probability:.2f}"
                            )

                reply_boost_until = status.get("reply_boost_until")
                if reply_boost_until is not None:
                    try:
                        reply_boost_until = float(reply_boost_until)
                    except (TypeError, ValueError):
                        logger.warning(
                            f"[概率管理器] 会话 {chat_key} 的传统回复后提升到期时间无效，已清理该状态"
                        )
                        status.pop("reply_boost_probability", None)
                        status.pop("reply_boost_until", None)
                        status.pop("reply_boost_source", None)
                        reply_boost_until = None

                if reply_boost_until is not None:
                    if reply_boost_until < 0:
                        logger.warning(
                            f"[概率管理器] 会话 {chat_key} 的传统回复后提升到期时间为负值，已清理该状态"
                        )
                        status.pop("reply_boost_probability", None)
                        status.pop("reply_boost_until", None)
                        status.pop("reply_boost_source", None)
                    elif current_time < reply_boost_until:
                        boosted_probability = ProbabilityManager._clamp_probability(
                            status.get("reply_boost_probability", base_probability),
                            base_probability,
                            f"会话 {chat_key} 的传统回复后提升概率",
                        )
                        base_probability = boosted_probability
                        base_source = status.get(
                            "reply_boost_source", "after_reply_probability"
                        )
                        logger.debug(
                            f"[传统回复后提升] 会话 {chat_key} 当前使用临时提升概率: {base_probability:.2f}"
                        )
                    else:
                        status.pop("reply_boost_probability", None)
                        status.pop("reply_boost_until", None)
                        status.pop("reply_boost_source", None)
                        logger.debug(
                            f"[传统回复后提升] 会话 {chat_key} 的临时提升已过期，恢复到基础概率层"
                        )

                compacted = ProbabilityManager._compact_status(status)
                if compacted is None:
                    del ProbabilityManager._probability_status[chat_key]
                else:
                    ProbabilityManager._probability_status[chat_key] = compacted

        # ========== 最后一步：统一安全限制（确保所有路径都返回0-1范围内的值） ==========
        # 无论前面的计算如何，最终概率必须在0.0-1.0范围内
        base_probability = max(0.0, min(1.0, base_probability))

        # ========== 返回最终概率 ==========
        return base_probability

    @staticmethod
    async def boost_probability(
        platform_name: str,
        is_private: bool,
        chat_id: str,
        boosted_probability: float,
        duration: int,
    ) -> None:
        """
        临时提升参与判断概率

        AI回复后调用，提升概率促进连续对话

        Args:
            platform_name: 平台名称
            is_private: 是否私聊
            chat_id: 聊天ID
            boosted_probability: 提升后的概率
            duration: 持续时间（秒）
        """
        chat_key = ProbabilityManager.get_chat_key(platform_name, is_private, chat_id)
        current_time = time.time()
        safe_probability = ProbabilityManager._clamp_probability(
            boosted_probability,
            0.8,
            f"会话 {chat_key} 的传统回复后提升概率",
        )
        safe_duration = ProbabilityManager._normalize_duration(
            duration,
            120,
            f"会话 {chat_key} 的传统回复后提升持续时间",
        )
        boosted_until = current_time + safe_duration

        async with ProbabilityManager._get_lock():
            status = ProbabilityManager._probability_status.get(chat_key, {})
            status = ProbabilityManager._migrate_legacy_status(status, chat_key)
            status["reply_boost_probability"] = safe_probability
            status["reply_boost_until"] = boosted_until
            status["reply_boost_source"] = "after_reply_probability"
            ProbabilityManager._probability_status[chat_key] = status

        logger.debug(
            f"[传统回复后提升] 会话 {chat_key} 已启用临时提升: {safe_probability:.2f}, "
            f"持续 {safe_duration} 秒 (至 {time.strftime('%H:%M:%S', time.localtime(boosted_until))})"
        )

    @staticmethod
    async def reset_probability(
        platform_name: str, is_private: bool, chat_id: str
    ) -> None:
        """
        重置概率状态

        立即清除提升状态，恢复初始概率

        Args:
            platform_name: 平台名称
            is_private: 是否私聊
            chat_id: 聊天ID
        """
        chat_key = ProbabilityManager.get_chat_key(platform_name, is_private, chat_id)

        async with ProbabilityManager._get_lock():
            if chat_key in ProbabilityManager._probability_status:
                del ProbabilityManager._probability_status[chat_key]
                logger.debug(f"会话 {chat_key} 概率状态已重置")

    @staticmethod
    async def set_base_probability(
        platform_name: str,
        is_private: bool,
        chat_id: str,
        new_probability: float,
        duration: int = 600,
    ) -> None:
        """
        设置基础概率（用于频率动态调整）

        与 boost_probability 类似，但用于频率调整器修改基础概率
        这个概率会持续较长时间（默认10分钟），直到下次频率检查

        Args:
            platform_name: 平台名称
            is_private: 是否私聊
            chat_id: 聊天ID
            new_probability: 新的基础概率
            duration: 持续时间（秒），默认600秒（10分钟）
        """
        chat_key = ProbabilityManager.get_chat_key(platform_name, is_private, chat_id)
        current_time = time.time()
        safe_probability = ProbabilityManager._clamp_probability(
            new_probability,
            0.0,
            f"会话 {chat_key} 的基础概率覆盖值",
        )
        safe_duration = ProbabilityManager._normalize_duration(
            duration,
            600,
            f"会话 {chat_key} 的基础概率覆盖持续时间",
        )
        boosted_until = current_time + safe_duration

        async with ProbabilityManager._get_lock():
            status = ProbabilityManager._probability_status.get(chat_key, {})
            status = ProbabilityManager._migrate_legacy_status(status, chat_key)
            status["base_probability"] = safe_probability
            status["base_until"] = boosted_until
            status["base_source"] = "frequency_adjuster"
            ProbabilityManager._probability_status[chat_key] = status

        logger.debug(
            f"[频率调整] 会话 {chat_key} 基础概率已调整为 {safe_probability:.2f}, "
            f"持续 {safe_duration} 秒"
        )
