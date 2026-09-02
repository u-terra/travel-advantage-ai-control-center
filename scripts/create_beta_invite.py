"""Issues a one-time beta registration invite for an existing
(workspace_id, telegram_user_id) pair - the compatibility layer a web
account binds to (see app.domain.web_auth).

Prints the one-time registration URL/token to stdout ONCE. Only its
SHA-256 hash is ever written to the database (app.services.web_auth_tokens)
- if you lose the printed value, the invite is gone; create a new one.

Never hardcodes an email or password: this script only issues an invite,
the actual account (email + password) is created by whoever opens the
printed URL and fills in /register.

Usage:
    python -m scripts.create_beta_invite --workspace-id 1 --telegram-user-id 586249067
    python -m scripts.create_beta_invite --workspace-id 1 --telegram-user-id 586249067 \
        --email owner@example.com --ttl-hours 48 --base-url https://app.example.com
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

# Тот же .env, что читает web_api.py - иначе invite создался бы не в той БД.
load_dotenv()

from app.config import load_settings  # noqa: E402
from app.repositories.partner_repository import PartnerRepository  # noqa: E402
from app.repositories.web_auth_repository import WebAuthRepository  # noqa: E402
from app.services.web_auth_tokens import generate_token, hash_token  # noqa: E402

DEFAULT_TTL_HOURS = 168  # 7 дней


async def _create_invite(
    workspace_id: int, telegram_user_id: int, *,
    email: str | None, ttl_hours: float, base_url: str,
) -> int:
    settings = load_settings()

    partner_repository = PartnerRepository(settings.journal_db_path)
    await partner_repository.init()
    workspace = await partner_repository.get_workspace(workspace_id)
    if workspace is None:
        print(f"ОШИБКА: workspace_id={workspace_id} не найден.")
        return 1

    # An invite must bind only to a pair PartnerRepository's own access
    # model already recognizes - the same check get_current_principal()
    # re-runs on every request (see app/web_api.py). Refuse to create an
    # invite for a made-up or no-longer-active pair, rather than let a
    # typo/stale telegram_user_id issue a link that can never actually be
    # used to reach the cabinet.
    try:
        workspace_context = await partner_repository.resolve_workspace_context(
            telegram_user_id,
        )
    except Exception as exc:
        print(f"ОШИБКА: не удалось проверить доступ ({exc}).")
        return 1

    if workspace_context is None or workspace_context.workspace_id != workspace_id:
        print(
            f"ОШИБКА: telegram_user_id={telegram_user_id} не имеет активной "
            f"membership в workspace_id={workspace_id} (или workspace не активен)."
        )
        return 1

    web_auth_repository = WebAuthRepository(settings.journal_db_path)
    await web_auth_repository.init()

    raw_token = generate_token()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()

    invite = await web_auth_repository.create_invite(
        workspace_id, telegram_user_id, hash_token(raw_token), expires_at,
        email_restriction=email,
    )

    registration_url = f"{base_url.rstrip('/')}/register?invite={raw_token}"

    print("Приглашение создано. Эта ссылка и токен показываются ОДИН РАЗ -")
    print("в базе данных сохранён только их хэш, восстановить их отсюда нельзя.")
    print("")
    print(f"  workspace:        {workspace.name!r} (id={workspace_id})")
    print(f"  telegram_user_id: {telegram_user_id}")
    if email:
        print(f"  email:            только {email}")
    print(f"  действует до:     {invite.expires_at}")
    print("")
    print(f"  Ссылка для регистрации:\n  {registration_url}")
    print("")
    print(f"  (если ссылку нельзя открыть напрямую, токен: {raw_token})")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Создать одноразовое приглашение на бета-регистрацию web-аккаунта",
    )
    parser.add_argument("--workspace-id", type=int, required=True)
    parser.add_argument("--telegram-user-id", type=int, required=True)
    parser.add_argument(
        "--email", default=None,
        help="Ограничить регистрацию конкретным email (необязательно)",
    )
    parser.add_argument(
        "--ttl-hours", type=float, default=DEFAULT_TTL_HOURS,
        help=f"Срок действия приглашения в часах (по умолчанию {DEFAULT_TTL_HOURS})",
    )
    parser.add_argument(
        "--base-url", default="http://localhost:8000",
        help="Базовый URL веб-кабинета для итоговой ссылки",
    )
    args = parser.parse_args()

    return asyncio.run(_create_invite(
        args.workspace_id, args.telegram_user_id,
        email=args.email, ttl_hours=args.ttl_hours, base_url=args.base_url,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
