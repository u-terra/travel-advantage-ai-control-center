"""ORCHESTRAVEL v1 plan limits - Telegram quota-gap fix.

Commit e705494 instrumented on_free_text but left on_task_after_button/
on_reply_subject_received (both go through the shared _route_and_dispatch ->
_maybe_send_module_result -> _maybe_send_draft tail) unable to receive
plan_quota_service at all. Both are real, reachable production paths:

- on_task_after_button is reached after a v1/v2 category button sets
  forced_module (see app.handlers.menu.on_category/on_v2_category) and the
  user then types free text - forced_module can be Module.CONTENT_FACTORY,
  which _route_and_dispatch/_maybe_send_draft treats as a regular-post
  (material) request, exactly like on_free_text's is_regular_post branch.
- on_reply_subject_received's own direct dispatch always forces
  Module.TRAVEL_ASSISTANT (client reply) - never material - kept here as a
  regression guard that "Ответить клиенту" still never spends the quota
  once plan_quota_service is wired through this second entry point too.

No competitor-analysis path exists anywhere in app.handlers.tasks (confirmed
by inspection: the only CompetitorIntelligenceService callers in this repo
are app.web_api and app.handlers.competitors, both already instrumented) -
there is nothing to fix here for competitor analysis.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.domain.partners import WorkspaceContext
from app.handlers.tasks import on_reply_subject_received, on_task_after_button
from app.routing.modules import Module
from app.services.llm.models import ContentDraft
from app.services.plan_quota_service import QuotaDecision
from tests.llm_fakes import FakeLLMProvider


def run(coro):
    return asyncio.run(coro)


def context(workspace_id: int = 42, telegram_user_id: int = 100) -> WorkspaceContext:
    return WorkspaceContext(telegram_user_id, workspace_id, "owner", "active")


class Message:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[tuple[str, dict]] = []
        self.chat = SimpleNamespace(id=555)

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return SimpleNamespace(chat=self.chat, message_id=1)


class State:
    def __init__(self, data=None) -> None:
        self.data = data or {}

    async def get_data(self):
        return self.data

    async def clear(self):
        self.data = {}

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, value):
        pass


def journal():
    return SimpleNamespace(add=AsyncMock(return_value=1), last=AsyncMock())


def profile_repository():
    return SimpleNamespace(
        get_business_profile=AsyncMock(return_value=None),
        get_user_preferences=AsyncMock(return_value=None),
    )


def artifact_repository(artifact_id: int = 901):
    return SimpleNamespace(
        create_artifact_with_initial_version=AsyncMock(
            return_value=(SimpleNamespace(id=artifact_id), object())
        )
    )


def quota_service(*, allowed: bool, message: str | None = None):
    return SimpleNamespace(
        check_material_quota=AsyncMock(return_value=QuotaDecision(allowed, message)),
        record_material_created=AsyncMock(),
    )


# ── material path via on_task_after_button -> _route_and_dispatch ─────────


def test_task_after_button_material_blocked_by_quota_never_calls_provider() -> None:
    provider = FakeLLMProvider(draft=ContentDraft("не должно появиться", ()))
    artifacts = artifact_repository()
    quota = quota_service(
        allowed=False,
        message="Лимит тарифа START: 15 материалов за 14 дней исчерпан.",
    )
    state = State({"forced_module": Module.CONTENT_FACTORY.value})

    run(on_task_after_button(
        Message("Нужен пост о путешествиях"), state, journal(), provider,
        context(), profile_repository(),
        artifact_repository=artifacts,
        plan_quota_service=quota,
    ))

    quota.check_material_quota.assert_awaited_once_with(42)
    provider.generate_draft.assert_not_called()  # expensive call never happened
    artifacts.create_artifact_with_initial_version.assert_not_awaited()
    quota.record_material_created.assert_not_awaited()


def test_task_after_button_material_allowed_calls_provider_and_records_once() -> None:
    provider = FakeLLMProvider(draft=ContentDraft("Черновик после кнопки", ()))
    artifacts = artifact_repository()
    quota = quota_service(allowed=True)
    state = State({"forced_module": Module.CONTENT_FACTORY.value})

    run(on_task_after_button(
        Message("Нужен пост о путешествиях"), state, journal(), provider,
        context(), profile_repository(),
        artifact_repository=artifacts,
        plan_quota_service=quota,
    ))

    provider.generate_draft.assert_called_once()
    artifacts.create_artifact_with_initial_version.assert_awaited_once()
    quota.record_material_created.assert_awaited_once_with(42)


def test_task_after_button_failed_generation_never_records() -> None:
    provider = FakeLLMProvider(draft=None)  # Content Factory returned nothing
    artifacts = artifact_repository()
    quota = quota_service(allowed=True)
    state = State({"forced_module": Module.CONTENT_FACTORY.value})

    run(on_task_after_button(
        Message("Нужен пост о путешествиях"), state, journal(), provider,
        context(), profile_repository(),
        artifact_repository=artifacts,
        plan_quota_service=quota,
    ))

    artifacts.create_artifact_with_initial_version.assert_not_awaited()
    quota.record_material_created.assert_not_awaited()


def test_task_after_button_no_quota_service_wired_is_unaffected() -> None:
    """plan_quota_service defaults to None (legacy behavior) - existing
    callers/tests that never pass it keep working exactly as before."""
    provider = FakeLLMProvider(draft=ContentDraft("Черновик", ()))
    artifacts = artifact_repository()
    state = State({"forced_module": Module.CONTENT_FACTORY.value})

    run(on_task_after_button(
        Message("Нужен пост о путешествиях"), state, journal(), provider,
        context(), profile_repository(),
        artifact_repository=artifacts,
    ))

    provider.generate_draft.assert_called_once()
    artifacts.create_artifact_with_initial_version.assert_awaited_once()


def test_route_and_dispatch_does_not_double_count_a_single_action() -> None:
    """One user action through on_task_after_button -> _route_and_dispatch ->
    _maybe_send_module_result -> _maybe_send_draft must record exactly once,
    never twice, regardless of how many layers the call passes through."""
    provider = FakeLLMProvider(draft=ContentDraft("Черновик", ()))
    artifacts = artifact_repository()
    quota = quota_service(allowed=True)
    state = State({"forced_module": Module.CONTENT_FACTORY.value})

    run(on_task_after_button(
        Message("Нужен пост о путешествиях"), state, journal(), provider,
        context(), profile_repository(),
        artifact_repository=artifacts,
        plan_quota_service=quota,
    ))

    assert quota.record_material_created.await_count == 1


# ── client-reply path via on_reply_subject_received -> _route_and_dispatch ──


def test_reply_subject_received_client_message_never_checks_material_quota() -> None:
    """A text that looks like an already-pasted client message dispatches
    straight to _route_and_dispatch with forced_module=TRAVEL_ASSISTANT
    (client reply) - «Ответить клиенту» must never touch material quota,
    even now that plan_quota_service is wired through this entry point."""
    provider = FakeLLMProvider(draft=ContentDraft("Черновик ответа клиенту", ()))
    quota = quota_service(allowed=True)
    state = State()
    work_repository = SimpleNamespace(get_or_create_subject=AsyncMock())

    run(on_reply_subject_received(
        Message("Здравствуйте! Можно ли оплатить бронирование картой МИР?"),
        state, journal(), provider, work_repository, context(),
        profile_repository(),
        plan_quota_service=quota,
    ))

    quota.check_material_quota.assert_not_awaited()
    quota.record_material_created.assert_not_awaited()
    work_repository.get_or_create_subject.assert_not_awaited()
