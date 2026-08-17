"""Обязательный Business Onboarding: cutoff, missing-fields, полный flow.

Использует существующий BusinessProfile/BusinessProfileService — никаких
новых таблиц. practical_stage и user display name здесь не участвуют
(намеренно не реализованы на этом этапе).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.config import load_settings
from app.domain.business_profiles import BusinessClaim, BusinessContext, BusinessProfile
from app.domain.partners import WorkspaceContext
from app.handlers.daily_actions import BTN_V2_DAILY_ACTIONS as _  # noqa: F401  (import sanity)
from app.handlers.onboarding import (
    BusinessOnboarding,
    enter_onboarding_gate,
    missing_onboarding_fields,
    on_onboarding_business_type_selected,
    on_onboarding_field_answer,
    on_onboarding_gate_intercept,
    on_onboarding_gate_intercept_callback,
    start_business_onboarding,
)
from app.keyboards import ONBOARDING_BUSINESS_TYPE_PREFIX
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository, empty_business_context
from app.repositories.work_repository import WorkRepository
from app.services.business_profile_context import is_onboarding_required

# ── shared helpers (mirrors tests/test_reply_work_memory.py style) ─────────


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Message:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[tuple[str, dict]] = []
        self.edited_reply_markups: list[Any] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))

    async def edit_reply_markup(self, reply_markup: Any = None) -> None:
        self.edited_reply_markups.append(reply_markup)


class _Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _Message()
        self.answers: list[tuple[Any, dict]] = []

    async def answer(self, text: Any = None, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class _State:
    def __init__(self, data: dict | None = None) -> None:
        self.data: dict[str, Any] = data or {}
        self.state: Any = None

    async def get_data(self) -> dict:
        return self.data

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def set_state(self, state: Any) -> None:
        self.state = state

    async def clear(self) -> None:
        self.data = {}
        self.state = None


def _ctx(workspace_id: int, role: str = "owner") -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, role, "active")


def _stack(tmp_path: Path) -> tuple[PartnerRepository, WorkRepository, ArtifactRepository]:
    db_path = tmp_path / "workspace.sqlite3"
    partners = PartnerRepository(db_path)
    _run(partners.init())
    work_repo = WorkRepository(db_path)
    _run(work_repo.init())
    artifact_repo = ArtifactRepository(db_path)
    _run(artifact_repo.init())
    return partners, work_repo, artifact_repo


def _provision(
    partners: PartnerRepository,
    telegram_user_id: int,
    slug: str,
    *,
    # provision_partner требует непустой business_name всегда (реальное
    # ограничение validate_business_profile_input) — "пустой" onboarding-
    # сценарий недостижим через провижининг, поэтому дефолт здесь — минимальный
    # placeholder, как мог бы ввести администратор, а не пустая строка.
    business_name: str = "Черновой профиль",
    business_type: str = "other",
    short_description: str = "",
    specializations: list[str] | None = None,
    ta_affiliated: bool = False,
) -> int:
    context = empty_business_context()
    context["specializations"] = specializations or []
    provisioned = _run(partners.provision_partner(
        telegram_user_id, slug, slug,
        business_name=business_name, business_type=business_type,
        short_description=short_description, context=context,
        ta_affiliated=ta_affiliated,
    ))
    return provisioned.workspace.id


# ── 1-3. is_onboarding_required: cutoff logic ───────────────────────────────


def test_legacy_incomplete_before_cutoff_is_not_required() -> None:
    now = datetime.now(timezone.utc)
    required = is_onboarding_required(
        profile_status="incomplete",
        workspace_created_at=(now - timedelta(days=30)).isoformat(),
        rollout_at=now,
    )
    assert required is False


def test_new_incomplete_after_cutoff_is_required() -> None:
    now = datetime.now(timezone.utc)
    required = is_onboarding_required(
        profile_status="incomplete",
        workspace_created_at=(now + timedelta(minutes=1)).isoformat(),
        rollout_at=now,
    )
    assert required is True


def test_usable_after_cutoff_is_never_required() -> None:
    now = datetime.now(timezone.utc)
    required = is_onboarding_required(
        profile_status="usable",
        workspace_created_at=(now + timedelta(days=1)).isoformat(),
        rollout_at=now,
    )
    assert required is False


def test_unset_rollout_at_never_requires_onboarding() -> None:
    """Безопасное поведение при unset ONBOARDING_ROLLOUT_AT: никто не
    блокируется, даже совсем свежий incomplete workspace."""
    required = is_onboarding_required(
        profile_status="incomplete",
        workspace_created_at=datetime.now(timezone.utc).isoformat(),
        rollout_at=None,
    )
    assert required is False


def test_updated_at_does_not_participate_in_the_decision() -> None:
    """Функция вообще не принимает updated_at — сигнатура сама это
    гарантирует, но явно фиксируем это тестом на None-профиль (отсутствие
    строки), где updated_at не может влиять по определению."""
    now = datetime.now(timezone.utc)
    required = is_onboarding_required(
        profile_status=None,
        workspace_created_at=(now - timedelta(days=1)).isoformat(),
        rollout_at=now,
    )
    assert required is False  # создан до cutoff — не требуется, несмотря на None-профиль


def test_created_at_exactly_at_cutoff_requires_onboarding() -> None:
    """Граница включительно: created_at == rollout_at — онбординг обязателен."""
    now = datetime.now(timezone.utc)
    required = is_onboarding_required(
        profile_status="incomplete",
        workspace_created_at=now.isoformat(),
        rollout_at=now,
    )
    assert required is True


# ── Settings: ONBOARDING_ROLLOUT_AT parsing (timezone safety) ──────────────


def _settings_with_rollout(
    monkeypatch: pytest.MonkeyPatch, raw: str | None
):
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", "586249067")
    if raw is None:
        monkeypatch.delenv("ONBOARDING_ROLLOUT_AT", raising=False)
    else:
        monkeypatch.setenv("ONBOARDING_ROLLOUT_AT", raw)
    return load_settings()


def test_settings_parses_timezone_aware_onboarding_rollout_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_rollout(monkeypatch, "2026-01-01T00:00:00+03:00")
    assert settings.onboarding_rollout_at is not None
    assert settings.onboarding_rollout_at.tzinfo is not None
    assert settings.onboarding_rollout_at == datetime(
        2026, 1, 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=3))
    )


def test_settings_normalizes_naive_onboarding_rollout_at_to_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Наивная (без смещения) строка в ONBOARDING_ROLLOUT_AT не должна ронять
    старт бота — приводится к UTC, а не остаётся naive (что привело бы к
    TypeError при сравнении с workspace.created_at, которое всегда aware)."""
    settings = _settings_with_rollout(monkeypatch, "2026-01-01T00:00:00")
    assert settings.onboarding_rollout_at == datetime(2026, 1, 1, tzinfo=timezone.utc)

    # created_at в БД всегда aware (см. partner_repository._now()) — сравнение
    # не должно бросать TypeError на смешении naive/aware.
    required = is_onboarding_required(
        profile_status="incomplete",
        workspace_created_at=datetime(2026, 6, 1, tzinfo=timezone.utc).isoformat(),
        rollout_at=settings.onboarding_rollout_at,
    )
    assert required is True


def test_settings_unset_onboarding_rollout_at_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_rollout(monkeypatch, None)
    assert settings.onboarding_rollout_at is None


# ── missing_onboarding_fields: не переспрашивать заполненное ───────────────


def _profile(
    *, business_name: str = "", business_type: str = "other",
    short_description: str = "", specializations: tuple[str, ...] = (),
    ta_affiliated: bool = False,
) -> BusinessProfile:
    context = BusinessContext(
        specializations=specializations, destinations=(), audiences=(), markets=(),
        positioning={"statement": "", "value_proposition": "", "differentiators": ()},
        communication={
            "tone": "", "formality": "", "emoji_preference": "",
            "preferred_terms": (), "banned_formulations": (), "cta_preference": "",
        },
        goals=(),
        content_preferences={"formats": (), "channels": (), "preferred_topics": (), "prohibited_topics": ()},
        public_contacts={}, claims=(),
    )
    return BusinessProfile(
        id=1, workspace_id=42, business_name=business_name, business_type=business_type,
        short_description=short_description, profile_status="incomplete",
        schema_version=1, revision=1, context=context,
        created_at="now", updated_at="now", ta_affiliated=ta_affiliated,
    )


def test_existing_business_name_is_not_asked_again() -> None:
    profile = _profile(business_name="Мой проект", business_type="agency")
    assert "business_name" not in missing_onboarding_fields(profile)


def test_existing_short_description_is_not_asked_again() -> None:
    profile = _profile(short_description="Помогаю с турами", business_type="agency")
    assert "short_description" not in missing_onboarding_fields(profile)


def test_existing_specializations_are_not_asked_again() -> None:
    profile = _profile(specializations=("круизы",), business_type="agency")
    assert "specializations" not in missing_onboarding_fields(profile)


def test_ta_never_asks_business_type_or_lets_it_be_missing() -> None:
    profile = _profile(business_type="club_partner", ta_affiliated=True)
    assert "business_type" not in missing_onboarding_fields(profile)


def test_independent_with_invalid_business_type_is_asked() -> None:
    profile = _profile(business_type="other", ta_affiliated=False)
    assert "business_type" in missing_onboarding_fields(profile)


def test_independent_with_valid_business_type_is_not_asked() -> None:
    for valid in ("independent_agent", "agency", "travel_company"):
        profile = _profile(business_type=valid, ta_affiliated=False)
        assert "business_type" not in missing_onboarding_fields(profile)


def test_fully_filled_profile_has_no_missing_fields() -> None:
    profile = _profile(
        business_name="X", business_type="agency", short_description="Y",
        specializations=("круизы",),
    )
    assert missing_onboarding_fields(profile) == []


# ── full flow: TA workspace ──────────────────────────────────────────────


def test_ta_onboarding_skips_business_type_and_prefilled_fields(tmp_path: Path) -> None:
    partners, work_repo, artifacts = _stack(tmp_path)
    workspace_id = _provision(
        partners, 111, "ta-ws", business_name="Уже задано",
        business_type="club_partner", ta_affiliated=True,
    )
    profile = _run(partners.get_business_profile(workspace_id))

    message = _Message()
    state = _State()
    _run(start_business_onboarding(message, state, profile))

    assert state.state == BusinessOnboarding.waiting_for_field
    assert state.data["field"] == "short_description"  # business_name/type пропущены
    prompt_texts = "\n".join(text for text, _ in message.answers)
    assert "Расскажите в 1" in prompt_texts
    assert "Как вас зовут" not in prompt_texts


def test_ta_onboarding_completes_and_becomes_usable(tmp_path: Path) -> None:
    partners, work_repo, artifacts = _stack(tmp_path)
    workspace_id = _provision(
        partners, 111, "ta-ws2", business_name="Проект", business_type="club_partner",
        ta_affiliated=True,
    )
    context_obj = _ctx(workspace_id)
    state = _State({"field": "short_description"})
    await_msg = _Message("Помогаю с путешествиями через Travel Advantage-формат")

    _run(on_onboarding_field_answer(
        await_msg, state, context_obj, partners, work_repo, artifacts,
    ))
    assert state.data["field"] == "specializations"

    spec_msg = _Message("круизы, семейные путешествия")
    _run(on_onboarding_field_answer(
        spec_msg, state, context_obj, partners, work_repo, artifacts,
    ))

    profile = _run(partners.get_business_profile(workspace_id))
    assert profile.profile_status == "usable"
    assert profile.ta_affiliated is True
    assert list(profile.context.specializations) == ["круизы", "семейные путешествия"]
    assert state.state is None  # FSM очищен после завершения


def test_ta_cannot_change_ta_affiliated_via_onboarding(tmp_path: Path) -> None:
    """ta_affiliated не входит ни в один onboarding-payload — update_business_profile
    его вообще не трогает (проверено на уровне репозитория раньше); здесь
    фиксируем это end-to-end."""
    partners, work_repo, artifacts = _stack(tmp_path)
    workspace_id = _provision(
        partners, 111, "ta-ws3", business_name="Проект", business_type="club_partner",
        ta_affiliated=True,
    )
    context_obj = _ctx(workspace_id)
    state = _State({"field": "short_description"})
    _run(on_onboarding_field_answer(
        _Message("Короткое описание"), state, context_obj, partners, work_repo, artifacts,
    ))
    _run(on_onboarding_field_answer(
        _Message("круизы"), state, context_obj, partners, work_repo, artifacts,
    ))
    profile = _run(partners.get_business_profile(workspace_id))
    assert profile.ta_affiliated is True


# ── full flow: independent workspace ────────────────────────────────────


def test_independent_onboarding_asks_business_type_via_buttons(tmp_path: Path) -> None:
    partners, work_repo, artifacts = _stack(tmp_path)
    workspace_id = _provision(
        partners, 222, "indep-ws", business_name="Тревел Клуб",
        short_description="Организуем туры", specializations=["круизы"],
    )
    profile = _run(partners.get_business_profile(workspace_id))

    message = _Message()
    state = _State()
    _run(start_business_onboarding(message, state, profile))

    # Остальные поля уже заполнены — единственное недостающее для
    # independent-профиля (business_type="other" по умолчанию невалиден).
    assert state.data["field"] == "business_type"


def test_independent_business_type_button_advances_flow(tmp_path: Path) -> None:
    partners, work_repo, artifacts = _stack(tmp_path)
    workspace_id = _provision(
        partners, 222, "indep-ws2", business_name="Тревел Клуб",
        short_description="Организуем туры", specializations=["круизы"],
    )
    context_obj = _ctx(workspace_id)
    state = _State({"field": "business_type"})
    callback = _Callback(f"{ONBOARDING_BUSINESS_TYPE_PREFIX}agency")

    _run(on_onboarding_business_type_selected(
        callback, state, context_obj, partners, work_repo, artifacts,
    ))

    profile = _run(partners.get_business_profile(workspace_id))
    assert profile.business_type == "agency"
    assert profile.profile_status == "usable"


def test_independent_onboarding_never_mentions_ta_wording(tmp_path: Path) -> None:
    partners, work_repo, artifacts = _stack(tmp_path)
    workspace_id = _provision(partners, 222, "indep-ws3")
    profile = _run(partners.get_business_profile(workspace_id))

    message = _Message()
    state = _State()
    _run(start_business_onboarding(message, state, profile))
    _run(on_onboarding_field_answer(
        _Message("Мой Тревел Клуб"), state, _ctx(workspace_id), partners, work_repo, artifacts,
    ))
    callback = _Callback(f"{ONBOARDING_BUSINESS_TYPE_PREFIX}independent_agent")
    _run(on_onboarding_business_type_selected(
        callback, state, _ctx(workspace_id), partners, work_repo, artifacts,
    ))
    _run(on_onboarding_field_answer(
        _Message("Организую туры для семей"), state, _ctx(workspace_id), partners, work_repo, artifacts,
    ))
    _run(on_onboarding_field_answer(
        _Message("семейные путешествия"), state, _ctx(workspace_id), partners, work_repo, artifacts,
    ))

    all_text = "\n".join(text for text, _ in message.answers)
    all_text += "\n".join(text for text, _ in callback.message.answers)
    for forbidden in ("Travel Advantage", "Carbon", "Xlife"):
        assert forbidden not in all_text


# ── create vs update: no duplicate profile row ──────────────────────────


def test_onboarding_updates_existing_row_never_creates_second(tmp_path: Path) -> None:
    partners, work_repo, artifacts = _stack(tmp_path)
    workspace_id = _provision(partners, 333, "upd-ws")
    before = _run(partners.get_business_profile(workspace_id))
    assert before.id is not None
    original_id = before.id

    context_obj = _ctx(workspace_id)
    state = _State({"field": "business_name"})
    _run(on_onboarding_field_answer(
        _Message("Название"), state, context_obj, partners, work_repo, artifacts,
    ))

    after = _run(partners.get_business_profile(workspace_id))
    assert after.id == original_id  # тот же id — не вторая строка
    assert after.revision == before.revision + 1


# ── role safety: member cannot edit ─────────────────────────────────────


def test_member_cannot_start_onboarding_and_sees_blocked_message(tmp_path: Path) -> None:
    partners, work_repo, artifacts = _stack(tmp_path)
    workspace_id = _provision(partners, 444, "member-ws")
    profile = _run(partners.get_business_profile(workspace_id))

    message = _Message()
    state = _State()
    _run(enter_onboarding_gate(message, state, _ctx(workspace_id, role="member"), profile))

    assert state.state is None
    text = message.answers[0][0]
    assert "владелец" in text.lower() or "администратор" in text.lower()

    unchanged = _run(partners.get_business_profile(workspace_id))
    assert unchanged.revision == profile.revision


def test_owner_can_start_onboarding(tmp_path: Path) -> None:
    partners, work_repo, artifacts = _stack(tmp_path)
    workspace_id = _provision(partners, 555, "owner-ws")
    profile = _run(partners.get_business_profile(workspace_id))

    message = _Message()
    state = _State()
    _run(enter_onboarding_gate(message, state, _ctx(workspace_id, role="owner"), profile))

    assert state.state == BusinessOnboarding.waiting_for_field


# ── centralized gate: cannot be bypassed by free text ───────────────────


def test_gate_intercepts_free_text_and_starts_onboarding_for_owner(tmp_path: Path) -> None:
    partners, work_repo, artifacts = _stack(tmp_path)
    workspace_id = _provision(partners, 666, "gate-ws")
    profile = _run(partners.get_business_profile(workspace_id))

    message = _Message("☀️ Что делать сегодня")  # пытается нажать кнопку меню
    state = _State()
    _run(on_onboarding_gate_intercept(
        message, state, _ctx(workspace_id), onboarding_profile=profile,
    ))

    assert state.state == BusinessOnboarding.waiting_for_field


def test_gate_intercepts_stray_callback_without_mutating_anything(tmp_path: Path) -> None:
    callback = _Callback("daily_action:done:5")
    _run(on_onboarding_gate_intercept_callback(callback))
    assert callback.answers[0][1] == {"show_alert": True}


def test_gate_does_not_fire_without_workspace_context() -> None:
    message = _Message("что угодно")
    state = _State()
    _run(on_onboarding_gate_intercept(message, state, None))
    assert message.answers == []
    assert state.state is None


def test_real_router_order_gate_intercepts_v2_button_before_menu() -> None:
    """Тот же приём, что и test_daily_actions_button_reaches_handler_in_real_registration_order:
    реальный список router-объектов из app/handlers/__init__.py, без
    повторного вызова build_router() (module-level singletons)."""
    from app.handlers import competitors as competitors_handlers
    from app.handlers import daily_actions as daily_actions_handlers
    from app.handlers import menu as menu_handlers
    from app.handlers import onboarding as onboarding_handlers
    from app.keyboards import BTN_V2_DAILY_ACTIONS

    real_order = (
        onboarding_handlers.router,
        menu_handlers.router,
        daily_actions_handlers.router,
        competitors_handlers.router,
    )

    async def _first_match(text: str, **workflow_data: Any) -> str | None:
        message = _Message(text)
        data = {"raw_state": None, **workflow_data}
        for sub_router in real_order:
            for handler in sub_router.message.handlers:
                matched, _ = await handler.check(message, **data)
                if matched:
                    return handler.callback.__name__
        return None

    handler_name = _run(_first_match(
        BTN_V2_DAILY_ACTIONS, v2_menu_enabled=True, onboarding_required=True,
    ))
    assert handler_name == "on_onboarding_gate_intercept"

    # Без onboarding_required та же кнопка доходит до show_daily_actions как раньше.
    handler_name_normal = _run(_first_match(
        BTN_V2_DAILY_ACTIONS, v2_menu_enabled=True, onboarding_required=False,
    ))
    assert handler_name_normal == "show_daily_actions"


def test_onboarding_fsm_itself_is_not_blocked_by_the_gate() -> None:
    """Пока пользователь реально отвечает на текущий вопрос (state ==
    BusinessOnboarding.waiting_for_field), catch-all не должен его
    перехватывать — специфичный хендлер зарегистрирован раньше catch-all
    в том же роутере."""
    from app.handlers import onboarding as onboarding_handlers

    handlers_in_order = [h.callback.__name__ for h in onboarding_handlers.router.message.handlers]
    assert handlers_in_order.index("on_onboarding_field_answer") < handlers_in_order.index(
        "on_onboarding_gate_intercept"
    )


# ── after completion: DailyActions opens ────────────────────────────────


def test_completion_opens_daily_actions_without_llm(tmp_path: Path) -> None:
    partners, work_repo, artifacts = _stack(tmp_path)
    workspace_id = _provision(
        partners, 777, "final-ws", business_name="Готово",
        business_type="agency", short_description="Описание",
    )
    context_obj = _ctx(workspace_id)
    state = _State({"field": "specializations"})

    message = _Message("круизы")
    _run(on_onboarding_field_answer(message, state, context_obj, partners, work_repo, artifacts))

    profile = _run(partners.get_business_profile(workspace_id))
    assert profile.profile_status == "usable"
    all_text = "\n".join(text for text, _ in message.answers)
    assert "Профиль сохранён" in all_text
    assert "Что делать сегодня" in all_text
    # DailyActions с пустым workspace всегда даёт минимум fallback-кандидат —
    # ни одного LLM-вызова для этого не требуется (show_daily_actions его
    # никогда не делает — уже проверено в tests/test_daily_actions_service.py).
    assert len(message.answers) >= 2


# ── tenant isolation ─────────────────────────────────────────────────────


def test_onboarding_is_tenant_isolated(tmp_path: Path) -> None:
    partners, work_repo, artifacts = _stack(tmp_path)
    workspace_a = _provision(partners, 1, "tenant-a")
    workspace_b = _provision(
        partners, 2, "tenant-b", business_name="B готов",
        business_type="agency", short_description="B описание",
        specializations=["круизы"],
    )

    profile_a = _run(partners.get_business_profile(workspace_a))
    state_a = _State()
    _run(start_business_onboarding(_Message(), state_a, profile_a))
    assert state_a.state == BusinessOnboarding.waiting_for_field

    profile_b = _run(partners.get_business_profile(workspace_b))
    assert profile_b.profile_status == "usable"
    state_b = _State()
    _run(start_business_onboarding(_Message(), state_b, profile_b))
    assert state_b.state is None  # ничего не спрашивает — B уже полностью заполнен

    # Ответ в рамках workspace A не задевает workspace B.
    _run(on_onboarding_field_answer(
        _Message("Название A"), state_a, _ctx(workspace_a), partners, work_repo, artifacts,
    ))
    untouched_b = _run(partners.get_business_profile(workspace_b))
    assert untouched_b.revision == profile_b.revision
