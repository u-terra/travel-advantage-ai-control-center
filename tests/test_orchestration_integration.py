"""Integration: the shadow orchestration call wired into
app.handlers.tasks.on_free_text never affects, delays before, or replaces
the user-facing reply - see the module docstring in app.orchestration.shadow
for the contract this exercises end to end."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.handlers.tasks import on_free_text
from app.domain.partners import WorkspaceContext
from app.orchestration.provider import NullOrchestrationLLMProvider, OrchestrationLLMProvider
from app.orchestration.shadow import ShadowComparisonLogger
from app.routing.modules import Module
from tests.llm_fakes import FakeLLMProvider


def run(value):
    return asyncio.run(value)


class State:
    def __init__(self, data=None):
        self.data = data or {}
        self.state = None

    async def get_data(self):
        return self.data

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_state(self):
        return self.state

    async def clear(self):
        self.data = {}
        self.state = None


class Message:
    def __init__(self, text: str, events: list[str] | None = None):
        self.text = text
        self.events = events if events is not None else []
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        self.events.append(f"reply:{text[:20]}")


def journal():
    return SimpleNamespace(add=AsyncMock(return_value=1), last=AsyncMock())


def context(workspace_id: int = 42, telegram_user_id: int = 100) -> WorkspaceContext:
    return WorkspaceContext(telegram_user_id, workspace_id, "owner", "active")


def profile_repository(profile=None):
    return SimpleNamespace(
        get_business_profile=AsyncMock(return_value=profile),
        get_user_preferences=AsyncMock(return_value=None),
    )


class OrderRecordingProvider(OrchestrationLLMProvider):
    name = "recording"

    def __init__(self, events: list[str]) -> None:
        self.events = events

    @property
    def is_configured(self) -> bool:
        return True

    def classify(self, *, request):
        self.events.append("shadow:classify")
        return {
            "intent": "create_content",
            "primary_module": Module.CONTENT_FACTORY.value,
            "secondary_modules": [],
            "safety_required": False,
            "uses_previous_turn": False,
            "needs_source_analysis": False,
            "needs_generation": True,
            "needs_clarification": False,
            "confidence": 0.9,
            "reason_code": "ok",
        }


class RecordingLogger(ShadowComparisonLogger):
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.records = []

    def log(self, record) -> None:
        self.events.append("shadow:logged")
        self.records.append(record)


def test_default_call_without_provider_is_unaffected():
    """No orchestration_llm_provider passed at all - the overwhelming
    majority of existing call sites/tests. Must behave exactly as before."""
    message = Message("Напиши пост про Travel Advantage")
    provider = FakeLLMProvider()
    run(on_free_text(message, journal(), provider, context(), profile_repository()))
    assert message.answers  # unaffected: still replies normally


def test_null_provider_never_calls_classify():
    events: list[str] = []
    message = Message("Напиши пост про Travel Advantage", events)
    provider = FakeLLMProvider()
    run(on_free_text(
        message, journal(), provider, context(), profile_repository(),
        orchestration_llm_provider=NullOrchestrationLLMProvider(),
    ))
    assert events == [event for event in events if not event.startswith("shadow:")]


def test_shadow_runs_strictly_after_the_user_reply_is_sent():
    events: list[str] = []
    message = Message("Напиши пост про Travel Advantage", events)
    provider = FakeLLMProvider()
    orchestration_provider = OrderRecordingProvider(events)

    import app.orchestration.shadow as shadow_module
    original = shadow_module.ShadowComparisonLogger
    recording_logger_holder = {}

    def _patched_logger():
        instance = RecordingLogger(events)
        recording_logger_holder["instance"] = instance
        return instance

    shadow_module.ShadowComparisonLogger = _patched_logger  # type: ignore[assignment]
    try:
        run(on_free_text(
            message, journal(), provider, context(), profile_repository(),
            orchestration_llm_provider=orchestration_provider,
        ))
    finally:
        shadow_module.ShadowComparisonLogger = original  # type: ignore[assignment]

    reply_events = [e for e in events if e.startswith("reply:")]
    shadow_events = [e for e in events if e.startswith("shadow:")]
    assert reply_events, "user must still get a reply"
    assert shadow_events == ["shadow:classify", "shadow:logged"]
    # Every reply happened before the shadow classify call - the whole point
    # of shadow mode being unable to affect or delay the user-facing answer.
    assert events.index(shadow_events[0]) > events.index(reply_events[-1])


def test_provider_raising_never_reaches_the_user():
    """Even a provider that raises outright must not surface anything to the
    user or break the handler - the wrapper in on_free_text is defense in
    depth on top of run_shadow_orchestration's own guarantee."""
    class ExplodingProvider(OrchestrationLLMProvider):
        name = "exploding"

        @property
        def is_configured(self) -> bool:
            return True

        def classify(self, *, request):
            raise RuntimeError("boom")

    message = Message("Напиши пост про Travel Advantage")
    provider = FakeLLMProvider()
    run(on_free_text(
        message, journal(), provider, context(), profile_repository(),
        orchestration_llm_provider=ExplodingProvider(),
    ))
    assert message.answers  # reply still went through, no exception propagated


def test_user_and_assistant_turns_are_recorded_for_the_next_message():
    state = State()
    message = Message("Напиши пост про Travel Advantage")
    provider = FakeLLMProvider()
    run(on_free_text(
        message, journal(), provider, context(), profile_repository(),
        state=state,
    ))
    from app.orchestration.context import recent_turns
    turns = run(recent_turns(state))
    roles = [t.role for t in turns]
    assert "user" in roles
    assert "assistant" in roles
