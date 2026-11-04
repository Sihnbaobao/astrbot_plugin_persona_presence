"""Shared types for optional feature packs.

A feature pack (called a DLC internally) is a self-contained bundle that can be
switched off as a whole. Turning one off must leave the base reply flow intact:
no imports, hooks, timers or log lines from that pack should run.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ModuleManifest:
    """Static description of one feature pack.

    Attributes:
        key: Stable identifier used in config and logs.
        name: Human readable name shown in the settings page.
        summary: One sentence about what the pack adds.
        config_group: Config group owned by this pack.
        default_enabled: Whether a fresh install gets this pack.
        depends: Keys of packs that must be enabled together with this one.
    """

    key: str
    name: str
    summary: str
    config_group: str
    default_enabled: bool = False
    depends: list[str] = field(default_factory=list)


class PluginModule:
    """Base class for one feature pack.

    Packs only ever receive already-normalised facts, so they never need to
    reach into the base reply pipeline.

    Attributes:
        manifest: Static description of the pack.
    """

    manifest: ModuleManifest

    def __init__(self, context: Any = None) -> None:
        self.context = context

    async def on_message(self, event: Any, is_private: bool) -> None:
        """Observe one incoming message.

        Args:
            event: The AstrBot message event.
            is_private: Whether the message came from a private chat.
        """

    async def on_silence(self, event: Any) -> None:
        """Called after the base flow decided not to reply.

        Args:
            event: The AstrBot message event that stayed unanswered.
        """

    async def close(self) -> None:
        """Flush any state this pack keeps on disk."""
