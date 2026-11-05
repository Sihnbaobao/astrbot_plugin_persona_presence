"""Regression tests for the proactive feature pack."""

import asyncio
import importlib.util
import sys
import time
import types
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
UTILS_DIR = REPO_ROOT / "utils"


class _Logger:
    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _Response:
    def __init__(self, text):
        self.completion_text = text


class _Context:
    def __init__(self, text=""):
        self.text = text
        self.sent = []
        self.calls = 0

    async def llm_generate(self, **_kwargs):
        self.calls += 1
        return _Response(self.text)

    async def get_using_provider_async(self, *_args, **_kwargs):
        return types.SimpleNamespace(
            meta=lambda: types.SimpleNamespace(id="test-provider")
        )

    async def send_message(self, session, chain):
        self.sent.append((session, chain))
        return True


class _Event:
    def __init__(self, group="100", text="聊点别的", sender="某人"):
        self._group = group
        self._text = text
        self._sender = sender
        self.unified_msg_origin = f"plat:GroupMessage:{group}"

    def get_group_id(self):
        return self._group

    def get_message_str(self):
        return self._text

    def get_sender_name(self):
        return self._sender

    def is_private_chat(self):
        return False


def _load_module(monkeypatch):
    api_all = types.ModuleType("astrbot.api.all")
    api_all.logger = _Logger()
    api_all.AstrMessageEvent = object
    api_all.Plain = lambda text: text
    api_all.MessageChain = lambda items: items
    api_mod = types.ModuleType("astrbot.api")
    api_mod.logger = api_all.logger
    monkeypatch.setitem(sys.modules, "astrbot", types.ModuleType("astrbot"))
    monkeypatch.setitem(sys.modules, "astrbot.api", api_mod)
    monkeypatch.setitem(sys.modules, "astrbot.api.all", api_all)

    package_name = "proactive_test_pkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(REPO_ROOT)]
    modules_package = types.ModuleType(package_name + ".utils.modules")
    modules_package.__path__ = [str(UTILS_DIR / "modules")]
    utils_package = types.ModuleType(package_name + ".utils")
    utils_package.__path__ = [str(UTILS_DIR)]
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, package_name + ".utils", utils_package)
    monkeypatch.setitem(sys.modules, package_name + ".utils.modules", modules_package)

    for name in ("base", "proactive"):
        full = f"{package_name}.utils.modules.{name}"
        spec = importlib.util.spec_from_file_location(
            full, UTILS_DIR / "modules" / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, full, module)
        setattr(modules_package, name, module)
        spec.loader.exec_module(module)
    return sys.modules[package_name + ".utils.modules.proactive"]


def _settings(module, **overrides):
    values = dict(
        groups={"100"},
        cooldown_minutes=10,
        min_gap_seconds=0,
        daily_cap=20,
        global_cooldown_minutes=0,
        min_lull_seconds=0,
        max_lull_seconds=0,
        quiet_hours="",
        min_messages=1,
        base_probability=1.0,
        max_chars=30,
        provider_id="test",
    )
    values.update(overrides)
    return module.ProactiveSettings(**values)


def _ctx(module, event, *, is_private=False, now=None, persona="测试人格"):
    return module.FeatureContext(
        event=event,
        umo=event.unified_msg_origin,
        group_id="" if is_private else str(event.get_group_id() or ""),
        is_private=is_private,
        chat_key="test:" + str(event.get_group_id() or "private"),
        persona=persona,
        provider_id="test",
        now=now or datetime.now(),
    )


def test_message_that_wants_to_speak_is_proposed(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("接一句…")
    pack = module.ProactiveModule(context, _settings(module), data_dir=tmp_path)
    ctx = _ctx(module, _Event())

    asyncio.run(pack.on_event(ctx))
    proposal = asyncio.run(pack.propose(ctx))

    assert proposal is not None
    assert proposal.text == "接一句…"
    assert proposal.source == "proactive"
    assert context.sent == [], "the pack must never send directly"


def test_silence_is_respected(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("沉默")
    pack = module.ProactiveModule(context, _settings(module), data_dir=tmp_path)
    ctx = _ctx(module, _Event())

    asyncio.run(pack.on_event(ctx))
    assert asyncio.run(pack.propose(ctx)) is None


def test_private_chat_is_ignored(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("接一句")
    pack = module.ProactiveModule(context, _settings(module), data_dir=tmp_path)
    ctx = _ctx(module, _Event(), is_private=True)

    asyncio.run(pack.on_event(ctx))
    assert asyncio.run(pack.propose(ctx)) is None
    assert context.calls == 0


def test_unlisted_group_is_ignored(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("接一句")
    pack = module.ProactiveModule(context, _settings(module), data_dir=tmp_path)
    ctx = _ctx(module, _Event(group="999"))

    asyncio.run(pack.on_event(ctx))
    assert asyncio.run(pack.propose(ctx)) is None


def test_all_groups_scope_allows_an_unlisted_group(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("接一句")
    pack = module.ProactiveModule(
        context,
        _settings(module, groups=set(), all_groups=True),
        data_dir=tmp_path,
    )
    ctx = _ctx(module, _Event(group="999"))

    asyncio.run(pack.on_event(ctx))
    assert asyncio.run(pack.propose(ctx)) is not None


def test_cooldown_blocks_second_line(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("接一句")
    pack = module.ProactiveModule(context, _settings(module), data_dir=tmp_path)
    ctx = _ctx(module, _Event(text="第一句"))

    asyncio.run(pack.on_event(ctx))
    first = asyncio.run(pack.propose(ctx))
    assert first is not None
    asyncio.run(pack.on_proposal_sent(ctx, first))

    asyncio.run(pack.on_event(_ctx(module, _Event(text="第二句"))))
    assert asyncio.run(pack.propose(ctx)) is None


def test_state_survives_restart(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("接一句")
    pack = module.ProactiveModule(context, _settings(module), data_dir=tmp_path)
    ctx = _ctx(module, _Event())
    asyncio.run(pack.on_event(ctx))
    proposal = asyncio.run(pack.propose(ctx))
    assert proposal is not None
    asyncio.run(pack.on_proposal_sent(ctx, proposal))
    asyncio.run(pack.close())

    context2 = _Context("接一句")
    pack2 = module.ProactiveModule(context2, _settings(module), data_dir=tmp_path)
    ctx2 = _ctx(module, _Event(text="重启后"))
    asyncio.run(pack2.on_event(ctx2))

    assert asyncio.run(pack2.propose(ctx2)) is None


def test_quiet_hours_detection(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    pack = module.ProactiveModule(
        _Context(), _settings(module, quiet_hours="00:00-23:59"), data_dir=tmp_path
    )

    assert pack._in_quiet_hours(datetime(2026, 1, 1, 12, 0)) is True


def test_quiet_hours_blocks_proposal(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("接一句")
    pack = module.ProactiveModule(
        context, _settings(module, quiet_hours="01:00-07:00"), data_dir=tmp_path
    )
    ctx = _ctx(module, _Event(), now=datetime(2026, 1, 1, 2, 0))

    asyncio.run(pack.on_event(ctx))
    assert asyncio.run(pack.propose(ctx)) is None


def test_daily_cap_blocks_proposal(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("接一句")
    pack = module.ProactiveModule(
        context,
        _settings(module, groups={"100", "200"}, daily_cap=1),
        data_dir=tmp_path,
    )
    first_ctx = _ctx(module, _Event(group="100"))
    asyncio.run(pack.on_event(first_ctx))
    first = asyncio.run(pack.propose(first_ctx))
    assert first is not None
    asyncio.run(pack.on_proposal_sent(first_ctx, first))

    second_ctx = _ctx(module, _Event(group="200"))
    asyncio.run(pack.on_event(second_ctx))
    assert asyncio.run(pack.propose(second_ctx)) is None


def test_zero_daily_cap_means_no_hard_limit(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("接一句")
    pack = module.ProactiveModule(
        context, _settings(module, daily_cap=0), data_dir=tmp_path
    )
    ctx = _ctx(module, _Event())
    asyncio.run(pack.on_event(ctx))

    assert pack.can_propose(ctx) is True
    assert asyncio.run(pack.propose(ctx)) is not None


def test_interests_and_experience_reach_the_prompt(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    captured = {}

    class _Recorder(_Context):
        async def llm_generate(self, **kwargs):
            captured["prompt"] = kwargs.get("prompt", "")
            return await super().llm_generate(**kwargs)

    pack = module.ProactiveModule(
        _Recorder("沉默"),
        _settings(module),
        interest_provider=lambda: ["在意：妹妹泠", "喜欢：推理小说"],
        state_provider=lambda: ["最近：5 小时前看了《奶龙》"],
        data_dir=tmp_path,
    )
    ctx = _ctx(module, _Event(), persona="一个安静的人格")

    asyncio.run(pack.on_event(ctx))
    asyncio.run(pack.propose(ctx))

    prompt = captured.get("prompt", "")
    assert "妹妹泠" in prompt
    assert "推理小说" in prompt
    assert "奶龙" in prompt
    assert "一个安静的人格" in prompt


def test_recent_knowledge_reaches_the_prompt(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    captured = {}

    class _Recorder(_Context):
        async def llm_generate(self, **kwargs):
            captured["prompt"] = kwargs.get("prompt", "")
            return await super().llm_generate(**kwargs)

    pack = module.ProactiveModule(
        _Recorder("沉默"),
        _settings(module),
        knowledge_provider=lambda: ["提瓦特：游戏中的世界"],
        data_dir=tmp_path,
    )
    ctx = _ctx(module, _Event())

    asyncio.run(pack.on_event(ctx))
    asyncio.run(pack.propose(ctx))

    prompt = captured.get("prompt", "")
    assert "你最近刚知道的" in prompt
    assert "提瓦特" in prompt


def test_group_transcript_is_not_fed_to_the_model(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    captured = {}

    class _Recorder(_Context):
        async def llm_generate(self, **kwargs):
            captured["prompt"] = kwargs.get("prompt", "")
            return await super().llm_generate(**kwargs)

    pack = module.ProactiveModule(
        _Recorder("沉默"), _settings(module), data_dir=tmp_path
    )
    ctx = _ctx(module, _Event(text="群友正在聊原神新版本"))

    asyncio.run(pack.on_event(ctx))
    asyncio.run(pack.propose(ctx))

    prompt = captured.get("prompt", "")
    assert "原神新版本" not in prompt
    assert "你此刻有没有什么想主动跟群里说的" in prompt


def test_snapshot_explains_the_blocking_gate(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    pack = module.ProactiveModule(
        _Context("接一句"), _settings(module, min_messages=3), data_dir=tmp_path
    )
    ctx = _ctx(module, _Event())
    asyncio.run(pack.on_event(ctx))

    snapshot = pack.snapshot(ctx.now)

    group = snapshot["groups"][0]
    assert group["buffered"] == 1
    assert group["eligible"] is False
    assert "消息不足" in group["reason"]


def test_snapshot_lists_configured_groups_without_messages(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    pack = module.ProactiveModule(
        _Context(), _settings(module, groups={"100", "200"}), data_dir=tmp_path
    )
    ctx = _ctx(module, _Event(group="100"))
    asyncio.run(pack.on_event(ctx))

    snapshot = pack.snapshot(ctx.now)
    groups = {item["group"]: item for item in snapshot["groups"]}

    assert "200" in groups
    assert groups["200"]["buffered"] == 0
    assert "消息不足" in groups["200"]["reason"]


def test_too_long_line_is_recorded(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    pack = module.ProactiveModule(
        _Context("这一句话明显超过三十个字符，所以它只会被记下来，不会发出去。"),
        _settings(module, max_chars=10),
        data_dir=tmp_path,
    )
    ctx = _ctx(module, _Event())
    asyncio.run(pack.on_event(ctx))

    assert asyncio.run(pack.propose(ctx)) is None
    snapshot = pack.snapshot(ctx.now)
    assert any(
        item["kind"] == "skip" and item["reason"] == "too_long"
        for item in snapshot["history"]
    )


def test_global_cooldown_blocks_another_group(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("接一句")
    pack = module.ProactiveModule(
        context,
        _settings(module, groups={"100", "200"}, global_cooldown_minutes=30),
        data_dir=tmp_path,
    )
    first = _ctx(module, _Event(group="100"))
    asyncio.run(pack.on_event(first))
    proposal = asyncio.run(pack.propose(first))
    assert proposal is not None
    asyncio.run(pack.on_proposal_sent(first, proposal))

    second = _ctx(module, _Event(group="200"))
    asyncio.run(pack.on_event(second))
    assert asyncio.run(pack.propose(second)) is None
    reason = pack.snapshot(second.now)["groups"]
    assert any("全局冷却" in item["reason"] for item in reason)


def test_group_needs_a_lull_before_speaking(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    pack = module.ProactiveModule(
        _Context("接一句"),
        _settings(module, min_lull_seconds=60),
        data_dir=tmp_path,
    )
    ctx = _ctx(module, _Event())
    asyncio.run(pack.on_event(ctx))

    assert asyncio.run(pack.propose(ctx)) is None
    reason = pack.snapshot(ctx.now)["groups"][0]["reason"]
    assert "还在聊" in reason

    pack.groups["100"].last_seen = time.time() - 120
    assert asyncio.run(pack.propose(ctx)) is not None


def test_repeated_topic_is_not_proposed(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    context = _Context("蓝色星原怎么还没出呢...")
    pack = module.ProactiveModule(
        context, _settings(module, max_chars=60), data_dir=tmp_path
    )
    ctx = _ctx(module, _Event())
    asyncio.run(pack.on_event(ctx))
    first = asyncio.run(pack.propose(ctx))
    assert first is not None
    asyncio.run(pack.on_proposal_sent(ctx, first))

    state = pack.groups["100"]
    state.last_spoke = 0.0
    state.last_try = 0.0
    pack._last_spoke_any = 0.0
    context.text = "蓝色星原的新pv...还行。"
    asyncio.run(pack.on_event(_ctx(module, _Event(text="再聊一次"))))

    assert asyncio.run(pack.propose(ctx)) is None
    assert any(item["reason"] == "repeat" for item in pack.snapshot(ctx.now)["history"])


def test_old_history_is_pruned(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    pack = module.ProactiveModule(
        _Context(), _settings(module, history_ttl_days=3), data_dir=tmp_path
    )
    pack._history.append(
        {
            "kind": "sent",
            "group": "100",
            "text": "旧动作",
            "reason": "",
            "at": time.time() - 4 * 86400,
        }
    )

    snapshot = pack.snapshot()

    assert snapshot["history"] == []


def test_recent_spoke_timestamp_blocks_restart(monkeypatch, tmp_path):
    module = _load_module(monkeypatch)
    pack = module.ProactiveModule(
        _Context("接一句"), _settings(module), data_dir=tmp_path
    )
    ctx = _ctx(module, _Event())
    asyncio.run(pack.on_event(ctx))
    proposal = asyncio.run(pack.propose(ctx))
    assert proposal is not None
    asyncio.run(pack.on_proposal_sent(ctx, proposal))
    asyncio.run(pack.close())

    reloaded = module.ProactiveModule(
        _Context("接一句"), _settings(module), data_dir=tmp_path
    )
    assert reloaded._in_quiet_hours(datetime(2026, 1, 1, 12, 0)) is False
    state = reloaded.groups.get("100")
    assert state is not None and state.last_spoke > 0
