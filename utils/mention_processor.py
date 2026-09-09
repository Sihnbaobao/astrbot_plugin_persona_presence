"""
@/发送者/提及识别域（MentionMixin）。

从主插件类拆出的独立功能域：发送者显示、@全体/@他人判断、@解析与提及检查。
通过 self 访问 PersonaPresence 的状态与工具，保持行为不变。
"""

from astrbot.api.all import *  # noqa: F403  （与 main.py 保持一致）


class MentionMixin:
    @staticmethod
    def _safe_sender_display(event: AstrMessageEvent) -> str:
        """从事件获取安全的发送者显示名，解析失败时返回'未知用户'。"""
        try:
            name = str(event.get_sender_name() or "").strip()
            uid = str(event.get_sender_id() or "").strip()
            if name and name != uid:
                return name
            return "未知用户"
        except Exception:
            return "未知用户"

    # ============================================================
    # @提及检测
    # ============================================================

    def _is_at_all_message(self, event: AstrMessageEvent) -> bool:
        """检测消息是否包含@全体成员（仅识别，不决定是否忽略）。"""
        try:
            if not hasattr(event, "message_obj") or not hasattr(
                event.message_obj, "message"
            ):
                return False
            original_messages = event.message_obj.message
            if not original_messages:
                return False
            for component in original_messages:
                if isinstance(component, AtAll):
                    return True
                if isinstance(component, At):
                    qq_value = str(component.qq).lower()
                    if qq_value == "all":
                        return True
            return False
        except Exception as e:
            logger.warning(f"[@全体成员识别] 检测失败，按普通消息继续: {e}")
            return False

    def _should_ignore_at_all(self, event: AstrMessageEvent) -> bool:
        """检测是否应该忽略@全体成员的消息（插件内部额外过滤）。"""
        try:
            if not self.ignore_at_all_enabled:
                return False
            if not hasattr(event, "message_obj") or not hasattr(
                event.message_obj, "message"
            ):
                return False
            original_messages = event.message_obj.message
            if not original_messages:
                return False
            for component in original_messages:
                if isinstance(component, AtAll):
                    return True
                if isinstance(component, At):
                    qq_value = str(component.qq).lower()
                    if qq_value == "all":
                        return True
            return False
        except Exception as e:
            logger.error(f"[@全体成员检测] 发生错误: {e}", exc_info=True)
            return False

    def _should_ignore_at_others(self, event: AstrMessageEvent) -> bool:
        """检测是否应该忽略@他人的消息。"""
        try:
            if not self.enable_ignore_at_others:
                return False

            ignore_mode = self.ignore_at_others_mode
            bot_id = event.get_self_id()

            messages = (
                event.message_obj.message
                if hasattr(event, "message_obj")
                and hasattr(event.message_obj, "message")
                else []
            )
            if not messages:
                messages = []

            has_at_others = False
            has_at_bot = False

            for component in messages:
                if isinstance(component, At):
                    mentioned_id = str(component.qq)
                    if mentioned_id == bot_id:
                        has_at_bot = True
                    elif mentioned_id.lower() != "all":
                        has_at_others = True

            # 消息链中未检测到任何At组件时，尝试从原始消息数据读取（后备方案）
            if not has_at_others and not has_at_bot:
                raw_results = self._detect_at_from_raw_message(event, str(bot_id))
                has_at_bot = raw_results.get("has_at_bot", False)
                has_at_others = raw_results.get("has_at_others", False)

            # 若消息中包含对机器人的 @，无论模式如何都应该继续处理
            if has_at_bot:
                return False

            if ignore_mode == "strict":
                if has_at_others:
                    return True
            elif ignore_mode == "allow_with_bot":
                if has_at_others and not has_at_bot:
                    return True

            return False

        except Exception as e:
            logger.error(f"[@他人检测] 发生错误: {e}", exc_info=True)
            return False

    def _detect_at_from_raw_message(self, event: AstrMessageEvent, bot_id: str) -> dict:
        """从原始消息数据中检测 At 组件（后备方案）。"""
        result = {
            "has_at_bot": False,
            "has_at_others": False,
            "has_at_all": False,
            "mentions": [],
        }
        try:
            raw_event = getattr(event.message_obj, "raw_message", None)
            if not raw_event:
                return result

            raw_message = None
            if hasattr(raw_event, "message"):
                raw_message = raw_event.message
            elif isinstance(raw_event, dict):
                raw_message = raw_event.get("message", [])

            if not raw_message or not isinstance(raw_message, list):
                return result

            for segment in raw_message:
                seg_type = None
                seg_data = None
                if isinstance(segment, dict):
                    seg_type = segment.get("type")
                    seg_data = segment.get("data", {})
                elif hasattr(segment, "__getitem__"):
                    try:
                        seg_type = segment["type"]
                        seg_data = segment["data"]
                    except (KeyError, TypeError):
                        continue

                if seg_type != "at" or not seg_data:
                    continue

                qq_val = str(
                    seg_data.get("qq", "") if isinstance(seg_data, dict) else ""
                )
                if not qq_val:
                    continue

                if qq_val == bot_id:
                    result["has_at_bot"] = True
                    result["mentions"].append(
                        {
                            "user_id": qq_val,
                            "user_name": "",
                            "is_bot": True,
                            "is_all": False,
                            "resolved": True,
                        }
                    )
                elif qq_val.lower() == "all":
                    result["has_at_all"] = True
                    result["mentions"].append(
                        {
                            "user_id": "all",
                            "user_name": "全体成员",
                            "is_bot": False,
                            "is_all": True,
                            "resolved": True,
                        }
                    )
                else:
                    result["has_at_others"] = True
                    result["mentions"].append(
                        {
                            "user_id": qq_val,
                            "user_name": "",
                            "is_bot": False,
                            "is_all": False,
                            "resolved": False,
                        }
                    )

        except Exception as e:
            if self.debug_mode:
                logger.info(f"[@他人检测-原始消息后备] 读取失败: {e}")
        return result

    async def _check_mention_others(self, event: AstrMessageEvent) -> dict:
        """
        检测消息中的完整@信息，兼容多人/重复@场景。

        Returns:
            dict | None: 统一的@信息结构；无任何@时返回None
        """
        try:
            bot_id = str(event.get_self_id() or "")
            group_id = str(event.get_group_id() or "")
            messages = event.get_messages() or []
            mentions = []
            has_at_ai = False
            has_at_others = False
            has_at_all = False

            for component in messages:
                if isinstance(component, AtAll):
                    has_at_all = True
                    mentions.append(
                        {
                            "user_id": "all",
                            "user_name": "全体成员",
                            "is_bot": False,
                            "is_all": True,
                            "resolved": True,
                        }
                    )
                    continue

                if not isinstance(component, At):
                    continue

                mentioned_id = str(component.qq)
                if not mentioned_id:
                    continue

                if mentioned_id.lower() == "all":
                    has_at_all = True
                    mentions.append(
                        {
                            "user_id": "all",
                            "user_name": "全体成员",
                            "is_bot": False,
                            "is_all": True,
                            "resolved": True,
                        }
                    )
                    continue

                is_bot = mentioned_id == bot_id
                if is_bot:
                    has_at_ai = True
                    mentions.append(
                        {
                            "user_id": mentioned_id,
                            "user_name": "你",
                            "is_bot": True,
                            "is_all": False,
                            "resolved": True,
                        }
                    )
                    continue

                has_at_others = True
                fallback_name = (
                    component.name
                    if hasattr(component, "name") and component.name
                    else ""
                )
                resolved_name = await self._resolve_group_member_name(
                    event, mentioned_id, group_id, fallback_name=fallback_name
                )
                resolved_ok = bool(
                    resolved_name and resolved_name not in (mentioned_id, "未知用户")
                )
                mentions.append(
                    {
                        "user_id": mentioned_id,
                        "user_name": resolved_name or mentioned_id,
                        "is_bot": False,
                        "is_all": False,
                        "resolved": resolved_ok,
                    }
                )

            if not mentions:
                raw_results = self._detect_at_from_raw_message(event, bot_id)
                raw_mentions = raw_results.get("mentions", [])
                if raw_mentions:
                    has_at_all = bool(raw_results.get("has_at_all", False))
                    for raw_mention in raw_mentions:
                        if not isinstance(raw_mention, dict):
                            continue
                        raw_user_id = str(raw_mention.get("user_id", "") or "")
                        if not raw_user_id:
                            continue
                        if raw_user_id.lower() == "all":
                            has_at_all = True
                            mentions.append(
                                {
                                    "user_id": "all",
                                    "user_name": "全体成员",
                                    "is_bot": False,
                                    "is_all": True,
                                    "resolved": True,
                                }
                            )
                            continue
                        if raw_user_id == bot_id:
                            has_at_ai = True
                            mentions.append(
                                {
                                    "user_id": raw_user_id,
                                    "user_name": "你",
                                    "is_bot": True,
                                    "is_all": False,
                                    "resolved": True,
                                }
                            )
                            continue
                        has_at_others = True
                        resolved_name = await self._resolve_group_member_name(
                            event, raw_user_id, group_id, fallback_name=""
                        )
                        resolved_ok = bool(
                            resolved_name
                            and resolved_name not in (raw_user_id, "未知用户")
                        )
                        mentions.append(
                            {
                                "user_id": raw_user_id,
                                "user_name": resolved_name or raw_user_id,
                                "is_bot": False,
                                "is_all": False,
                                "resolved": resolved_ok,
                            }
                        )
                else:
                    if self.debug_mode:
                        logger.info("【@检测】未检测到任何@信息")
                    return None

            other_mentions = [
                m
                for m in mentions
                if isinstance(m, dict) and not m.get("is_bot") and not m.get("is_all")
            ]
            first_other = other_mentions[0] if other_mentions else {}

            mention_info = {
                "has_any_mention": bool(mentions),
                "has_at_ai": has_at_ai,
                "has_at_others": has_at_others,
                "has_at_all": has_at_all,
                "mentions": mentions,
                "other_mentions": other_mentions,
                "mention_count": len(mentions),
                "other_mention_count": len(other_mentions),
                "mentioned_user_id": str(first_other.get("user_id", "") or ""),
                "mentioned_user_name": str(first_other.get("user_name", "") or ""),
                "parse_failed": False,
            }

            if self.debug_mode and mentions:
                logger.info(
                    f"【@检测】检测完成: total={len(mentions)}, at_ai={has_at_ai}, at_others={has_at_others}, at_all={has_at_all}"
                )
            return mention_info

        except Exception as e:
            logger.error(f"检测@提及时发生错误: {e}", exc_info=True)
            return None
