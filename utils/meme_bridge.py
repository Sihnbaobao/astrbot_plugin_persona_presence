"""Optional bridge to meme_manager for queued, same-message memes.

The bridge only selects one existing image. It never imports or modifies the
third-party plugin at module load time; the plugin is resolved lazily so a
missing or updated meme_manager degrades to "no meme" instead of breaking the
base reply flow.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from astrbot.api import logger


async def select_meme_image(
    context: Any,
    event: Any,
    query: str,
    min_score: float = 0.25,
    avoid_paths: set[str] | None = None,
) -> str | None:
    """Select one specific meme image with meme_manager's semantic index.

    Args:
        context: AstrBot context used to locate the loaded plugin.
        event: Current message event, used for pack/persona resolution.
        query: Natural-language description of the intended expression.
        min_score: Minimum semantic similarity accepted by meme_manager.
        avoid_paths: Recently used image basenames to skip when possible.

    Returns:
        An existing image path, or None when the bridge is unavailable.
    """
    query = str(query or "").strip()
    if not query:
        return None

    try:
        stars = getattr(context, "get_all_stars", lambda: [])()
        star = next(
            (item for item in stars if getattr(item, "name", "") == "meme_manager"),
            None,
        )
        plugin = getattr(star, "star_cls", None) if star is not None else None
        if plugin is None or not getattr(star, "activated", False):
            return None

        package = plugin.__class__.__module__.rsplit(".", 1)[0]
        query_module = importlib.import_module(f"{package}.backend.semantic.query")
        config_module = importlib.import_module(f"{package}.config")
        plugin_context = plugin._resolve_runtime_pack_context(event=event)
        pack_ids = list(plugin_context.get("pack_ids") or [])
        pack_dirs = dict(plugin_context.get("pack_dirs") or {})
        if not pack_ids:
            return None
        pack_contexts = [
            {"pack_id": pack_id, "pack_dir": pack_dirs[pack_id]}
            for pack_id in pack_ids
            if pack_id in pack_dirs
        ]
        if not pack_contexts:
            return None

        result = await query_module.search_memes_multi_pack(
            pack_contexts,
            config_module.PLUGIN_DATA_DIR,
            query,
            plugin._resolve_embedding_provider,
            top_k=3,
            min_score=max(0.0, min(float(min_score), 1.0)),
        )
        candidates = list(result.get("candidates") or []) if result else []
        if not candidates:
            return None
        records = query_module.candidate_records_multi_pack(pack_contexts, candidates)
        if not records:
            return None

        # validate_selected_id expects the candidate map on the event; keep the
        # bridge's bookkeeping separate from meme_manager's own selected IDs.
        old_candidates = event.get_extra("meme_manager_semantic_candidates")
        event.set_extra(
            "meme_manager_semantic_candidates",
            {str(item.get("id")): item for item in records if item.get("id")},
        )
        avoid = {str(item) for item in (avoid_paths or set()) if item}
        fallback: str | None = None
        try:
            for record in records:
                path = query_module.validate_selected_id(
                    event, str(record.get("id") or ""), plugin_context
                )
                if path is None:
                    continue
                path_text = str(path)
                if fallback is None:
                    fallback = path_text
                if not avoid or Path(path_text).name not in avoid:
                    return path_text
        finally:
            event.set_extra("meme_manager_semantic_candidates", old_candidates)
        return fallback
    except Exception as exc:
        logger.info(f"[表情桥接] 选图不可用: {exc}")
    return None
