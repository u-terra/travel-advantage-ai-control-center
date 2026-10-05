"""Regression tests for two layered real prod bugs on the same follow-up
path:

1. A bare follow-up like "короче и мягче", "ещё вариант" or a one-word "да"
   sent in reply to the bot's own suggestion used to fall through
   route_text() as is_uncertain (no routing keyword of its own) even though
   the previous turn had already resolved a real module - the user saw
   "⚠️ Не удалось уверенно определить маршрут..." instead of a continuation.
   Fixed by app.handlers.tasks._recover_follow_up_module()/
   _is_reply_to_bot_message(), reading the same rolling conversation window
   (app.orchestration.context.recent_turns/record_turn) that
   _recover_rewrite_source_text already uses for a narrower case, plus the
   Telegram reply_to_message signal, which was never read anywhere before.

2. Once (1) was fixed, the module came back correctly (TRAVEL_ASSISTANT),
   but task_text itself stayed the bare "короче и мягче" - meaningless as a
   standalone question, so reference_resolver/generation found no relevant
   topic and the user got an irrelevant "нет подтверждённых данных" answer
   instead of an actual revision of the bot's own previous draft. Fixed by
   app.handlers.tasks._recover_assistant_response_follow_up(), which splices
   the real text of the bot's last assistant turn (now recorded for real by
   record_turn(), not as a technical "[module] ответ отправлен" label) in
   front of the short instruction, and by the new force_client_reply flag
   threaded through _maybe_send_module_result/_maybe_send_draft so the
   recovered follow-up never falls back to the INFORMATIONAL persona just
   because "короче и мягче" itself carries no ASSISTANT_INTENT_KEYWORDS
   phrase.

See the "Live prod bug" comments above these functions in
app/handlers/tasks.py for the exact priority rules this file verifies.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.handlers.tasks import (
    _CLIENT_REPLY_HEADING,
    _INFORMATIONAL_HEADING,
    _is_reply_to_bot_message,
    _recover_assistant_response_follow_up,
    _recover_follow_up_module,
    on_free_text,
)
from app.orchestration.context import record_turn, recent_turns
from app.routing.modules import Module
from app.services.llm.models import ContentDraft
from tests.llm_fakes import FakeLLMProvider
from tests.test_journal_handlers import Message, State, business_profile, context, journal, profile_repository


def run(coro):
    return asyncio.run(coro)


def _bot_reply_message(text: str, *, bot_id: int = 777) -> Message:
    message = Message(text)
    message.bot = SimpleNamespace(id=bot_id)
    message.reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=bot_id))
    return message


async def _seed_last_module(state: State, module: str) -> None:
    await record_turn(
        state, role="assistant", text="[module] ответ отправлен", module=module,
    )


# --- _is_reply_to_bot_message: the explicit reply signal -------------------


def test_is_reply_to_bot_message_true_when_sender_matches_bot_id() -> None:
    assert _is_reply_to_bot_message(_bot_reply_message("да")) is True


def test_is_reply_to_bot_message_false_without_any_reply() -> None:
    assert _is_reply_to_bot_message(Message("да")) is False


def test_is_reply_to_bot_message_false_when_reply_is_to_another_user() -> None:
    message = Message("да")
    message.bot = SimpleNamespace(id=777)
    message.reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=111))
    assert _is_reply_to_bot_message(message) is False


# --- _recover_follow_up_module: priority rules ------------------------------


def test_short_natural_follow_ups_continue_last_real_module_without_reply() -> None:
    state = State()
    run(_seed_last_module(state, Module.TRAVEL_ASSISTANT.value))
    for phrase in ("короче и мягче", "короче", "мягче", "ещё вариант", "сделай деловее"):
        recovered = run(_recover_follow_up_module(state, Message(phrase), phrase))
        assert recovered is Module.TRAVEL_ASSISTANT, phrase


def test_reply_to_bot_with_da_continues_last_real_module() -> None:
    state = State()
    run(_seed_last_module(state, Module.TRAVEL_ASSISTANT.value))
    message = _bot_reply_message("да")
    recovered = run(_recover_follow_up_module(state, message, "да"))
    assert recovered is Module.TRAVEL_ASSISTANT


def test_reply_signal_works_even_past_the_short_text_cap() -> None:
    """Priority #2 (Telegram reply) is independent of priority #3's length
    cap - a longer reply to the bot must still continue the module, proving
    the two signals are genuinely separate triggers, not the same check."""
    state = State()
    run(_seed_last_module(state, Module.TRAVEL_ASSISTANT.value))
    long_reply = "Пожалуйста, сформулируй это ещё мягче для ответа в личной переписке"
    assert len(long_reply) > 60
    message = _bot_reply_message(long_reply)
    recovered = run(_recover_follow_up_module(state, message, long_reply))
    assert recovered is Module.TRAVEL_ASSISTANT


def test_follow_up_without_any_prior_turn_stays_none() -> None:
    state = State()
    recovered = run(_recover_follow_up_module(state, Message("да"), "да"))
    assert recovered is None


def test_follow_up_after_uncertain_assistant_turn_stays_none() -> None:
    """The bot's own uncertain-route reply is itself recorded with
    module=Module.ORCHESTRATOR (see record_turn() call in on_free_text) -
    that must not be treated as a "known" module to continue, or a single
    uncertain route would make every short reply after it uncertain too."""
    state = State()
    run(_seed_last_module(state, Module.ORCHESTRATOR.value))
    recovered = run(_recover_follow_up_module(state, Message("да"), "да"))
    assert recovered is None


def test_long_unrelated_message_without_reply_does_not_stick_to_old_module() -> None:
    """A substantial, content-bearing message happens to follow an
    assistant turn, but is long and not a Telegram reply - must not be
    treated as a follow-up just because it is adjacent in the conversation."""
    state = State()
    run(_seed_last_module(state, Module.TRAVEL_ASSISTANT.value))
    long_text = (
        "Напишите, пожалуйста, подробный план публикаций на следующий месяц "
        "с учётом сезонности и акций по разным направлениям"
    )
    assert len(long_text) > 60
    recovered = run(_recover_follow_up_module(state, Message(long_text), long_text))
    assert recovered is None


# --- end-to-end through on_free_text ----------------------------------------


def test_short_follow_up_reaches_generation_instead_of_uncertain_warning() -> None:
    state = State()
    run(_seed_last_module(state, Module.TRAVEL_ASSISTANT.value))
    provider = FakeLLMProvider(draft=ContentDraft("Короче и мягче.", ()))
    profiles = profile_repository(business_profile())
    j = journal()
    message = Message("короче и мягче")
    run(on_free_text(message, j, provider, context(), profiles, state=state))

    assert j.add.call_args.kwargs["primary_module"] == Module.TRAVEL_ASSISTANT.value
    texts = [text for text, _ in message.answers]
    assert not any("Не удалось уверенно определить маршрут" in text for text in texts)
    provider.generate_draft.assert_called_once()


def test_explicit_new_task_after_known_module_picks_its_own_route() -> None:
    """route_text() confidently resolves this on its own (an explicit
    Content Factory keyword) - the follow-up recovery must never even
    engage, regardless of what the previous module happened to be."""
    state = State()
    run(_seed_last_module(state, Module.TRAVEL_ASSISTANT.value))
    provider = FakeLLMProvider(draft=ContentDraft("Пост про раннее бронирование.", ()))
    profiles = profile_repository(business_profile())
    j = journal()
    message = Message("Напиши пост про раннее бронирование туров")
    run(on_free_text(message, j, provider, context(), profiles, state=state))

    assert j.add.call_args.kwargs["primary_module"] == Module.CONTENT_FACTORY.value


def test_follow_up_recovery_behaves_identically_for_club_partner_and_agency() -> None:
    """The fix lives entirely in app.routing/app.orchestration.context and
    never branches on business_type - this guards that no TA-specific
    behavior was accidentally introduced."""
    for business_type in ("club_partner", "agency"):
        state = State()
        run(_seed_last_module(state, Module.TRAVEL_ASSISTANT.value))
        provider = FakeLLMProvider(draft=ContentDraft("Короче и мягче.", ()))
        profiles = profile_repository(business_profile(business_type=business_type))
        j = journal()
        run(on_free_text(
            Message("короче и мягче"), j, provider, context(), profiles, state=state,
        ))
        assert j.add.call_args.kwargs["primary_module"] == Module.TRAVEL_ASSISTANT.value, business_type


# ============================================================================
# Part 2: restoring the actual PREVIOUS DRAFT, not just the module - see the
# module docstring's item (2). Everything below exercises
# _recover_assistant_response_follow_up end to end through on_free_text,
# using two REAL calls (not a seeded turn) so the scenario matches the exact
# live prod report: a genuine client-reply draft, then a short revision.
# ============================================================================

_ORIGINAL_QUESTION = (
    "Человек спрашивает: «Travel Adventis — это сетевой маркетинг?» "
    "Что мне ему ответить?"
)
# No trailing assistant-offer phrasing ("Могу...", "Если хотите...") - must
# survive strip_assistant_tail() unchanged, so the splice assertions below
# can look for this exact string.
_ORIGINAL_DRAFT_TEXT = (
    "Это не классический сетевой маркетинг, а партнёрская программа Travel "
    "Adventis со своей структурой вознаграждений."
)


def _run_client_reply_then_followup(followup_text: str, *, message_factory=Message):
    state = State()
    first_provider = FakeLLMProvider(draft=ContentDraft(_ORIGINAL_DRAFT_TEXT, ()))
    profiles = profile_repository(business_profile())
    run(on_free_text(
        Message(_ORIGINAL_QUESTION), journal(), first_provider, context(), profiles,
        state=state,
    ))

    followup_journal = journal()
    followup_provider = FakeLLMProvider(draft=ContentDraft("Переработанный вариант.", ()))
    followup_message = message_factory(followup_text)
    run(on_free_text(
        followup_message, followup_journal, followup_provider, context(), profiles,
        state=state,
    ))
    return followup_journal, followup_message, followup_provider


def test_original_client_reply_draft_is_recorded_as_real_text_not_a_label() -> None:
    """Requirement (1): record_turn for a successful assistant response in
    this flow must store the actual sent draft, not "[module] ответ
    отправлен"."""
    state = State()
    provider = FakeLLMProvider(draft=ContentDraft(_ORIGINAL_DRAFT_TEXT, ()))
    profiles = profile_repository(business_profile())
    run(on_free_text(
        Message(_ORIGINAL_QUESTION), journal(), provider, context(), profiles, state=state,
    ))

    turns = run(recent_turns(state))
    assistant_turns = [turn for turn in turns if turn.role == "assistant"]
    assert assistant_turns, "no assistant turn was recorded"
    assert assistant_turns[-1].text == _ORIGINAL_DRAFT_TEXT
    assert assistant_turns[-1].module == Module.TRAVEL_ASSISTANT.value


def test_short_revision_phrases_rewrite_the_previous_draft_not_a_new_task() -> None:
    for phrase in (
        "короче и мягче", "короче", "мягче", "ещё вариант", "деловее", "без давления",
    ):
        followup_journal, followup_message, followup_provider = (
            _run_client_reply_then_followup(phrase)
        )
        logged_task_text = followup_journal.add.call_args.kwargs["task_text"]
        assert _ORIGINAL_DRAFT_TEXT in logged_task_text, phrase
        assert phrase in logged_task_text, phrase
        assert (
            followup_journal.add.call_args.kwargs["primary_module"]
            == Module.TRAVEL_ASSISTANT.value
        ), phrase

        texts = [text for text, _ in followup_message.answers]
        # Requirement (4): must stay in the CLIENT_REPLY persona, never fall
        # back to INFORMATIONAL just because the bare phrase itself carries
        # no ASSISTANT_INTENT_KEYWORDS.
        assert any(_CLIENT_REPLY_HEADING in text for text in texts), phrase
        assert not any(_INFORMATIONAL_HEADING in text for text in texts), phrase
        assert not any(
            "Не удалось уверенно определить маршрут" in text for text in texts
        ), phrase
        followup_provider.generate_draft.assert_called_once()


def test_reply_to_bot_with_da_rewrites_the_previous_draft() -> None:
    followup_journal, followup_message, followup_provider = _run_client_reply_then_followup(
        "да", message_factory=_bot_reply_message,
    )
    logged_task_text = followup_journal.add.call_args.kwargs["task_text"]
    assert _ORIGINAL_DRAFT_TEXT in logged_task_text
    assert (
        followup_journal.add.call_args.kwargs["primary_module"] == Module.TRAVEL_ASSISTANT.value
    )
    texts = [text for text, _ in followup_message.answers]
    assert any(_CLIENT_REPLY_HEADING in text for text in texts)
    assert not any(_INFORMATIONAL_HEADING in text for text in texts)
    followup_provider.generate_draft.assert_called_once()


def test_bare_da_without_reply_signal_stays_uncertain_even_after_client_reply() -> None:
    """Requirement (6): a standalone "да" with no Telegram-reply signal must
    not be guessed as a continuation, even though the previous turn has a
    perfectly good known module/draft available."""
    followup_journal, followup_message, followup_provider = _run_client_reply_then_followup(
        "да", message_factory=Message,
    )
    texts = [text for text, _ in followup_message.answers]
    assert any("Не удалось уверенно определить маршрут" in text for text in texts)
    followup_provider.generate_draft.assert_not_called()


def test_explicit_new_question_after_a_draft_does_not_absorb_the_old_draft() -> None:
    """Requirement (5): route_text() confidently resolving a brand-new,
    unrelated client question on its own must never trigger the splice -
    the new task_text must reach generation on its own, without the old
    draft glued in front of it."""
    followup_journal, followup_message, followup_provider = _run_client_reply_then_followup(
        "Клиент спрашивает про стоимость Elite Turbo"
    )
    logged_task_text = followup_journal.add.call_args.kwargs["task_text"]
    assert _ORIGINAL_DRAFT_TEXT not in logged_task_text
    assert logged_task_text == "Клиент спрашивает про стоимость Elite Turbo"
    assert (
        followup_journal.add.call_args.kwargs["primary_module"] == Module.TRAVEL_ASSISTANT.value
    )


def test_existing_pasted_post_rewrite_flow_is_unaffected() -> None:
    """Regression guard: the OLDER _recover_rewrite_source_text mechanism
    (restoring the last USER turn for an explicit "перепиши"/"сократи" on a
    pasted post) must keep working exactly as before - this fix only adds a
    parallel path for the bot's OWN previous draft, never replacing it."""
    state = State()
    pasted_post = (
        "Нужно переписать пост чтобы не обвинили в плагиате: Эксклюзивная "
        "скидка 30% только сегодня на бронирование отеля в Дубае"
    )
    first_provider = FakeLLMProvider(draft=ContentDraft("Первый черновик", ()))
    profiles = profile_repository(business_profile())
    run(on_free_text(
        Message(pasted_post), journal(), first_provider, context(), profiles, state=state,
    ))

    followup_journal = journal()
    followup_provider = FakeLLMProvider(draft=ContentDraft("Второй черновик", ()))
    followup_text = "Это достоверная информация. Просто перепиши"
    run(on_free_text(
        Message(followup_text), followup_journal, followup_provider, context(), profiles,
        state=state,
    ))

    logged_task_text = followup_journal.add.call_args.kwargs["task_text"]
    assert "30%" in logged_task_text
    assert followup_text in logged_task_text
    followup_provider.generate_draft.assert_called_once()


def test_assistant_response_follow_up_behaves_identically_for_club_partner_and_agency() -> None:
    for business_type in ("club_partner", "agency"):
        state = State()
        first_provider = FakeLLMProvider(draft=ContentDraft(_ORIGINAL_DRAFT_TEXT, ()))
        profiles = profile_repository(business_profile(business_type=business_type))
        run(on_free_text(
            Message(_ORIGINAL_QUESTION), journal(), first_provider, context(), profiles,
            state=state,
        ))

        followup_journal = journal()
        followup_provider = FakeLLMProvider(draft=ContentDraft("Переработанный вариант.", ()))
        run(on_free_text(
            Message("короче и мягче"), followup_journal, followup_provider, context(), profiles,
            state=state,
        ))

        logged_task_text = followup_journal.add.call_args.kwargs["task_text"]
        assert _ORIGINAL_DRAFT_TEXT in logged_task_text, business_type
        assert (
            followup_journal.add.call_args.kwargs["primary_module"]
            == Module.TRAVEL_ASSISTANT.value
        ), business_type
