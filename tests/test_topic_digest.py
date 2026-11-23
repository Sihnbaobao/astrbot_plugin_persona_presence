"""Tests for the persistent rolling topic digest."""

import importlib.util
import sys
from pathlib import Path

MODULE = Path(__file__).parents[1] / "utils" / "modules" / "topic_digest.py"


def _load():
    spec = importlib.util.spec_from_file_location("topic_digest_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_digest_survives_reload(tmp_path):
    module = _load()
    store = module.TopicDigestStore(tmp_path)
    store.update("g", "在聊蘑菇怪不找女朋友的事", participants=["蘑菇怪"])
    again = module.TopicDigestStore(tmp_path)
    assert again.summary_for("g") == "在聊蘑菇怪不找女朋友的事"


def test_missing_summary_is_empty(tmp_path):
    module = _load()
    store = module.TopicDigestStore(tmp_path)
    assert store.summary_for("unknown") == ""
