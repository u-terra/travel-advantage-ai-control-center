from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any, Sequence

from app.repositories.partner_repository import PartnerRepository
from app.repositories.source_catalog_repository import SourceCatalogRepository
from app.services.source_registry import SEED_REGISTRY_PATH, runtime_registry_path


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
    parser.add_argument(
        "--source-registry-path",
        default=str(runtime_registry_path()),
        help="Generated Radar source projection path",
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

    list_requests = commands.add_parser("list-source-requests")
    list_requests.add_argument(
        "--status", choices=("pending", "approved", "rejected", "all"),
        default="pending",
    )
    list_requests.add_argument("--workspace-id", type=int)

    show_request = commands.add_parser("show-source-request")
    show_request.add_argument("--request-id", type=int, required=True)

    approve = commands.add_parser("approve-source-request")
    approve.add_argument("--request-id", type=int, required=True)
    approve.add_argument("--workspace-id", type=int, required=True)

    reject = commands.add_parser("reject-source-request")
    reject.add_argument("--request-id", type=int, required=True)
    reject.add_argument("--workspace-id", type=int, required=True)
    reject.add_argument("--reason")
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


def _print_source_request(request, *, detailed: bool = False) -> None:
    print(f"request_id: {request.id}")
    print(f"workspace_id: {request.workspace_id}")
    print(f"workspace_name: {request.workspace_name}")
    print(f"source_id: {request.source_id}")
    print(f"source_url: {request.source_url}")
    print(f"platform: {request.platform}")
    print(f"submitted_by_telegram_user_id: {request.submitted_by_telegram_user_id}")
    print(f"status: {request.status}")
    print(f"created_at: {request.created_at}")
    if detailed:
        print(f"reason: {request.reason or ''}")
        print(f"reviewed_at: {request.reviewed_at or ''}")


async def _source_request_command(args: argparse.Namespace, db_path: Path) -> int:
    repository = SourceCatalogRepository(
        db_path, Path(args.source_registry_path)
    )
    await repository.init(None, legacy_path=SEED_REGISTRY_PATH)
    if args.command == "list-source-requests":
        status = None if args.status == "all" else args.status
        requests = await repository.list_source_requests(
            status=status, workspace_id=args.workspace_id
        )
        print(f"source_requests: {len(requests)}")
        for index, request in enumerate(requests):
            if index:
                print("---")
            _print_source_request(request)
        return 0
    if args.command == "show-source-request":
        request = await repository.get_source_request(args.request_id)
        if request is None:
            raise ValueError("Source request не найден")
        _print_source_request(request, detailed=True)
        return 0
    if args.command == "approve-source-request":
        request = await repository.approve_source_request(
            args.request_id, args.workspace_id
        )
        print("Source request approved.")
        _print_source_request(request, detailed=True)
        print(
            "CHECK: confirm that this source is supported by the current collector infrastructure."
        )
        return 0
    request = await repository.reject_source_request(
        args.request_id, args.workspace_id, args.reason
    )
    print("Source request rejected.")
    _print_source_request(request, detailed=True)
    return 0


async def _execute(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path)
    if not db_path.is_file():
        raise ValueError(f"Database не найдена: {db_path}")
    if args.command in {
        "list-source-requests", "show-source-request",
        "approve-source-request", "reject-source-request",
    }:
        return await _source_request_command(args, db_path)
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
