"""Единая, чистая (без БД/IO) логика вычисления фактического access_state
workspace - используется ОБОИМИ каналами продукта через один и тот же
вызов: SubscriptionRepository.resolve_access_state() (см.
app/repositories/subscription_repository.py), которая читает
workspace_subscriptions (единственный источник subscription state - см.
app/domain/subscription.py) и вызывает compute_access_state() ниже.
Telegram (app/access_state_gate.py) и Web (app/web_api.py) оба идут через
resolve_access_state() - ни один из них не вычисляет access_state
самостоятельно, поэтому оба канала всегда видят одно и то же значение.

partner_workspaces.access_status/access_expires_at (app/domain/partners.py)
здесь больше не читаются - тот механизм deprecated, читается один раз
только для миграционного бэкфилла в SubscriptionRepository.init().
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.subscription import SubscriptionStatus

NO_WORKSPACE = "no_workspace"
TRIAL_ACTIVE = "trial_active"
ACTIVE = "active"
PAST_DUE = "past_due"
EXPIRED = "expired"
SUSPENDED = "suspended"
# Self-service signup, never paid yet - deliberately NOT in
# GRANTED_ACCESS_STATES (see compute_access_state's PENDING branch): same
# "no product access" bucket as EXPIRED/PAST_DUE/SUSPENDED for every gate
# in the app (Web's _subscription_inactive, Telegram's
# AccessStateMiddleware), just a distinct label so the UI can say "choose
# a plan" instead of "your subscription expired".
PENDING = "pending"

# Значения access_state, при которых пользователь получает рабочий
# Оркестратор - одинаково для Web и Telegram.
GRANTED_ACCESS_STATES = frozenset({TRIAL_ACTIVE, ACTIVE})


def is_access_granted(access_state: str) -> bool:
    return access_state in GRANTED_ACCESS_STATES


def compute_access_state(
    status: SubscriptionStatus,
    trial_until: str | None,
    paid_until: str | None,
    *,
    now: datetime | None = None,
) -> str:
    """workspace_subscriptions row -> фактический access_state.

    suspended и past_due всегда блокируют, независимо от дат: suspended —
    административная причина (не платёжная), past_due — платёж не прошёл,
    ни то ни другое не значит "подписка активна". trial проверяется на
    истечение trial_until; beta/active — оба гранты рабочего доступа,
    ограниченные только paid_until.

    Дата ОТСУТСТВУЕТ (None) — легитимная, явно предусмотренная семантика
    "бессрочно" для того статуса, где это уместно (grandfathered/beta и
    active без paid_until - тот же принцип, что был у
    access_expires_at=None раньше; ни один существующий workspace от
    этого доступ не теряет).

    Дата ЕСТЬ, но не парсится — это НЕ "бессрочно", это порча
    billing/access-критичных данных: единственное поле, которое решает,
    когда закрывать доступ, нечитаемо. Fail-closed: access_state = expired,
    а не молчаливое продолжение доступа. Отличие от отсутствующей даты
    принципиально — "нет даты" описывает состояние подписки, "есть
    нечитаемая дата" описывает баг/повреждение данных, и baseline для
    billing/access должен быть "закрыто", а не "открыто".
    """
    if status == SubscriptionStatus.SUSPENDED:
        return SUSPENDED
    if status == SubscriptionStatus.PAST_DUE:
        return PAST_DUE
    if status == SubscriptionStatus.EXPIRED:
        return EXPIRED
    if status == SubscriptionStatus.PENDING:
        return PENDING
    if status == SubscriptionStatus.TRIAL:
        return EXPIRED if _is_expired(trial_until, now) else TRIAL_ACTIVE
    # BETA и ACTIVE - оба гранты рабочего доступа, отличаются только planом.
    return EXPIRED if _is_expired(paid_until, now) else ACTIVE


def _is_expired(expires_at: str | None, now: datetime | None) -> bool:
    if not expires_at:
        # Явно отсутствующая дата - легитимное "бессрочно" для тех
        # статусов, где это допустимо (см. compute_access_state).
        return False
    expires = _parse_datetime(expires_at)
    if expires is None:
        # Дата ЕСТЬ, но не парсится - billing/access-критичная порча
        # данных, не "бессрочно". Fail-closed.
        return True
    return _now(now) >= expires


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _parse_datetime(raw: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value
