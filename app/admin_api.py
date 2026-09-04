"""Beta Control Center - platform-admin-only JSON API (see app/web_api.py
for the require_platform_admin/require_platform_admin_csrf gate this
entire router sits behind, and the report for the full architecture
description).

Deliberately built as a standalone APIRouter constructed by
build_admin_router(), never importing app.web_api itself - app.web_api
imports THIS module and passes in the gate dependencies + repository
instances it already owns, avoiding a circular import between the two
(web_api.py -> admin_api.py -> web_api.py would be a cycle).

Every mutation here:
- requires require_platform_admin_csrf (platform-admin + CSRF);
- requires an explicit confirm=true in the request body (a second,
  server-enforced confirmation on top of whatever the UI's own dialog
  does - a stray/scripted request without it is rejected, not just
  discouraged);
- goes through an existing SubscriptionRepository/PaymentOrderRepository/
  FeedbackRepository method - never a raw SQL UPDATE against those
  tables from here;
- is written to admin_audit_log via AdminAuditLogRepository.record()
  with a safe before/after snapshot (status/plan/dates only - never a
  raw row dump, never a secret).

No endpoint here ever returns a password hash, session/CSRF token,
RoboKassa password/signature, or raw attachment file path. No raw SQL
console, no arbitrary command execution, no impersonation/login-as-user,
no full conversation content (only conversation_id/message_id/timestamps
- see the workspace detail endpoint's recent_events).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.domain.admin_directory import WorkspaceDirectoryRow
from app.domain.feedback import FeedbackStatus
from app.domain.subscription import Subscription, SubscriptionPlan
from app.domain.web_auth import WebPrincipal
from app.repositories.admin_audit_log_repository import AdminAuditLogRepository
from app.repositories.admin_directory_repository import AdminDirectoryRepository
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.competitor_repository import CompetitorRepository
from app.repositories.feedback_repository import FeedbackRepository
from app.repositories.operational_event_repository import OperationalEventRepository
from app.repositories.payment_order_repository import PaymentOrderRepository
from app.repositories.subscription_repository import SubscriptionRepository
from app.repositories.usage_ledger_repository import UsageLedgerRepository
from app.repositories.web_attachment_repository import WebAttachmentRepository
from app.repositories.web_auth_repository import WebAuthRepository
from app.repositories.web_conversation_repository import WebConversationRepository
from app.services.robokassa import RoboKassaConfig, compute_extended_paid_until

_EPOCH = "1970-01-01T00:00:00+00:00"
_MAX_LIST_LIMIT = 100
_MAX_EXTEND_TRIAL_DAYS = 90
_MAX_ACTIVATE_DAYS = 366


@dataclass(frozen=True)
class AdminDeps:
    web_auth_repository: WebAuthRepository
    subscription_repository: SubscriptionRepository
    payment_order_repository: PaymentOrderRepository
    usage_ledger_repository: UsageLedgerRepository
    competitor_repository: CompetitorRepository
    artifact_repository: ArtifactRepository
    web_conversation_repository: WebConversationRepository
    web_attachment_repository: WebAttachmentRepository
    operational_event_repository: OperationalEventRepository
    feedback_repository: FeedbackRepository
    admin_audit_log_repository: AdminAuditLogRepository
    admin_directory_repository: AdminDirectoryRepository
    robokassa_config: RoboKassaConfig
    upload_storage_root: Path
    llm_provider_configured: bool


class ConfirmRequest(BaseModel):
    confirm: bool = False


class ExtendTrialRequest(BaseModel):
    confirm: bool = False
    days: int = 14


class ActivateSubscriptionRequest(BaseModel):
    confirm: bool = False
    days: int | None = None


class FeedbackStatusRequest(BaseModel):
    confirm: bool = False
    status: str


def _workspace_row_payload(row: WorkspaceDirectoryRow) -> dict:
    return {
        "workspace_id": row.workspace_id,
        "name": row.name,
        "slug": row.slug,
        "status": row.status,
        "created_at": row.created_at,
        "business_name": row.business_name,
        "ta_affiliated": row.ta_affiliated,
        "subscription_status": row.subscription_status,
        "plan": row.plan,
        "paid_until": row.paid_until,
        "trial_until": row.trial_until,
        "primary_email": row.primary_email,
        "onboarding_completed": row.onboarding_completed,
    }


def _subscription_payload(sub: Subscription | None) -> dict | None:
    if sub is None:
        return None
    return {
        "workspace_id": sub.workspace_id,
        "status": sub.status.value,
        "plan": sub.plan.value,
        "started_at": sub.started_at,
        "trial_until": sub.trial_until,
        "paid_until": sub.paid_until,
        "payment_provider": sub.payment_provider,
        "updated_at": sub.updated_at,
        # external_payment_id is our own order id / "admin:<email>" marker,
        # never a RoboKassa secret - safe to show.
        "external_payment_id": sub.external_payment_id,
    }


def build_admin_router(
    deps: AdminDeps, *, require_platform_admin, require_platform_admin_csrf,
) -> APIRouter:
    router = APIRouter()

    # ── dashboard ────────────────────────────────────────────────────

    async def _window_metrics(since_iso: str) -> dict:
        usage = await deps.usage_ledger_repository.global_summary_since(since_iso)
        return {
            "registered_web_users": await deps.web_auth_repository.count_users_created_since(since_iso),
            "active_workspaces": await deps.operational_event_repository.distinct_workspace_count_since(since_iso),
            "active_users": await deps.operational_event_repository.distinct_web_user_count_since(since_iso),
            "conversations": await deps.web_conversation_repository.count_conversations_created_since(since_iso),
            "messages": await deps.web_conversation_repository.count_messages_created_since(since_iso),
            "uploads": await deps.web_attachment_repository.count_uploads_since(since_iso),
            "competitor_analyses": await deps.competitor_repository.count_intelligence_analyses_since(since_iso),
            "materials_created": await deps.artifact_repository.count_created_since(since_iso),
            "signals_read": await deps.operational_event_repository.count_since(
                since_iso, module="signals", event_type="read", success=True,
            ),
            "errors": await deps.operational_event_repository.count_since(since_iso, success=False),
            "feedback_up": await deps.feedback_repository.count_since(since_iso, rating="up"),
            "feedback_down": await deps.feedback_repository.count_since(since_iso, rating="down"),
            "llm_calls": usage.total_calls,
            "llm_tokens": usage.total_tokens,
            "llm_cost_usd": usage.estimated_cost_usd,
            "payments_created": await deps.payment_order_repository.count_since(since_iso),
            "payments_paid": await deps.payment_order_repository.count_since(since_iso, status="paid"),
        }

    @router.get("/api/admin/dashboard")
    async def dashboard(principal: WebPrincipal = Depends(require_platform_admin)):
        now = datetime.now(timezone.utc)
        since_24h = (now - timedelta(hours=24)).isoformat()
        since_7d = (now - timedelta(days=7)).isoformat()

        subscriptions = await deps.subscription_repository.list_all()
        by_status: dict[str, int] = {}
        for sub in subscriptions:
            by_status[sub.status.value] = by_status.get(sub.status.value, 0) + 1

        return {
            "today": await _window_metrics(since_24h),
            "last_7_days": await _window_metrics(since_7d),
            "subscriptions_by_status": by_status,
            "total_workspaces_with_subscription": len(subscriptions),
            "robokassa_is_test": deps.robokassa_config.is_test,
            "robokassa_configured": deps.robokassa_config.is_configured,
            "generated_at": now.isoformat(),
        }

    # ── users / workspaces ───────────────────────────────────────────

    @router.get("/api/admin/workspaces")
    async def list_workspaces(
        query: str | None = None, limit: int = 20, offset: int = 0,
        principal: WebPrincipal = Depends(require_platform_admin),
    ):
        limit = max(1, min(limit, _MAX_LIST_LIMIT))
        offset = max(0, offset)
        rows, total = await deps.admin_directory_repository.search_workspaces(
            query=query, limit=limit, offset=offset,
        )
        workspace_ids = [row.workspace_id for row in rows]
        now = datetime.now(timezone.utc)
        calls_7d = await deps.usage_ledger_repository.call_counts_since_for_workspaces(
            workspace_ids, (now - timedelta(days=7)).isoformat(),
        )
        calls_30d = await deps.usage_ledger_repository.call_counts_since_for_workspaces(
            workspace_ids, (now - timedelta(days=30)).isoformat(),
        )
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "workspaces": [
                {
                    **_workspace_row_payload(row),
                    "usage_calls_7d": calls_7d.get(row.workspace_id, 0),
                    "usage_calls_30d": calls_30d.get(row.workspace_id, 0),
                }
                for row in rows
            ],
        }

    @router.get("/api/admin/workspaces/{workspace_id}")
    async def workspace_detail(
        workspace_id: int, principal: WebPrincipal = Depends(require_platform_admin),
    ):
        workspace = await deps.admin_directory_repository.get_workspace(workspace_id)
        if workspace is None:
            return {"error": "Workspace не найден.", "workspace": None}

        members = await deps.admin_directory_repository.get_members(workspace_id)
        # A workspace created after the last startup backfill may not have
        # a workspace_subscriptions row yet (same lazy-provisioning gap
        # SubscriptionRepository.resolve_access_state() already papers over
        # for the live access gate - see its docstring). Mirror that here
        # so the card never shows a confusing "null" for a workspace the
        # access layer already treats as grandfathered-in beta.
        subscription = await deps.subscription_repository.get_for_workspace(workspace_id)
        if subscription is None:
            subscription = await deps.subscription_repository.ensure_beta(workspace_id)
        payment_orders = await deps.payment_order_repository.list_for_workspace(
            workspace_id, limit=20,
        )
        now = datetime.now(timezone.utc)
        usage_7d = await deps.usage_ledger_repository.summary_for_workspace(
            workspace_id, since=(now - timedelta(days=7)).isoformat(),
        )
        usage_30d = await deps.usage_ledger_repository.summary_for_workspace(
            workspace_id, since=(now - timedelta(days=30)).isoformat(),
        )
        # Technical reference only - never message/prompt content. See the
        # module docstring: safe_message is a short pre-summarized string
        # the recording call site writes itself, metadata_json is
        # intentionally NOT included here.
        recent_events = await deps.operational_event_repository.list_recent_events(
            workspace_id=workspace_id, limit=50,
        )

        return {
            "workspace": _workspace_row_payload(workspace),
            "members": [
                {
                    "telegram_user_id": m.telegram_user_id, "role": m.role,
                    "status": m.status, "email": m.email,
                    "onboarding_completed": m.onboarding_completed,
                }
                for m in members
            ],
            "subscription": _subscription_payload(subscription),
            "payment_orders": [
                {
                    "id": o.id, "plan": o.plan, "amount": o.amount, "currency": o.currency,
                    "provider": o.provider, "status": o.status.value,
                    "created_at": o.created_at, "paid_at": o.paid_at,
                }
                for o in payment_orders
            ],
            "usage_7d": {
                "calls": usage_7d.total_calls, "tokens": usage_7d.total_tokens,
                "cost_usd": usage_7d.estimated_cost_usd,
            },
            "usage_30d": {
                "calls": usage_30d.total_calls, "tokens": usage_30d.total_tokens,
                "cost_usd": usage_30d.estimated_cost_usd,
            },
            "recent_events": [
                {
                    "occurred_at": e.occurred_at, "module": e.module, "event_type": e.event_type,
                    "severity": e.severity.value, "success": e.success,
                    "latency_ms": e.latency_ms, "error_code": e.error_code,
                    "safe_message": e.safe_message, "request_id": e.request_id,
                }
                for e in recent_events
            ],
        }

    # ── billing admin actions ────────────────────────────────────────

    async def _audit(
        principal: WebPrincipal, *, action: str, target_workspace_id: int,
        before: dict, after: dict,
    ) -> None:
        await deps.admin_audit_log_repository.record(
            admin_web_user_id=principal.web_user_id, admin_email=principal.email,
            action=action, target_workspace_id=target_workspace_id,
            before=before, after=after,
        )

    @router.post("/api/admin/workspaces/{workspace_id}/extend-trial")
    async def extend_trial(
        workspace_id: int, request: ExtendTrialRequest,
        principal: WebPrincipal = Depends(require_platform_admin_csrf),
    ):
        if not request.confirm:
            return {"error": "Требуется подтверждение (confirm=true)."}
        if request.days <= 0 or request.days > _MAX_EXTEND_TRIAL_DAYS:
            return {"error": f"Количество дней должно быть от 1 до {_MAX_EXTEND_TRIAL_DAYS}."}

        before = await deps.subscription_repository.get_for_workspace(workspace_id)
        now = datetime.now(timezone.utc)
        new_trial_until = compute_extended_paid_until(
            current_paid_until=before.trial_until if before is not None else None,
            subscription_days=request.days, now=now,
        )
        after = await deps.subscription_repository.start_trial(workspace_id, new_trial_until)
        await _audit(
            principal, action="extend_trial", target_workspace_id=workspace_id,
            before={"status": before.status.value if before else None, "trial_until": before.trial_until if before else None},
            after={"status": after.status.value if after else None, "trial_until": after.trial_until if after else None},
        )
        return {"subscription": _subscription_payload(after)}

    @router.post("/api/admin/workspaces/{workspace_id}/activate")
    async def activate_subscription(
        workspace_id: int, request: ActivateSubscriptionRequest,
        principal: WebPrincipal = Depends(require_platform_admin_csrf),
    ):
        """Manual activation/extension - the admin equivalent of a
        successful RoboKassa callback, going through the exact same
        SubscriptionRepository.mark_paid() + renewal rule
        (compute_extended_paid_until) as a real payment - see
        app.services.billing_service.BillingService._extend_subscription.
        payment_provider='admin' (not 'robokassa') makes manual grants
        distinguishable from real payments in payment history."""
        if not request.confirm:
            return {"error": "Требуется подтверждение (confirm=true)."}
        days = request.days if request.days is not None else deps.robokassa_config.subscription_days
        if not days or days <= 0 or days > _MAX_ACTIVATE_DAYS:
            return {"error": f"Количество дней должно быть от 1 до {_MAX_ACTIVATE_DAYS}."}

        before = await deps.subscription_repository.get_for_workspace(workspace_id)
        now = datetime.now(timezone.utc)
        new_paid_until = compute_extended_paid_until(
            current_paid_until=before.paid_until if before is not None else None,
            subscription_days=days, now=now,
        )
        after = await deps.subscription_repository.mark_paid(
            workspace_id, external_payment_id=f"admin:{principal.email}",
            payment_provider="admin", paid_until=new_paid_until,
            plan=SubscriptionPlan.STANDARD,
        )
        await _audit(
            principal, action="activate_subscription", target_workspace_id=workspace_id,
            before={"status": before.status.value if before else None, "paid_until": before.paid_until if before else None},
            after={"status": after.status.value if after else None, "paid_until": after.paid_until if after else None, "plan": after.plan.value if after else None},
        )
        return {"subscription": _subscription_payload(after)}

    @router.post("/api/admin/workspaces/{workspace_id}/suspend")
    async def suspend_workspace(
        workspace_id: int, request: ConfirmRequest,
        principal: WebPrincipal = Depends(require_platform_admin_csrf),
    ):
        if not request.confirm:
            return {"error": "Требуется подтверждение (confirm=true)."}
        before = await deps.subscription_repository.get_for_workspace(workspace_id)
        after = await deps.subscription_repository.mark_suspended(workspace_id)
        await _audit(
            principal, action="suspend", target_workspace_id=workspace_id,
            before={"status": before.status.value if before else None},
            after={"status": after.status.value if after else None},
        )
        return {"subscription": _subscription_payload(after)}

    @router.post("/api/admin/workspaces/{workspace_id}/restore")
    async def restore_workspace(
        workspace_id: int, request: ConfirmRequest,
        principal: WebPrincipal = Depends(require_platform_admin_csrf),
    ):
        """Counterpart to suspend - restores to 'active' without touching
        plan/paid_until (see SubscriptionRepository.mark_active)."""
        if not request.confirm:
            return {"error": "Требуется подтверждение (confirm=true)."}
        before = await deps.subscription_repository.get_for_workspace(workspace_id)
        after = await deps.subscription_repository.mark_active(workspace_id)
        await _audit(
            principal, action="restore", target_workspace_id=workspace_id,
            before={"status": before.status.value if before else None},
            after={"status": after.status.value if after else None},
        )
        return {"subscription": _subscription_payload(after)}

    # ── errors ───────────────────────────────────────────────────────

    @router.get("/api/admin/errors")
    async def errors_view(
        period: str = "7d", module: str | None = None, severity: str | None = None,
        workspace_id: int | None = None,
        principal: WebPrincipal = Depends(require_platform_admin),
    ):
        days = 1 if period == "24h" else 7
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        groups = await deps.operational_event_repository.error_summary_since(
            since, module=module, severity=severity, workspace_id=workspace_id, limit=200,
        )
        return {
            "period": period,
            "groups": [
                {
                    "module": g.module, "event_type": g.event_type, "error_code": g.error_code,
                    "severity": g.severity.value, "occurrences": g.occurrences,
                    "workspace_count": g.workspace_count, "last_occurred_at": g.last_occurred_at,
                    "sample_safe_message": g.sample_safe_message,
                    "sample_request_id": g.sample_request_id,
                }
                for g in groups
            ],
        }

    # ── activity / funnel ────────────────────────────────────────────

    @router.get("/api/admin/activity")
    async def activity_funnel(principal: WebPrincipal = Depends(require_platform_admin)):
        registered = await deps.web_auth_repository.count_users_created_since(_EPOCH)
        onboarding_completed = await deps.operational_event_repository.distinct_web_user_count_since(
            _EPOCH, module="onboarding", event_type="complete",
        )
        first_chat = await deps.operational_event_repository.distinct_web_user_count_since(
            _EPOCH, module="chat", event_type="message",
        )
        first_competitor = await deps.operational_event_repository.distinct_web_user_count_since(
            _EPOCH, module="competitors", event_type="add",
        )
        first_attachment = await deps.operational_event_repository.distinct_web_user_count_since(
            _EPOCH, module="attachments", event_type="upload",
        )
        first_material_edit = await deps.operational_event_repository.distinct_web_user_count_since(
            _EPOCH, module="materials", event_type="update",
        )
        return_visit = await deps.operational_event_repository.distinct_web_user_count_with_event_on_a_later_day()

        return {
            "funnel": [
                {"step": "registered", "count": registered},
                {"step": "onboarding_completed", "count": onboarding_completed},
                {"step": "first_chat", "count": first_chat},
                {"step": "first_competitor", "count": first_competitor},
                {"step": "first_attachment", "count": first_attachment},
                {"step": "first_material_edit", "count": first_material_edit},
                {"step": "return_visit", "count": return_visit},
            ],
            "note": (
                "Шаги onboarding/chat/competitor/attachment/material считаются "
                "только с момента подключения телеметрии (см. отчёт) - более "
                "ранняя активность в них не попадёт. return_visit - события "
                "как минимум за 2 разных календарных дня, не отдельная сессия."
            ),
        }

    # ── feedback ─────────────────────────────────────────────────────

    @router.get("/api/admin/feedback")
    async def feedback_view(
        status: str | None = None, principal: WebPrincipal = Depends(require_platform_admin),
    ):
        items = await deps.feedback_repository.list_recent(status=status, limit=200)
        return {
            "feedback": [
                {
                    "id": f.id, "workspace_id": f.workspace_id, "web_user_id": f.web_user_id,
                    "conversation_id": f.conversation_id, "message_id": f.message_id,
                    "rating": f.rating.value, "reason": f.reason, "comment": f.comment,
                    "status": f.status.value, "created_at": f.created_at,
                }
                for f in items
            ],
        }

    @router.post("/api/admin/feedback/{feedback_id}/status")
    async def update_feedback_status(
        feedback_id: int, request: FeedbackStatusRequest,
        principal: WebPrincipal = Depends(require_platform_admin_csrf),
    ):
        if not request.confirm:
            return {"error": "Требуется подтверждение (confirm=true)."}
        try:
            new_status = FeedbackStatus(request.status)
        except ValueError:
            return {"error": "Недопустимый статус."}

        before = await deps.feedback_repository.get(feedback_id)
        if before is None:
            return {"error": "Отзыв не найден."}
        after = await deps.feedback_repository.set_status(feedback_id, new_status)
        await _audit(
            principal, action="feedback_status_change", target_workspace_id=before.workspace_id,
            before={"status": before.status.value},
            after={"status": after.status.value if after else None},
        )
        return {
            "feedback": {
                "id": after.id, "status": after.status.value,
            } if after is not None else None,
        }

    # ── health ───────────────────────────────────────────────────────

    @router.get("/api/admin/health")
    async def health_view(principal: WebPrincipal = Depends(require_platform_admin)):
        db_read_ok = True
        try:
            await deps.subscription_repository.list_all()
        except Exception:
            db_read_ok = False

        db_write_ok = True
        try:
            await deps.operational_event_repository.record(
                module="health", event_type="check", success=True,
            )
        except Exception:
            db_write_ok = False

        upload_root = deps.upload_storage_root
        try:
            upload_root.mkdir(parents=True, exist_ok=True)
            upload_writable = os.access(upload_root, os.W_OK)
            upload_free_bytes = shutil.disk_usage(upload_root).free
        except Exception:
            upload_writable, upload_free_bytes = False, None

        last_ai = await deps.operational_event_repository.last_success(
            module="chat", event_type="message",
        )
        last_competitor = await deps.operational_event_repository.last_success(
            module="competitors", event_type="analyze",
        )
        last_upload = await deps.operational_event_repository.last_success(
            module="attachments", event_type="upload",
        )
        last_robokassa = await deps.operational_event_repository.last_success(
            module="billing", event_type="robokassa_callback",
        )

        return {
            "web_app": "ok",
            "db_read": "ok" if db_read_ok else "error",
            "db_write": "ok" if db_write_ok else "error",
            "subscription_repository": "ok" if db_read_ok else "error",
            "upload_storage_writable": upload_writable,
            "upload_storage_free_bytes": upload_free_bytes,
            "llm_provider_configured": deps.llm_provider_configured,
            "billing_configured": deps.robokassa_config.is_configured,
            "billing_mode": "test" if deps.robokassa_config.is_test else "production",
            "last_successful_ai_request": last_ai.occurred_at if last_ai else None,
            "last_successful_competitor_analysis": last_competitor.occurred_at if last_competitor else None,
            "last_successful_upload": last_upload.occurred_at if last_upload else None,
            "last_robokassa_callback": last_robokassa.occurred_at if last_robokassa else None,
            "telegram_note": (
                "Нет безопасного общего heartbeat для Telegram - статус "
                "намеренно не показывается, чтобы не выдумывать 'online'."
            ),
        }

    # ── audit log ────────────────────────────────────────────────────

    @router.get("/api/admin/audit-log")
    async def audit_log_view(
        workspace_id: int | None = None,
        principal: WebPrincipal = Depends(require_platform_admin),
    ):
        entries = await deps.admin_audit_log_repository.list_recent(
            target_workspace_id=workspace_id, limit=200,
        )
        return {
            "entries": [
                {
                    "id": e.id, "occurred_at": e.occurred_at, "admin_email": e.admin_email,
                    "action": e.action, "target_workspace_id": e.target_workspace_id,
                    "before": e.before_json, "after": e.after_json,
                    "request_id": e.request_id,
                }
                for e in entries
            ],
        }

    return router
