from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any, Sequence

from app.repositories.partner_repository import PartnerRepository


_PROFILE_KEYS = {
    "business_name", "business_type", "short_description", "context", "schema_version",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pilot partner administration")
    parser.add_argument(
        "--db-path",
        default=os.environ.get("JOURNAL_DB_PATH", "data/journal.sqlite3"),
        help="SQLite database path (defaults to JOURNAL_DB_PATH)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    provision = commands.add_parser("provision-partner")
    provision.add_argument("--telegram-user-id", type=int, required=True)
    provision.add_argument("--workspace-name", required=True)
    provision.add_argument("--workspace-slug", required=True)
    provision.add_argument("--profile-file", type=Path, required=True)

    for name in ("show-partner", "deactivate-partner", "reactivate-partner"):
        command = commands.add_parser(name)
        command.add_argument("--telegram-user-id", type=int, required=True)
    return parser


def _load_profile(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать profile JSON: {exc}") from exc
    if not isinstance(raw, dict) or not set(raw) <= _PROFILE_KEYS:
        raise ValueError("Profile JSON содержит неизвестные поля")
    required = {"business_name", "business_type", "short_description", "context"}
    if not required <= set(raw):
        raise ValueError("Profile JSON не содержит все обязательные поля")
    return raw


async def _show(repository: PartnerRepository, telegram_user_id: int) -> int:
    memberships = await repository.list_memberships_by_telegram_id(telegram_user_id)
    if not memberships:
        raise ValueError("Partner membership не найдена")
    print(f"telegram_user_id: {telegram_user_id}")
    print(f"memberships: {len(memberships)}")
    if len(memberships) != 1:
        print("state: AMBIGUOUS - автоматический выбор workspace запрещён")
    for membership in memberships:
        workspace = await repository.get_workspace(membership.workspace_id)
        profile = await repository.get_business_profile(membership.workspace_id)
        if workspace is None:
            raise ValueError("Membership ссылается на отсутствующий workspace")
        print(f"workspace_id: {workspace.id}")
        print(f"workspace_slug: {workspace.slug}")
        print(f"workspace_name: {workspace.name}")
        print(f"membership_role: {membership.role}")
        print(f"membership_status: {membership.status}")
        if profile is None:
            print("profile_status: MISSING")
        else:
            print(f"profile_status: {profile.profile_status}")
            print(f"profile_revision: {profile.revision}")
            print(f"business_name: {profile.business_name}")
            print(f"business_type: {profile.business_type}")
    return 0 if len(memberships) == 1 else 2


async def _execute(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path)
    if not db_path.is_file():
        raise ValueError(f"Database не найдена: {db_path}")
    repository = PartnerRepository(db_path)
    await repository.init()

    if args.command == "show-partner":
        return await _show(repository, args.telegram_user_id)
    if args.command == "provision-partner":
        profile = _load_profile(args.profile_file)
        result = await repository.provision_partner(
            args.telegram_user_id,
            args.workspace_name,
            args.workspace_slug,
            **profile,
        )
        print("Partner provisioned." if result.created else "Partner already provisioned.")
        print(f"workspace_id: {result.workspace.id}")
        print(f"membership_status: {result.membership.status}")
        print(f"profile_status: {result.profile.profile_status}")
        if result.profile.profile_status == "incomplete":
            print("WARNING: Business Profile incomplete; generation will use limited context.")
        print(
            "NEXT: add Telegram user ID to TELEGRAM_ALLOWED_USER_IDS and restart the service."
        )
        return 0

    status = "inactive" if args.command == "deactivate-partner" else "active"
    membership = await repository.set_partner_membership_status(
        args.telegram_user_id, status
    )
    print(f"membership_status: {membership.status}")
    if status == "inactive":
        print(
            "NEXT: remove Telegram user ID from TELEGRAM_ALLOWED_USER_IDS and restart the service."
        )
    else:
        print(
            "NEXT: add Telegram user ID to TELEGRAM_ALLOWED_USER_IDS and restart the service."
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_execute(args))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
