"""Вычисление рабочего access state workspace (Stage 3A).

Чистая логика без БД/IO — принимает уже прочитанные access_status/
access_expires_at и текущий момент времени, возвращает один из states.
Используется app/access_state_gate.py (middleware) и тестами напрямую.

Это НЕ то же самое, что PartnerWorkspace.status (жизненный цикл самого
workspace) и НЕ то же самое, что workspace_source_subscriptions (какие
источники мониторить) — отдельная, узкая ответственность: подписка/пробный
доступ.
"""

from __future__ import annotations

from datetime import datetime, timezone

NO_WORKSPACE = "no_workspace"
TRIAL_ACTIVE = "trial_active"
ACTIVE = "active"
EXPIRED = "expired"
SUSPENDED = "suspended"

# Значения, которые реально хранятся в partner_workspaces.access_status.
STORED_ACCESS_STATUSES = frozenset({TRIAL_ACTIVE, ACTIVE, EXPIRED, SUSPENDED})

# Значения access_state, при которых пользователь получает рабочий Оркестратор.
GRANTED_ACCESS_STATES = frozenset({TRIAL_ACTIVE, ACTIVE})


def is_access_granted(access_state: str) -> bool:
    return access_state in GRANTED_ACCESS_STATES


def compute_access_state(
    access_status: str,
    access_expires_at: str | None,
    *,
    now: datetime | None = None,
) -> str:
    """access_status/access_expires_at workspace → фактический access_state.

    suspended всегда побеждает независимо от даты. active/trial_active с
    истёкшим access_expires_at превращаются в expired на лету — отдельного
    крон-джоба, который бы физически переписывал access_status, не требуется.
    access_expires_at=None означает бессрочный доступ (так после additive
    миграции остаются все существующие production workspace).
    Неизвестный/повреждённый access_status — fail-safe как expired, а не
    как active: лучше по ошибке показать лобби, чem по ошибке открыть
    рабочий доступ.
    """
    if access_status not in STORED_ACCESS_STATUSES:
        return EXPIRED
    if access_status == SUSPENDED:
        return SUSPENDED
    if access_expires_at:
        expires = _parse_datetime(access_expires_at)
        if expires is not None and _now(now) >= expires:
            return EXPIRED
    return access_status


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _parse_datetime(raw: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        # Повреждённая/непарсящаяся дата — не должна сама по себе заблокировать
        # уже оплаченный доступ; относимся как к отсутствию даты истечения.
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value
