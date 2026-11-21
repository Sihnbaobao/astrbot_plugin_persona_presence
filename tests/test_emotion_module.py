"""Regression tests for the emotion feature pack."""

import asyncio
import importlib.util
import sys
import types
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
UTILS_DIR = REPO_ROOT / "utils"
MODULES_DIR = UTILS_DIR / "modules"


class _Logger:
    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _Event:
    def __init__(self, text, uid="u1", private=True, at=True):
        self._text = text
        self._uid = uid
        self.unified_msg_origin = "test:private:u1" if private else "test:group:g1"
        self.is_at_or_wake_command = at

    def get_message_str(self):
        return self._text

    def get_sender_id(self):
        return self._uid

    def get_group_id(self):
        return "" if "private" in self.unified_msg_origin else "g1"


def _ctx(event, persona="你的名字是星野璃月"):
    base = _load().base
    return base.FeatureContext(
        event=event,
        umo=event.unified_msg_origin,
        group_id="" if "private" in event.unified_msg_origin else "g1",
        is_private="private" in event.unified_msg_origin,
        chat_key=event.unified_msg_origin,
        persona=persona,
        provider_id="test",
        now=datetime.now(),
    )


def _load():
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    sys.modules["astrbot.api"] = api
    package_name = "emotion_test_pkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(REPO_ROOT)]
    utils_package = types.ModuleType(package_name + ".utils")
    utils_package.__path__ = [str(UTILS_DIR)]
    modules_package = types.ModuleType(package_name + ".utils.modules")
    modules_package.__path__ = [str(MODULES_DIR)]
    sys.modules[package_name] = package
    sys.modules[package_name + ".utils"] = utils_package
    sys.modules[package_name + ".utils.modules"] = modules_package
    loaded = {}
    for name in ("base", "emotion"):
        full = f"{package_name}.utils.modules.{name}"
        spec = importlib.util.spec_from_file_location(full, MODULES_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full] = module
        setattr(modules_package, name, module)
        spec.loader.exec_module(module)
        loaded[name] = module
    return types.SimpleNamespace(**loaded)


def _mood(pack, event, persona="你的名字是星野璃月"):
    """Return the mood bucket for the persona the fixture uses."""
    key = pack._persona_key(persona)
    return pack.chats[event.unified_msg_origin][key][event.get_sender_id()]


def test_persona_switch_keeps_moods_separate(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    first = _Event("你真是傻，闭嘴")
    asyncio.run(pack.on_event(_ctx(first)))
    assert "[此刻]" in pack.block_for(first, True)

    second = _Event("", uid="u1")
    asyncio.run(pack.on_event(_ctx(second, persona="你的名字是另一个女孩，安静寡言。")))
    assert pack.block_for(second, True) == ""


def test_global_mood_survives_for_another_user(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    event = _Event("你真是傻，闭嘴", uid="a")
    asyncio.run(pack.on_event(_ctx(event)))
    other = _Event("", uid="b")
    block = pack.block_for(other, True)
    assert "[心情]" in block
    assert "恼" in block or "生气" in block


def test_persona_name_does_not_leak_across_personas(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    asyncio.run(pack.on_event(_ctx(_Event("你好"), persona="你的名字是星野璃月")))
    assert pack._name == "星野璃月"
    asyncio.run(
        pack.on_event(_ctx(_Event("你好"), persona="一个普通的助手，安静寡言。"))
    )
    assert pack._name == ""


def test_persona_name_has_no_hardcoded_default() -> None:
    loaded = _load()
    assert loaded.emotion.EmotionModule._persona_name("一个普通的助手") == ""
    assert (
        loaded.emotion.EmotionModule._persona_name("你的名字是星野璃月") == "星野璃月"
    )


def test_old_emotion_file_is_migrated(tmp_path):
    import json
    import time as _time

    legacy = {
        "version": 1,
        "chats": {
            "test:private:u1": {
                "u1": {
                    "base": {"anger": 0.4},
                    "cause": "旧记录",
                    "name": "旧用户",
                    "updated_at": _time.time(),
                }
            }
        },
    }
    (tmp_path / "emotion.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    block = pack.block_for(_Event(""), True)
    assert "恼" in block
    assert "旧记录" in block


def test_mood_file_is_published_for_the_body_plugin(tmp_path):
    import json

    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    event = _Event("你真是傻，闭嘴")
    asyncio.run(pack.on_event(_ctx(event)))
    payload = json.loads((tmp_path / "mood.json").read_text(encoding="utf-8"))
    assert payload["personas"]
    entry = next(iter(payload["personas"].values()))
    assert entry["name"] == "星野璃月"
    assert entry["base"]["anger"] > 0.1


def test_insult_creates_anger_state(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    asyncio.run(pack.on_event(_ctx(_Event("你真是傻，闭嘴"))))
    block = pack.block_for(_Event("你真是傻，闭嘴"), True)
    assert "[此刻]" in block
    assert "恼" in block
    assert "闭嘴" in block


def test_group_message_not_addressed_does_not_change(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    asyncio.run(pack.on_event(_ctx(_Event("今天天气不错", private=False, at=False))))
    assert pack.chats == {}


def test_state_is_separate_per_sender(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    asyncio.run(pack.on_event(_ctx(_Event("你真烦", uid="a", private=True))))
    block = pack.block_for(_Event("", uid="b"), True)
    # The personal mood stays with user a, but the persona-wide mood is shared.
    assert "[此刻]" not in block
    assert "[心情]" in block


def test_relationship_can_bottom_out_and_stay(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=2.0)
    event = _Event("你真是傻，闭嘴")
    for _ in range(3):
        asyncio.run(pack.on_event(_ctx(event)))
    relation = pack.ledger.relations[event.unified_msg_origin][event.get_sender_id()]
    assert relation.bottomed_out
    asyncio.run(pack.on_event(_ctx(_Event("对不起，我错了"))))
    assert relation.bottomed_out
    assert "不想再给这个人好脸色" in pack.block_for(event, True)


def test_group_can_become_uncomfortable(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=2.0)
    event = _Event("你真是傻，闭嘴", private=False, at=True)
    for _ in range(4):
        asyncio.run(pack.on_event(_ctx(event)))
    assert pack.should_withdraw(event.get_group_id())
    assert "不太想在这里开口" in pack.block_for(event, False)


def test_neutral_installation_is_not_anger(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    event = _Event("我先装个软件")
    asyncio.run(pack.on_event(_ctx(event)))
    assert pack.block_for(event, True) == ""
    assert pack.ledger.relations == {}


def test_negative_like_is_not_praise(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    event = _Event("我不喜欢你这样")
    asyncio.run(pack.on_event(_ctx(event)))
    mood = _mood(pack, event)
    assert mood.base["joy"] == 0.0


class _AppraisalResponse:
    completion_text = '{"intent":"挑衅","intensity":0.8,"cause":"对方觉得她在装"}'


class _AppraisalProvider:
    async def text_chat(self, **_kwargs):
        return _AppraisalResponse()


class _AppraisalContext:
    async def get_using_provider_async(self, *_args):
        return _AppraisalProvider()


def test_ambiguous_message_uses_semantic_appraisal(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(
        tmp_path, sensitivity=0.6, context=_AppraisalContext()
    )
    event = _Event("你是不是觉得我装")

    async def run():
        await pack.on_event(_ctx(event))
        pending = list(pack._appraisal_tasks)
        if pending:
            await asyncio.gather(*pending)

    asyncio.run(run())
    mood = _mood(pack, event)
    assert mood.base["anger"] > 0.1
    assert "对方觉得她在装" in mood.cause


def test_snapshot_is_json_serialisable(tmp_path):
    import json

    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    event = _Event("你真是傻，闭嘴")
    asyncio.run(pack.on_event(_ctx(event)))
    payload = pack.snapshot()
    assert payload["counts"]["moods"] == 1
    assert payload["counts"]["relations"] == 1
    assert isinstance(json.dumps(payload, ensure_ascii=False), str)


def _write_vitals(
    tmp_path,
    persona="璃月",
    tension=0.8,
    valence=-0.6,
    distress=0.7,
    dirt=0.2,
    line="饿着，耐心比平时薄",
):
    import json

    payload = {
        "version": 1,
        "personas": {
            persona: {
                "tension": tension,
                "valence": valence,
                "distress": distress,
                "dirt": dirt,
                "line": line,
            }
        },
    }
    (tmp_path / "vitals.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_body_state_adds_a_background_mood(tmp_path):
    loaded = _load()
    _write_vitals(tmp_path)
    pack = loaded.emotion.EmotionModule(
        tmp_path / "emotion", sensitivity=1.0, body_dir=tmp_path
    )
    event = _Event("今天天气不错")
    asyncio.run(pack.on_event(_ctx(event)))
    block = pack.block_for(event, True)
    assert "[身体]" in block
    assert "耐心" in block
    assert "[此刻]" in block
    assert "生气" in block or "恼" in block


def test_body_link_can_be_switched_off(tmp_path):
    loaded = _load()
    _write_vitals(tmp_path)
    pack = loaded.emotion.EmotionModule(
        tmp_path / "emotion", sensitivity=1.0, body_dir=tmp_path, body_link=False
    )
    event = _Event("今天天气不错")
    asyncio.run(pack.on_event(_ctx(event)))
    assert pack.block_for(event, True) == ""


def test_comfortable_body_leaves_the_block_empty(tmp_path):
    loaded = _load()
    _write_vitals(tmp_path, tension=0.05, valence=0.3, distress=0.1, dirt=0.0, line="")
    pack = loaded.emotion.EmotionModule(
        tmp_path / "emotion", sensitivity=1.0, body_dir=tmp_path
    )
    event = _Event("今天天气不错")
    asyncio.run(pack.on_event(_ctx(event)))
    assert pack.block_for(event, True) == ""


def test_media_comment_is_not_praise(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    event = _Event("给我找几个好看的百合")
    asyncio.run(pack.on_event(_ctx(event)))
    mood = _mood(pack, event)
    assert mood.base["joy"] == 0.0


def test_harassment_comment_is_not_praise(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    event = _Event("没意思，现在这璃月喜欢被骚扰")
    asyncio.run(pack.on_event(_ctx(event)))
    mood = _mood(pack, event)
    assert mood.base["joy"] == 0.0


class _TeaseResponse:
    completion_text = '{"intent":"调侃","intensity":0.8,"cause":"在逗AI玩"}'


class _TeaseProvider:
    async def text_chat(self, **_kwargs):
        return _TeaseResponse()


class _TeaseContext:
    async def get_using_provider_async(self, *_args):
        return _TeaseProvider()


def test_group_praise_can_be_reappraised_as_teasing(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(
        tmp_path, sensitivity=1.0, context=_TeaseContext()
    )
    event = _Event("你好可爱", private=False, at=True)

    async def run():
        await pack.on_event(_ctx(event))
        pending = list(pack._appraisal_tasks)
        if pending:
            await asyncio.gather(*pending)

    asyncio.run(run())
    mood = _mood(pack, event)
    assert mood.base["joy"] < 0.1
    assert "逗AI" in mood.cause


def test_praise_from_disliked_person_does_not_raise_joy(tmp_path):
    import types

    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    event = _Event("你好可爱")
    pack.ledger.relations[event.unified_msg_origin] = {
        event.get_sender_id(): types.SimpleNamespace(
            warmth=0.0,
            trust=0.2,
            resentment=0.8,
            bottomed_out=False,
            name="",
            negative_events=5,
            positive_events=0,
            cause="",
            updated_at=0.0,
        )
    }
    asyncio.run(pack.on_event(_ctx(event)))
    mood = _mood(pack, event)
    assert mood.base["joy"] < 0.1
    assert mood.base["disgust"] > 0

def test_music_activity_only_reads_new_entries(tmp_path):
    import json

    plugin_data = tmp_path / "plugin_data"
    music_dir = plugin_data / "astrbot_plugin_music_card"
    music_dir.mkdir(parents=True)
    listened = music_dir / "listened.jsonl"
    listened.write_text(
        json.dumps({"at": "old", "name": "旧歌", "score": 9, "like": True})
        + "\n",
        encoding="utf-8",
    )
    loaded = _load()
    pack = loaded.emotion.EmotionModule(
        tmp_path, sensitivity=1.0, activity_link=True, activity_dir=plugin_data
    )
    assert pack.activity is not None
    assert pack.activity.poll() == []

    with listened.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "at": "new",
                    "name": "新歌",
                    "artist": "花譜",
                    "score": 9,
                    "like": True,
                }
            )
            + "\n"
        )
    events = pack.activity.poll()
    assert events
    assert events[-1]["source"] == "music"
    assert "新歌" in events[-1]["cause"]
    assert events[-1]["deltas"]["joy"] > 0

def test_body_cycle_state_colors_the_emotion_bias(tmp_path):
    import json

    pack = _load()

    module = pack.emotion.EmotionModule(
        data_dir=tmp_path / "emotion",
        body_link=True,
        body_dir=tmp_path,
        sensitivity=1.0,
    )
    (tmp_path / "vitals.json").write_text(
        json.dumps(
            {
                "personas": {
                    "璃月": {
                        "tension": 0.5,
                        "distress": 0.4,
                        "valence": -0.4,
                        "dirt": 0.2,
                        "pms": 0.8,
                        "irritability": 0.8,
                        "sensitivity": 0.7,
                        "tearfulness": 0.7,
                        "cycle_phase": "luteal",
                        "line": "经前，这几天容易烦、也容易委屈",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    bias, line = module._body_bias("璃月是一个女孩子")
    assert bias["anger"] > 0.3
    assert bias["sadness"] > 0.2
    assert bias["fear"] > 0.1
    assert "经前" in line

def test_temperament_comes_from_the_persona_text(tmp_path):
    pack = _load()
    module = pack.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    lively = module._keyword_temperament("她活泼、开朗、爱笑，喜欢黏人")
    cool = module._keyword_temperament("她冷淡、毒舌，脾气不太好")
    assert lively["joy"] > cool["joy"]
    assert cool["anger"] > lively["anger"]

def test_social_emotions_are_recorded(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    event = _Event("你好可爱，笑死我了")
    asyncio.run(pack.on_event(_ctx(event)))
    mood = _mood(pack, event)
    assert mood.social["embarrassment"] > 0.0
    assert mood.social["pride"] > 0.0


def test_repeated_anger_is_held_back(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=1.0)
    mood = loaded.emotion._Mood()
    pack._add(mood, anger=0.2)
    pack._add(mood, anger=0.2)
    assert mood.held["anger"] > 0.05
    assert mood.rumination > 0.0

def test_semantic_appraisal_parses_social_and_sarcasm(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path)
    payload = pack._parse_appraisal(
        '{"thought":"真会夸人","intent":"反讽","intensity":0.7,'
        '"social":{"embarrassment":0.2,"guilt":0}}'
    )
    assert payload["intent"] == "反讽"
    assert payload["intensity"] == 0.7
    assert payload["social"]["embarrassment"] == 0.2
    assert "guilt" not in payload["social"]


def test_person_attitude_is_shared_between_group_and_private(tmp_path):
    loaded = _load()
    pack = loaded.emotion.EmotionModule(tmp_path, sensitivity=2.0)
    group = _Event("你好可爱", uid="same-person", private=False, at=True)
    private = _Event("谢谢你", uid="same-person", private=True, at=True)
    asyncio.run(pack.on_event(_ctx(group)))
    asyncio.run(pack.on_event(_ctx(private)))
    group_rel = pack.ledger.relations[group.unified_msg_origin]["same-person"]
    private_rel = pack.ledger.relations[private.unified_msg_origin]["same-person"]
    assert group_rel is private_rel
    assert group_rel.positive_events >= 2
    snapshot = pack.snapshot()
    assert snapshot["counts"]["relations"] == 1
    assert len(snapshot["relations"][0]["contexts"]) == 2
