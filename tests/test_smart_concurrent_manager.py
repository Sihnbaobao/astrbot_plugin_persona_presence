"""Regression tests for SmartConcurrentManager batch limits."""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path


class _Logger:
    """Minimal logger double for the manager module."""

    def warning(self, *_args, **_kwargs):
        pass


def _load_manager(monkeypatch):
    """Load SmartConcurrentManager with a minimal AstrBot logger stub."""
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    astrbot = types.ModuleType("astrbot")
    monkeypatch.setitem(sys.modules, "astrbot", astrbot)
    monkeypatch.setitem(sys.modules, "astrbot.api", api)

    path = Path(__file__).parents[1] / "utils" / "smart_concurrent_manager.py"
    spec = importlib.util.spec_from_file_location("smart_concurrent_manager_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module.SmartConcurrentManager


def test_claim_batch_honors_private_batch_limit(monkeypatch):
    """A private batch limit leaves later followers for a later claim."""

    async def scenario():
        manager = _load_manager(monkeypatch)
        manager._pending = {}
        manager._consumed = {}
        manager._lock = None

        for index in range(4):
            processing_id = f"message-{index}"
            await manager.register_arrival(
                "private-user",
                processing_id,
                arrival_seq=index + 1,
                arrival_monotonic=float(index + 1),
            )
            await manager.attach_payload(
                "private-user",
                processing_id,
                content=f"message {index}",
                sender_name="User",
                sender_id="42",
                cached_data={"content": f"message {index}"},
            )

        result = await manager.claim_batch(
            "private-user", "message-0", max_batch_size=2
        )

        assert result["is_anchor"] is True
        assert [entry["processing_id"] for entry in result["merged_entries"]] == [
            "message-1",
            "message-2",
        ]
        assert "message-3" in manager._pending["private-user"]

    asyncio.run(scenario())


def test_private_forced_followups_merge_but_group_forced_messages_split(monkeypatch):
    """Private Smart merges forced follow-ups while group Smart preserves boundaries."""

    async def scenario():
        manager = _load_manager(monkeypatch)
        manager._pending = {}
        manager._consumed = {}
        manager._lock = None

        for chat_id in ("private-user", "group-1"):
            await manager.register_arrival(
                chat_id,
                "first",
                arrival_seq=1,
                arrival_monotonic=1.0,
            )
            await manager.register_arrival(
                chat_id,
                "second",
                arrival_seq=2,
                arrival_monotonic=2.0,
            )
            for processing_id, content in (
                ("first", "璃月喜欢什么歌"),
                ("second", "璃月再说一句"),
            ):
                await manager.attach_payload(
                    chat_id,
                    processing_id,
                    content=content,
                    sender_name="User",
                    sender_id="42",
                    cached_data={"content": content},
                    is_forced=True,
                )

        private_result = await manager.claim_batch(
            "private-user", "first", max_batch_size=10
        )
        assert [
            entry["processing_id"] for entry in private_result["merged_entries"]
        ] == ["second"]

        group_result = await manager.claim_batch("group-1", "first")
        assert group_result["merged_entries"] == []
        assert "second" in manager._pending["group-1"]

    asyncio.run(scenario())


def test_private_media_modes_are_configured_separately():
    """The schema and local config expose image and sticker policies separately."""
    root = Path(__file__).parents[1]
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    runtime = json.loads(
        (
            root.parent.parent
            / "config"
            / "astrbot_plugin_persona_presence_config.json"
        ).read_text(encoding="utf-8-sig")
    )

    enhance_items = schema["gcp_enhance"]["items"]
    assert set(enhance_items["private_image_mode"]["options"]) == {
        "ignore",
        "decide",
        "always",
    }
    assert set(enhance_items["private_emoji_mode"]["options"]) == {
        "ignore",
        "decide",
        "always",
    }
    basic_items = schema["gcp_basic"]["items"]
    assert basic_items["private_reply_mode"]["options"] == ["direct", "decide"]
    assert runtime["gcp_basic"]["private_reply_mode"] in set(
        basic_items["private_reply_mode"]["options"]
    )
    assert runtime["gcp_reply"]["collapse_reply_newlines"] is True
    assert enhance_items["private_image_mode"]["default"] == "decide"
    assert enhance_items["private_emoji_mode"]["default"] == "ignore"
    assert runtime["gcp_enhance"]["private_image_mode"] in set(
        enhance_items["private_image_mode"]["options"]
    )
    assert runtime["gcp_enhance"]["private_emoji_mode"] in set(
        enhance_items["private_emoji_mode"]["options"]
    )
    assert runtime["gcp_concurrent"]["private_batch_wait_ms"] == 4500


def test_arrival_order_survives_payload_processing_race(monkeypatch):
    """The first arrival remains the anchor even when its payload is later."""

    async def scenario():
        manager = _load_manager(monkeypatch)
        manager._pending = {}
        manager._consumed = {}
        manager._lock = None

        await manager.register_arrival(
            "private-user",
            "first",
            arrival_seq=1,
            arrival_monotonic=1.0,
        )
        await manager.register_arrival(
            "private-user",
            "second",
            arrival_seq=2,
            arrival_monotonic=2.0,
        )
        await manager.attach_payload(
            "private-user",
            "second",
            content="second message",
            sender_name="User",
            sender_id="42",
            cached_data={"content": "second message"},
        )

        blocked = await manager.claim_batch("private-user", "second")
        assert blocked["is_anchor"] is False
        assert blocked["blocked_by"] == "first"

        await manager.attach_payload(
            "private-user",
            "first",
            content="first message",
            sender_name="User",
            sender_id="42",
            cached_data={"content": "first message"},
        )
        claimed = await manager.claim_batch("private-user", "first")
        assert claimed["is_anchor"] is True
        assert [entry["processing_id"] for entry in claimed["merged_entries"]] == [
            "second"
        ]

    asyncio.run(scenario())


def test_late_payload_attach_is_rejected(monkeypatch):
    """An expired registration cannot be recreated by a slow payload task."""

    async def scenario():
        manager = _load_manager(monkeypatch)
        manager._pending = {}
        manager._consumed = {}
        manager._lock = None
        manager._EXPIRE_SECONDS = 0

        await manager.register_arrival("scoped-chat", "late", arrival_seq=1)
        attached = await manager.attach_payload(
            "scoped-chat",
            "late",
            content="late message",
            sender_name="User",
            sender_id="42",
            cached_data={"content": "late message"},
        )
        assert attached is False
        assert "scoped-chat" not in manager._pending

    asyncio.run(scenario())
