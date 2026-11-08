"""Shared contract for optional feature packs.

A feature pack (called a DLC internally) is a self-contained bundle that can be
switched off as a whole. Turning one off must leave the base reply flow intact:
no imports, hooks, timers or log lines from that pack should run.

Feature packs never receive the full AstrBot context. The base resolves the
persona, provider, time zone and chat scope first, then hands the pack a
read-only :class:`FeatureContext`. A pack may contribute context or propose a
line, but the base owns delivery, filtering, duplicate checks and history.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class FeatureContext:
    """Read-only view of one base turn handed to a feature pack.

    Attributes:
        event: The AstrBot message event that triggered this turn.
        umo: Unified message origin of the conversation.
        group_id: Group identifier, or an empty string in private chat.
        is_private: Whether this turn is a private conversation.
        chat_key: Scoped plugin chat key used by caches and throttles.
        persona: Persona prompt already resolved for this conversation.
        provider_id: Chat provider already resolved for this conversation.
        now: Time-zone-aware current time resolved by the base.
    """

    event: Any
    umo: str
    group_id: str
    is_private: bool
    chat_key: str
    persona: str
    provider_id: str
    now: datetime


@dataclass(frozen=True)
class Proposal:
    """A feature pack's request for the base to speak one line.

    Returning a proposal never guarantees delivery. The base still checks
    whether another reply is in flight, whether the line is a duplicate, and
    whether the conversation scope still allows the pack to speak.

    Attributes:
        text: The line the pack wants to say.
        source: Stable pack identifier, used in logs and policy checks.
        action: ``react`` for text, ``meme`` for an image bubble.
        meme_query: Semantic description used when action is ``meme``.
        reason: Short diagnostic reason for the proposal.
        group_id: Group the proposal belongs to, if any.
        created_at: Unix timestamp when the proposal was produced.
    """

    text: str
    source: str
    action: str = "react"
    meme_query: str = ""
    quote_id: str = ""
    reason: str = ""
    group_id: str = ""
    created_at: float = 0.0


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
        """Observe one incoming message (legacy hook).

        Args:
            event: The AstrBot message event.
            is_private: Whether the message came from a private chat.
        """

    async def on_silence(self, event: Any) -> None:
        """Called after the base flow decided not to reply (legacy hook).

        Args:
            event: The AstrBot message event that stayed unanswered.
        """

    async def on_event(self, ctx: FeatureContext) -> None:
        """Observe one base turn without producing side effects.

        Args:
            ctx: Read-only turn context resolved by the base.
        """

    async def contribute_context(self, ctx: FeatureContext) -> str:
        """Return optional background text for the base reply context.

        Args:
            ctx: Read-only turn context resolved by the base.

        Returns:
            A prompt block, or an empty string when the pack has nothing to add.
        """
        return ""

    async def propose(self, ctx: FeatureContext) -> Proposal | None:
        """Return one optional line for the base to consider sending.

        Args:
            ctx: Read-only turn context resolved by the base.

        Returns:
            A proposal, or None when the pack does not want to speak.
        """
        return None

    async def on_reply(self, ctx: FeatureContext, reply_text: str) -> None:
        """Learn from a reply the base actually sent.

        Args:
            ctx: Read-only turn context resolved by the base.
            reply_text: Text that reached the conversation.
        """

    async def on_proposal_sent(self, ctx: FeatureContext, proposal: Proposal) -> None:
        """Acknowledge that the base accepted and sent a proposal.

        Args:
            ctx: Read-only turn context resolved by the base.
            proposal: The proposal that was delivered.
        """

    async def close(self) -> None:
        """Flush any state this pack keeps on disk."""
