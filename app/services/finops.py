"""FinOps Monitor - read-only spend/limit status across external
providers (OpenAI API, Yandex, Beget).

Strictly read-only: no top-ups, no plan changes. Every check either
reads real data through an existing credential the process already has,
or returns status="unavailable" with an explanation - it never
fabricates balance/usage/limit numbers.

Beget note: this module only ever reports what a read call returns.
Any future automation that raises a Beget tariff must go up by exactly
one tier at a time - that policy belongs to whatever calls this
service, not to the read-only status check itself.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

ProviderStatus = Literal["ok", "warning", "unavailable"]

_OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
_DEFAULT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class FinOpsProviderReport:
    provider: str
    status: ProviderStatus
    checked_at: str
    message: str
    balance: float | None = None
    usage: float | None = None
    limit: float | None = None

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "status": self.status,
            "balance": self.balance,
            "usage": self.usage,
            "limit": self.limit,
            "checked_at": self.checked_at,
            "message": self.message,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_openai(api_key: str, *, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> FinOpsProviderReport:
    """OpenAI does not expose account balance/usage/limit through a
    standard API-key-scoped read endpoint, so this only verifies the
    configured key is live (GET /v1/models). balance/usage/limit stay
    None - reporting them would mean inventing numbers."""
    checked_at = _now_iso()
    if not api_key:
        return FinOpsProviderReport(
            provider="openai", status="unavailable", checked_at=checked_at,
            message="ORCHESTRATION_OPENAI_API_KEY не настроен - нет ключа для проверки.",
        )
    req = urllib.request.Request(
        _OPENAI_MODELS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            if resp.status == 200:
                return FinOpsProviderReport(
                    provider="openai", status="ok", checked_at=checked_at,
                    message="Ключ OpenAI API действителен (проверено через /v1/models). "
                    "Баланс/лимиты не публикуются через API-ключ - недоступны для чтения.",
                )
            return FinOpsProviderReport(
                provider="openai", status="warning", checked_at=checked_at,
                message=f"Неожиданный ответ OpenAI API: HTTP {resp.status}.",
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            message = "OpenAI API вернул 401 - ключ недействителен или отозван."
        else:
            message = f"OpenAI API вернул ошибку HTTP {exc.code}."
        return FinOpsProviderReport(
            provider="openai", status="unavailable", checked_at=checked_at, message=message,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return FinOpsProviderReport(
            provider="openai", status="unavailable", checked_at=checked_at,
            message=f"Не удалось связаться с OpenAI API: {exc.__class__.__name__}.",
        )


def _check_yandex(search_api_key: str) -> FinOpsProviderReport:
    """Only a Yandex Search API key is configured in this project - there
    is no Yandex Cloud Billing API credential wired up, so balance/usage
    cannot be read. Reports unavailable rather than guessing."""
    checked_at = _now_iso()
    if search_api_key:
        message = (
            "Настроен только YANDEX_SEARCH_API_KEY (для поиска), "
            "read-only доступа к Yandex Cloud Billing нет - баланс/лимиты недоступны."
        )
    else:
        message = "Нет учётных данных Yandex (ни поисковых, ни billing) для проверки."
    return FinOpsProviderReport(
        provider="yandex", status="unavailable", checked_at=checked_at, message=message,
    )


def _check_beget() -> FinOpsProviderReport:
    """No Beget API credentials exist in this project's config yet."""
    checked_at = _now_iso()
    return FinOpsProviderReport(
        provider="beget", status="unavailable", checked_at=checked_at,
        message="Нет учётных данных Beget API (логин/пароль или API-ключ) - проверка недоступна.",
    )


@dataclass(frozen=True)
class FinOpsConfig:
    openai_api_key: str
    yandex_search_api_key: str


class FinOpsService:
    """Aggregates read-only provider status checks. One provider failing
    or being unconfigured never breaks the overall response - each
    provider report is independent and always present."""

    def __init__(self, config: FinOpsConfig) -> None:
        self._config = config

    def check_all(self) -> list[FinOpsProviderReport]:
        reports: list[FinOpsProviderReport] = []
        for provider, fn in (
            ("openai", lambda: _check_openai(self._config.openai_api_key)),
            ("yandex", lambda: _check_yandex(self._config.yandex_search_api_key)),
            ("beget", _check_beget),
        ):
            try:
                reports.append(fn())
            except Exception as exc:  # noqa: BLE001 - a provider check must never take down the others
                reports.append(FinOpsProviderReport(
                    provider=provider, status="unavailable", checked_at=_now_iso(),
                    message=f"Внутренняя ошибка проверки: {exc.__class__.__name__}.",
                ))
        return reports
