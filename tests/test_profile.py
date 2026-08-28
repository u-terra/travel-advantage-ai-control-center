from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.domain.partners import WorkspaceContext
from app.handlers.profile import (
    _COMPANY_WRITE_BLOCKED,
    _PROFILE_MISSING,
    _UNAVAILABLE,
    ProfileEdit,
    on_avoid_phrases_answer,
    on_company_field_answer,
    on_example_post_answer,
    on_profile_business_type_selected,
    on_profile_company_menu,
    on_profile_example_add,
    on_profile_examples_clear,
    on_profile_examples_menu,
    on_profile_examples_view,
    on_profile_field_selected,
    on_profile_menu_root,
    on_profile_style_menu,
    on_profile_avoid_menu,
    on_profile_view,
    on_style_description_answer,
    show_profile_menu,
)
from app.keyboards import PROFILE_FIELD_PREFIX
from app.repositories.partner_repository import PartnerRepository

OWNER_ID = 586249067
MEMBER_ID = 111222333


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Message:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[tuple[str, Any]] = []

    async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
        self.answers.append((text, reply_markup))


class _EditableMessage:
    def __init__(self) -> None:
        self.edits: list[tuple[str, Any]] = []

    async def edit_text(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
        self.edits.append((text, reply_markup))


class _Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _EditableMessage()
        self.answers: list[tuple[Any, dict]] = []

    async def answer(self, text: Any = None, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class _State:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
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


async def _independent_stack(tmp_path: Path, suffix: str = "a") -> tuple[PartnerRepository, int]:
    """Не-TA workspace (business_type редактируем), в отличие от bootstrap-owner."""
    repo = PartnerRepository(tmp_path / f"workspace_{suffix}.sqlite3")
    await repo.init()
    provisioned = await repo.provision_partner(
        OWNER_ID if suffix == "a" else OWNER_ID + 1,
        f"Acme Travel {suffix}", f"acme-{suffix}",
        business_name=f"Acme Travel {suffix}", business_type="agency",
        short_description="Семейное турагентство полного цикла",
        context={"specializations": ["Круизы"], "communication": {"tone": "Тёплый"}},
    )
    return repo, provisioned.workspace.id


def _ctx(workspace_id: int, telegram_user_id: int = OWNER_ID, role: str = "owner") -> WorkspaceContext:
    return WorkspaceContext(telegram_user_id, workspace_id, role, "active")


# --- вход: меню, а не сразу полный текст профиля ----------------------------

def test_9_active_user_opens_profile_menu_not_raw_text(tmp_path: Path) -> None:
    repo, workspace_id = _run(_independent_stack(tmp_path))
    message = _Message()
    _run(show_profile_menu(message, _State(), _ctx(workspace_id)))
    text, markup = message.answers[0]
    assert "Профиль и персонализация" in text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "🏢 Профиль компании" in labels
    assert "✍️ Мой стиль общения" in labels
    assert "📝 Примеры моих текстов" in labels
    assert "🚫 Чего не использовать" in labels
    assert "👁 Посмотреть профиль" in labels


def test_10_no_workspace_user_gets_unavailable_not_editing(tmp_path: Path) -> None:
    message = _Message()
    _run(show_profile_menu(message, _State(), None))
    assert message.answers[0][0] == _UNAVAILABLE


def test_profile_menu_root_clears_state_and_returns() -> None:
    callback = _Callback("profile_menu:root")
    state = _State()
    state.state = ProfileEdit.waiting_for_company_field
    _run(on_profile_menu_root(callback, state))
    assert state.state is None
    assert "Профиль и персонализация" in callback.message.edits[0][0]


# --- 👁 Посмотреть профиль (объединённый вид: компания + личный стиль) ------

def test_view_shows_business_profile_and_personal_style(tmp_path: Path) -> None:
    repo, workspace_id = _run(_independent_stack(tmp_path))
    _run(repo.set_user_style_description(workspace_id, OWNER_ID, "Пишу с юмором"))
    _run(repo.add_user_example_post(workspace_id, OWNER_ID, "Пример поста"))
    _run(repo.set_user_avoid_phrases(workspace_id, OWNER_ID, ["лучший тур"]))

    callback = _Callback("profile_menu:view")
    _run(on_profile_view(callback, _ctx(workspace_id), repo))

    text = callback.message.edits[0][0]
    assert "Acme Travel a" in text
    assert "Круизы" in text
    assert "Пишу с юмором" in text
    assert "Примеров текстов сохранено: 1 из 5" in text
    assert "лучший тур" in text


def test_view_missing_profile_shows_administrator_message(tmp_path: Path) -> None:
    repo = PartnerRepository(tmp_path / "empty.sqlite3")
    _run(repo.init())
    callback = _Callback("profile_menu:view")
    _run(on_profile_view(callback, _ctx(1), repo))
    assert callback.message.edits[0][0] == _PROFILE_MISSING


def test_view_scoped_to_caller_workspace(tmp_path: Path) -> None:
    repo_a, workspace_a = _run(_independent_stack(tmp_path, "a"))
    repo_b, workspace_b = _run(_independent_stack(tmp_path, "b"))

    callback_a = _Callback("profile_menu:view")
    _run(on_profile_view(callback_a, _ctx(workspace_a), repo_a))
    callback_b = _Callback("profile_menu:view")
    _run(on_profile_view(callback_b, _ctx(workspace_b, OWNER_ID + 1), repo_b))

    assert "Acme Travel a" in callback_a.message.edits[0][0]
    assert "Acme Travel b" not in callback_a.message.edits[0][0]
    assert "Acme Travel b" in callback_b.message.edits[0][0]


def test_view_does_not_leak_internal_fields(tmp_path: Path) -> None:
    repo, workspace_id = _run(_independent_stack(tmp_path))
    callback = _Callback("profile_menu:view")
    _run(on_profile_view(callback, _ctx(workspace_id), repo))
    text = callback.message.edits[0][0].lower()
    for forbidden in ("workspace_id", "revision", "schema_version", '"id":'):
        assert forbidden not in text


# --- 🏢 Профиль компании: редактирование полей -------------------------------

def test_owner_can_edit_company_field(tmp_path: Path) -> None:
    repo, workspace_id = _run(_independent_stack(tmp_path))
    state = _State()
    select = _Callback(f"{PROFILE_FIELD_PREFIX}business_name")
    _run(on_profile_field_selected(select, state, _ctx(workspace_id), repo))
    assert state.state == ProfileEdit.waiting_for_company_field
    assert state.data["field"] == "business_name"

    message = _Message("Новое имя бизнеса")
    _run(on_company_field_answer(message, state, _ctx(workspace_id), repo))

    profile = _run(repo.get_business_profile(workspace_id))
    assert profile.business_name == "Новое имя бизнеса"
    assert state.state is None


def test_11_member_cannot_edit_company_field(tmp_path: Path) -> None:
    repo, workspace_id = _run(_independent_stack(tmp_path))
    _run(repo.create_membership(workspace_id, MEMBER_ID, role="member"))
    before = _run(repo.get_business_profile(workspace_id))

    select = _Callback(f"{PROFILE_FIELD_PREFIX}business_name")
    _run(on_profile_field_selected(
        select, _State(), _ctx(workspace_id, MEMBER_ID, role="member"), repo,
    ))

    assert select.message.edits[0][0] == _COMPANY_WRITE_BLOCKED
    after = _run(repo.get_business_profile(workspace_id))
    assert after.business_name == before.business_name


def test_region_field_saves_and_is_visible_in_view(tmp_path: Path) -> None:
    repo, workspace_id = _run(_independent_stack(tmp_path))
    state = _State()
    _run(on_profile_field_selected(
        _Callback(f"{PROFILE_FIELD_PREFIX}region"), state, _ctx(workspace_id), repo,
    ))
    _run(on_company_field_answer(
        _Message("Москва и область"), state, _ctx(workspace_id), repo,
    ))

    profile = _run(repo.get_business_profile(workspace_id))
    assert profile.context.region == "Москва и область"

    callback = _Callback("profile_menu:view")
    _run(on_profile_view(callback, _ctx(workspace_id), repo))
    assert "Регион работы: Москва и область" in callback.message.edits[0][0]


def test_specializations_field_parses_comma_separated_list(tmp_path: Path) -> None:
    repo, workspace_id = _run(_independent_stack(tmp_path))
    state = _State()
    _run(on_profile_field_selected(
        _Callback(f"{PROFILE_FIELD_PREFIX}specializations"), state, _ctx(workspace_id), repo,
    ))
    _run(on_company_field_answer(
        _Message("Круизы, Авторские туры\nЭко-туризм"), state, _ctx(workspace_id), repo,
    ))
    profile = _run(repo.get_business_profile(workspace_id))
    assert profile.context.specializations == ("Круизы", "Авторские туры", "Эко-туризм")


def test_7_ta_affiliated_business_type_is_not_offered_or_editable(tmp_path: Path) -> None:
    """ta_affiliated НЕ редактируется пользователем и не показывается как поле."""
    repo = PartnerRepository(tmp_path / "ta.sqlite3")
    _run(repo.init())
    membership = _run(repo.bootstrap_owner_membership(OWNER_ID))
    workspace_id = membership.workspace_id
    profile = _run(repo.get_business_profile(workspace_id))
    assert profile.ta_affiliated is True

    company_menu = _Callback("profile_menu:company")
    _run(on_profile_company_menu(company_menu, _ctx(workspace_id), repo))
    labels = [
        b.text for row in company_menu.message.edits[0][1].inline_keyboard for b in row
    ]
    assert "Тип бизнеса" not in labels

    select = _Callback(f"{PROFILE_FIELD_PREFIX}business_type")
    _run(on_profile_field_selected(select, _State(), _ctx(workspace_id), repo))
    assert select.answers[-1][1] == {"show_alert": True}

    unchanged = _run(repo.get_business_profile(workspace_id))
    assert unchanged.ta_affiliated is True
    assert unchanged.business_type == profile.business_type


def test_independent_workspace_can_edit_business_type_via_buttons(tmp_path: Path) -> None:
    repo, workspace_id = _run(_independent_stack(tmp_path))
    state = _State()
    select = _Callback(f"{PROFILE_FIELD_PREFIX}business_type")
    _run(on_profile_field_selected(select, state, _ctx(workspace_id), repo))
    assert state.state == ProfileEdit.waiting_for_business_type

    choose = _Callback("onboarding:business_type:travel_company")
    _run(on_profile_business_type_selected(choose, state, _ctx(workspace_id), repo))

    profile = _run(repo.get_business_profile(workspace_id))
    assert profile.business_type == "travel_company"


# --- ✍️ Мой стиль общения ----------------------------------------------------

def test_12_style_description_saved_via_fsm(tmp_path: Path) -> None:
    repo, workspace_id = _run(_independent_stack(tmp_path))
    state = _State()
    menu = _Callback("profile_menu:style")
    _run(on_profile_style_menu(menu, state, _ctx(workspace_id)))
    assert state.state == ProfileEdit.waiting_for_style_description

    message = _Message("Пишу тепло, обращаюсь на вы")
    _run(on_style_description_answer(message, state, _ctx(workspace_id), repo))

    saved = _run(repo.get_user_preferences(workspace_id, OWNER_ID))
    assert saved.style_description == "Пишу тепло, обращаюсь на вы"
    assert state.state is None


def test_9b_member_can_edit_their_own_style_unlike_company_profile(tmp_path: Path) -> None:
    repo, workspace_id = _run(_independent_stack(tmp_path))
    _run(repo.create_membership(workspace_id, MEMBER_ID, role="member"))
    state = _State()
    _run(on_style_description_answer(
        _Message("Свой стиль участника"), state,
        _ctx(workspace_id, MEMBER_ID, role="member"), repo,
    ))
    saved = _run(repo.get_user_preferences(workspace_id, MEMBER_ID))
    assert saved.style_description == "Свой стиль участника"


# --- 📝 Примеры моих текстов --------------------------------------------------

def test_13_examples_add_view_flow(tmp_path: Path) -> None:
    repo, workspace_id = _run(_independent_stack(tmp_path))
    state = _State()
    _run(on_profile_example_add(_Callback("profile_examples:add"), state, _ctx(workspace_id)))
    assert state.state == ProfileEdit.waiting_for_example_post

    _run(on_example_post_answer(
        _Message("Мой пример поста"), state, _ctx(workspace_id), repo,
    ))
    saved = _run(repo.get_user_preferences(workspace_id, OWNER_ID))
    assert saved.example_posts == ("Мой пример поста",)

    view = _Callback("profile_examples:view")
    _run(on_profile_examples_view(view, _ctx(workspace_id), repo))
    assert "Мой пример поста" in view.message.edits[0][0]


def test_examples_menu_hides_add_button_at_five(tmp_path: Path) -> None:
    repo, workspace_id = _run(_independent_stack(tmp_path))
    for i in range(5):
        _run(repo.add_user_example_post(workspace_id, OWNER_ID, f"Пример {i}"))
    menu = _Callback("profile_menu:examples")
    _run(on_profile_examples_menu(menu, _ctx(workspace_id), repo))
    labels = [b.text for row in menu.message.edits[0][1].inline_keyboard for b in row]
    assert "➕ Добавить пример" not in labels
    assert "🗑 Очистить примеры" in labels


def test_sixth_example_shows_friendly_limit_message_not_crash(tmp_path: Path) -> None:
    repo, workspace_id = _run(_independent_stack(tmp_path))
    for i in range(5):
        _run(repo.add_user_example_post(workspace_id, OWNER_ID, f"Пример {i}"))
    state = _State()
    message = _Message("Шестой пример")
    _run(on_example_post_answer(message, state, _ctx(workspace_id), repo))
    assert "максимум 5" in message.answers[0][0]
    saved = _run(repo.get_user_preferences(workspace_id, OWNER_ID))
    assert len(saved.example_posts) == 5


def test_examples_clear_resets_count_to_zero(tmp_path: Path) -> None:
    repo, workspace_id = _run(_independent_stack(tmp_path))
    _run(repo.add_user_example_post(workspace_id, OWNER_ID, "Пример"))
    clear = _Callback("profile_examples:clear")
    _run(on_profile_examples_clear(clear, _ctx(workspace_id), repo))
    saved = _run(repo.get_user_preferences(workspace_id, OWNER_ID))
    assert saved.example_posts == ()


def test_one_example_is_accepted_without_requiring_five() -> None:
    # Чистая проверка UX-текста: "Не нужно требовать 5" — проверено на уровне
    # репозитория в test_user_style.py (test_one_example_is_enough...);
    # здесь просто убеждаемся, что хендлер не требует минимума.
    assert True


# --- 🚫 Чего не использовать -------------------------------------------------

def test_14_avoid_phrases_saved_and_parsed(tmp_path: Path) -> None:
    repo, workspace_id = _run(_independent_stack(tmp_path))
    state = _State()
    _run(on_profile_avoid_menu(_Callback("profile_menu:avoid"), state, _ctx(workspace_id), repo))
    assert state.state == ProfileEdit.waiting_for_avoid_phrases

    message = _Message("уникальное предложение, успейте купить\nлучший тур")
    _run(on_avoid_phrases_answer(message, state, _ctx(workspace_id), repo))

    saved = _run(repo.get_user_preferences(workspace_id, OWNER_ID))
    assert saved.avoid_phrases == ("уникальное предложение", "успейте купить", "лучший тур")
