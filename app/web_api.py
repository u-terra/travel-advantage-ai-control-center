from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import markdown
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.admin_api import AdminDeps, build_admin_router
from app.chat_provider import AttachmentInput, ChatConfig, OpenAIChatProvider
from app.config import load_settings
from app.domain.business_profiles import (
    BusinessProfileValidationError,
    StaleBusinessProfileError,
)
from app.domain.competitor_discovery import canonical_domain
from app.domain.competitor_intelligence import DATA_ORIGIN_RADAR_SIGNAL
from app.domain.feedback import FEEDBACK_REASON_CODES, FeedbackRating, FeedbackStatus
from app.domain.telemetry import EventSeverity
from app.domain.usage import UsageStatus
from app.domain.web_attachment import WebAttachment
from app.domain.web_auth import WebPrincipal
from app.domain.web_conversation import ROLE_ASSISTANT, ROLE_USER
from app.repositories.admin_audit_log_repository import AdminAuditLogRepository
from app.repositories.admin_directory_repository import AdminDirectoryRepository
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.competitor_repository import (
    CompetitorAddressError,
    CompetitorLabelError,
    CompetitorRepository,
)
from app.repositories.feedback_repository import FeedbackRepository
from app.repositories.knowledge_repository import KnowledgeRepository
from app.repositories.operational_event_repository import OperationalEventRepository
from app.repositories.partner_repository import (
    PartnerRepository,
    TooManyUserExamplesError,
    VoiceSampleTooLongError,
    business_context_to_dict,
)
from app.repositories.payment_order_repository import PaymentOrderRepository
from app.repositories.subscription_repository import SubscriptionRepository
from app.repositories.usage_ledger_repository import UsageLedgerRepository
from app.repositories.web_attachment_repository import WebAttachmentRepository
from app.repositories.web_auth_repository import (
    EmailAlreadyRegisteredError,
    WebAuthRepository,
)
from app.repositories.web_conversation_repository import (
    WebConversationRepository,
    derive_conversation_title,
)
from app.repositories.workspace_memory_repository import WorkspaceMemoryRepository
from app.repositories.workspace_signal_repository import WorkspaceSignalRepository
from app.services.attachment_storage import AttachmentStorage
from app.services.attachment_validation import (
    MAX_FILES_PER_UPLOAD,
    MAX_FILE_SIZE_BYTES,
    MAX_TEXT_FILE_CHARS,
    MAX_TOTAL_UPLOAD_BYTES,
    AttachmentSniff,
    AttachmentValidationError,
    sanitize_display_filename,
    validate_attachment,
)
from app.services.billing_service import BillingNotConfigured, BillingService
from app.services.business_profile_context import (
    BusinessProfileAccessError,
    BusinessProfileService,
    build_assistant_context,
)
from app.services.competitor_intelligence import (
    CompetitorIntelligenceService,
    CompetitorIntelligenceUnavailable,
)
from app.services.content_factory import ContentFactoryConfig
from app.services.draft_sanitizer import sanitize_draft_text
from app.services.generation_request_builder import build_provider_generation_request
from app.services.knowledge_service import KnowledgeBundle, KnowledgeService
from app.services.lead_radar import (
    DISPLAY_LIMIT,
    LeadRadarConfig,
    build_workspace_signals,
    category_label,
)
from app.services.access_state import is_access_granted
from app.services.llm.factory import create_llm_provider
from app.services.material_orchestration import MaterialOrchestrationService
from app.services.robokassa import RoboKassaConfig
from app.services.telemetry import record_event
from app.services.usage_recorder import record_llm_call
from app.services.web_auth_passwords import (
    WeakPasswordError,
    hash_password,
    validate_password_policy,
    verify_password,
)
from app.services.web_auth_tokens import generate_token, hash_token, tokens_match

log = logging.getLogger(__name__)


app = FastAPI(title="Travel AI Orchestrator Web API")

settings = load_settings()

chat_provider = OpenAIChatProvider(
    ChatConfig(
        api_key=settings.planner_openai_api_key,
        model="gpt-5.6-terra",
        timeout_seconds=180,
    )
)

knowledge_repository = KnowledgeRepository()
usage_ledger_repository = UsageLedgerRepository(settings.journal_db_path)
competitor_repository = CompetitorRepository(settings.journal_db_path)
partner_repository = PartnerRepository(settings.journal_db_path)
workspace_memory_repository = WorkspaceMemoryRepository(settings.journal_db_path)
artifact_repository = ArtifactRepository(settings.journal_db_path)
web_conversation_repository = WebConversationRepository(settings.journal_db_path)
web_attachment_repository = WebAttachmentRepository(settings.journal_db_path)
# Sibling of journal.sqlite3 under the same data/ root - never a
# statically-served directory (this app has no StaticFiles mount at all).
attachment_storage = AttachmentStorage(settings.journal_db_path.parent / "web_uploads")
web_auth_repository = WebAuthRepository(settings.journal_db_path)
# Unified Subscription: the SAME repository/state Telegram's
# AccessStateMiddleware reads (app/access_state_gate.py) - see
# get_active_principal()/require_csrf_and_subscription() below. Web has no
# separate subscription concept; a workspace's access is granted or denied
# identically on both channels because both call
# subscription_repository.resolve_access_state() with no channel-specific
# logic in between.
subscription_repository = SubscriptionRepository(settings.journal_db_path)

# RoboKassa billing - see app.services.robokassa / app.services.billing_service.
# robokassa_config.is_configured is False (billing endpoints answer "not
# configured", nothing crashes) whenever ROBOKASSA_* env vars are missing -
# expected in dev/CI, where no real RoboKassa secret should ever exist.
payment_order_repository = PaymentOrderRepository(settings.journal_db_path)
robokassa_config = RoboKassaConfig(
    merchant_login=settings.robokassa_merchant_login,
    password1=settings.robokassa_password1,
    password2=settings.robokassa_password2,
    is_test=settings.robokassa_is_test,
    standard_price_rub=settings.robokassa_standard_price_rub,
    subscription_days=settings.orchestravel_subscription_days,
    public_base_url=settings.orchestravel_public_base_url,
)
billing_service = BillingService(
    config=robokassa_config,
    payment_order_repository=payment_order_repository,
    subscription_repository=subscription_repository,
)

# Beta Control Center (see app/admin_api.py) - telemetry/feedback/audit-log
# repositories and the one cross-tenant read path (AdminDirectoryRepository).
# All gated behind require_platform_admin below; operational_event_repository
# and feedback_repository are ALSO used from regular (non-admin) request
# handlers further down (telemetry recording, feedback submission).
operational_event_repository = OperationalEventRepository(settings.journal_db_path)
feedback_repository = FeedbackRepository(settings.journal_db_path)
admin_audit_log_repository = AdminAuditLogRepository(settings.journal_db_path)
admin_directory_repository = AdminDirectoryRepository(settings.journal_db_path)

# Orphan pending attachments (uploaded, never sent) older than this are
# reaped on startup - see _reap_orphan_attachments().
ATTACHMENT_ORPHAN_TTL = timedelta(hours=24)
workspace_signal_repository = WorkspaceSignalRepository(
    settings.journal_db_path, settings.lead_radar_db_path
)
lead_radar_config = LeadRadarConfig(db_path=settings.lead_radar_db_path)

# Верхняя граница объёма workspace memory, передаваемого в промпт.
MAX_WORKSPACE_MEMORY_CHARS = 6000

knowledge_service = KnowledgeService(
    knowledge_repository,
    max_primary_items=5,
    max_related_items=8,
    max_facts=30,
    max_examples=2,
)

# knowledge_repository is currently one shared Travel Advantage/MWR Life
# reference base with no workspace_id column (see GET /api/knowledge) - a
# workspace that isn't ta_affiliated must get none of it, not a filtered
# view. _EMPTY_KNOWLEDGE_BUNDLE is the fail-closed substitute for a real
# retrieve() call: every bundle.<field> access downstream stays valid
# without special-casing "no bundle". This is a deliberate all-or-nothing
# switch, not a schema change - the seam where a separate general/neutral
# knowledge base could be added later without touching tenant logic here.
_EMPTY_KNOWLEDGE_BUNDLE = KnowledgeBundle(
    question="", primary_items=(), related_items=(), facts=(),
    compliance_facts=(), examples=(), sources=(),
    potentially_ambiguous=False, ambiguity_reasons=(), missing_definitions=(),
)


async def _is_ta_affiliated(workspace_id: int) -> bool:
    """Authoritative source for any Travel Advantage/MWR Life-gated
    content: BusinessProfile.ta_affiliated only - never business_type,
    workspace_id, or role. Fail-closed: no profile means not affiliated."""
    profile = await partner_repository.get_business_profile(workspace_id)
    return profile is not None and profile.ta_affiliated


content_factory_config = ContentFactoryConfig(
    url=settings.content_factory_url,
    token=settings.content_factory_token,
    timeout_seconds=settings.content_factory_timeout_seconds,
    source_analysis_url=settings.content_factory_source_analysis_url,
    topics_url=settings.content_factory_topics_url,
)

competitor_llm_provider = create_llm_provider(
    settings.llm_provider,
    content_factory_config=content_factory_config,
)

competitor_intelligence_service = CompetitorIntelligenceService(
    competitor_llm_provider,
    knowledge_service,
    usage_ledger_repository=usage_ledger_repository,
    workspace_signal_repository=workspace_signal_repository,
)

# Signal/competitor -> материал (Radar/Competitor Intelligence "Создать
# материал"): same MaterialOrchestrationService + same competitor_llm_provider
# Telegram already uses (app/handlers/menu.py, app/handlers/competitors.py) -
# no second generator, no second LLM provider.
material_orchestration_service = MaterialOrchestrationService()

# Actions offered per signal/competitor opportunity - deliberately just two,
# both already understood by Content Factory via generation_request_builder's
# _PROVIDER_MATERIAL_TYPES ("post" -> market_offer, "client_message" ->
# client_question). Not a free-text artifact_type: only these two are
# reachable from this UI surface.
_SIGNAL_OR_COMPETITOR_MATERIAL_ACTIONS = frozenset({"post", "client_message"})


class MaterialActionRequest(BaseModel):
    action: str


# ── web-auth: cookie session + CSRF ─────────────────────────────────────
#
# SESSION_COOKIE_NAME (HttpOnly) is the only thing that proves who a
# browser is; CSRF_COOKIE_NAME is deliberately NOT HttpOnly - the frontend
# JS reads it and echoes it back as the X-CSRF-Token header on every
# mutating request (see chat.html's fetch wrapper). A forged cross-site
# request can trigger the HttpOnly cookie automatically, but has no way to
# read CSRF_COOKIE_NAME (different origin) or set a custom header, so it
# can never produce a matching X-CSRF-Token. The server never trusts the
# CSRF cookie by itself - require_csrf() below hashes the header value and
# compares it against the hash stored server-side for THIS session, so a
# stale/foreign CSRF cookie can't be replayed against a different session.

SESSION_COOKIE_NAME = "ta_session"
CSRF_COOKIE_NAME = "ta_csrf"
SESSION_TTL_DAYS = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_auth_cookies(response: Response, raw_session_token: str, raw_csrf_token: str) -> None:
    max_age = SESSION_TTL_DAYS * 24 * 3600
    response.set_cookie(
        SESSION_COOKIE_NAME, raw_session_token, max_age=max_age,
        httponly=True, secure=True, samesite="lax", path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME, raw_csrf_token, max_age=max_age,
        httponly=False, secure=True, samesite="lax", path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


async def _start_session(response: Response, web_user_id: int, binding_id: int) -> None:
    """Always mints a brand-new session row + brand-new random tokens -
    called on every successful login/register, never reusing a
    pre-existing session id. That's the session-fixation defense: there is
    nothing for a pre-auth session to "fix", because post-auth always gets
    a fresh one."""
    raw_session_token = generate_token()
    raw_csrf_token = generate_token()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)).isoformat()

    await web_auth_repository.create_session(
        web_user_id, binding_id,
        hash_token(raw_session_token), hash_token(raw_csrf_token), expires_at,
    )
    _set_auth_cookies(response, raw_session_token, raw_csrf_token)


async def get_current_principal(request: Request) -> WebPrincipal:
    """Identity comes from the server-verified session, never from
    anything the client could pass as a parameter."""
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(status_code=401, detail="Не авторизован.")

    ctx = await web_auth_repository.get_session_context(hash_token(raw_token))
    if (
        ctx is None
        or ctx.revoked_at is not None
        or ctx.expires_at <= _now_iso()
        or ctx.user_status != "active"
    ):
        raise HTTPException(status_code=401, detail="Сессия недействительна.")

    # Fail closed: the web-auth binding only claims "this account acts as
    # this (workspace_id, telegram_user_id) pair" - it is NOT itself proof
    # of access. PartnerRepository's workspace_memberships/
    # partner_workspaces is the one access model in this codebase, and
    # every authenticated request must be re-checked against it: a
    # membership that's been deactivated or deleted, or a workspace that's
    # been suspended, after the session was created must immediately stop
    # granting access. A failure of this check itself (exception, or no
    # active membership, or the membership resolving to a DIFFERENT
    # workspace than this binding claims) must also deny access - never a
    # silent role="member" fallback that still lets the request through.
    try:
        workspace_context = await partner_repository.resolve_workspace_context(
            ctx.telegram_user_id,
        )
    except Exception:
        raise HTTPException(
            status_code=403, detail="Не удалось подтвердить доступ к рабочему пространству.",
        )

    if workspace_context is None or workspace_context.workspace_id != ctx.workspace_id:
        raise HTTPException(status_code=403, detail="Доступ к рабочему пространству отозван.")

    await web_auth_repository.touch_session_last_seen(ctx.session_id)

    return WebPrincipal(
        web_user_id=ctx.web_user_id,
        email=ctx.email,
        workspace_id=ctx.workspace_id,
        telegram_user_id=ctx.telegram_user_id,
        role=workspace_context.role,
        session_id=ctx.session_id,
        csrf_token_hash=ctx.csrf_token_hash,
        binding_id=ctx.binding_id,
    )


async def require_csrf(
    request: Request,
    principal: WebPrincipal = Depends(get_current_principal),
) -> WebPrincipal:
    """For every state-changing (POST/PUT/DELETE) authenticated endpoint -
    GET/read-only endpoints only need get_current_principal, no CSRF
    check."""
    header_token = request.headers.get("x-csrf-token", "")
    if not header_token or not tokens_match(header_token, principal.csrf_token_hash):
        raise HTTPException(status_code=403, detail="Недействительный CSRF-токен.")
    return principal


# ── subscription gate: one workspace subscription, both channels ───────
#
# get_current_principal()/require_csrf() above are auth/membership/lifecycle
# ONLY - they answer "is this a real, still-active member of this
# workspace". Whether the WORKSPACE's subscription itself is active is a
# separate question, answered the same way Telegram answers it
# (AccessStateMiddleware, app/access_state_gate.py): both resolve through
# SubscriptionRepository.resolve_access_state(), the one function that
# reduces workspace_subscriptions to a grant/deny decision (see
# app/services/access_state.py). Login/logout/register/me stay on
# get_current_principal/require_csrf directly (never gated here) so an
# account with an inactive subscription can still sign in and see its own
# state - only the product's working API surface goes through these two.

async def get_active_principal(
    principal: WebPrincipal = Depends(get_current_principal),
) -> WebPrincipal:
    """Subscription-gated read access - use in place of get_current_principal
    on every GET endpoint that touches product data (materials, signals,
    competitors, knowledge, profile, conversations, ...)."""
    state = await subscription_repository.resolve_access_state(principal.workspace_id)
    if not is_access_granted(state):
        await record_event(
            operational_event_repository, module="subscription_gate", event_type="denied",
            success=False, workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
            severity=EventSeverity.INFO, error_code=state,
            safe_message="read access denied: subscription not active",
        )
        raise HTTPException(
            status_code=402,
            detail={"error": "subscription_inactive", "access_state": state},
        )
    return principal


async def require_csrf_and_subscription(
    principal: WebPrincipal = Depends(require_csrf),
) -> WebPrincipal:
    """Subscription-gated write access - use in place of require_csrf on
    every mutating (POST/PUT/DELETE) product endpoint. CSRF is checked
    first (require_csrf), subscription second - a forged/missing CSRF
    token gets the same 403 it always did, never a 402 that would leak
    subscription state to an unauthenticated-for-this-session request."""
    state = await subscription_repository.resolve_access_state(principal.workspace_id)
    if not is_access_granted(state):
        await record_event(
            operational_event_repository, module="subscription_gate", event_type="denied",
            success=False, workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
            severity=EventSeverity.INFO, error_code=state,
            safe_message="write access denied: subscription not active",
        )
        raise HTTPException(
            status_code=402,
            detail={"error": "subscription_inactive", "access_state": state},
        )
    return principal


async def _is_platform_admin(email: str) -> bool:
    return email.strip().lower() in settings.orchestravel_admin_emails


async def require_platform_admin(
    principal: WebPrincipal = Depends(get_current_principal),
) -> WebPrincipal:
    """Beta Control Center gate (see app/admin_api.py) - completely
    separate from any workspace membership role: a workspace owner/admin
    is NEVER a platform admin just by being one. Fail-closed: an empty
    ORCHESTRAVEL_ADMIN_EMAILS means nobody passes, ever, no matter who
    they are. 404 (not 403) on failure - a regular authenticated user
    hitting an admin route gets the same response as a route that simply
    doesn't exist, so /admin's existence is never confirmed to anyone
    probing it. No second login: this builds entirely on the same
    get_current_principal session every other endpoint already uses."""
    if not await _is_platform_admin(principal.email):
        raise HTTPException(status_code=404)
    return principal


async def require_platform_admin_csrf(
    principal: WebPrincipal = Depends(require_csrf),
) -> WebPrincipal:
    """Same gate as require_platform_admin, for admin POST/PUT/DELETE
    mutations - CSRF is checked first (require_csrf), platform-admin
    membership second."""
    if not await _is_platform_admin(principal.email):
        raise HTTPException(status_code=404)
    return principal


# Beta Control Center router (app/admin_api.py) - built here, not in
# admin_api.py itself, so admin_api.py never has to import app.web_api
# (which would be circular: web_api.py already imports admin_api.py to
# call this). Every dependency is an instance this module already
# constructed above; require_platform_admin/require_platform_admin_csrf
# are the only gate the whole router sits behind.
app.include_router(build_admin_router(
    AdminDeps(
        web_auth_repository=web_auth_repository,
        subscription_repository=subscription_repository,
        payment_order_repository=payment_order_repository,
        usage_ledger_repository=usage_ledger_repository,
        competitor_repository=competitor_repository,
        artifact_repository=artifact_repository,
        web_conversation_repository=web_conversation_repository,
        web_attachment_repository=web_attachment_repository,
        operational_event_repository=operational_event_repository,
        feedback_repository=feedback_repository,
        admin_audit_log_repository=admin_audit_log_repository,
        admin_directory_repository=admin_directory_repository,
        robokassa_config=robokassa_config,
        upload_storage_root=attachment_storage.root,
        llm_provider_configured=bool(settings.planner_openai_api_key),
    ),
    require_platform_admin=require_platform_admin,
    require_platform_admin_csrf=require_platform_admin_csrf,
))


# Fixed-cost dummy hash for login timing - see login() below: without
# this, "no such email" (skips verify_password entirely) would be
# measurably faster than "wrong password" (runs a real Argon2id verify),
# letting an attacker enumerate registered emails by response time even
# though the JSON error message itself is identical either way.
_DUMMY_PASSWORD_HASH = hash_password("not-a-real-password-placeholder-value")


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    invite_token: str
    email: str
    password: str


@app.post("/api/auth/login")
async def login(request: LoginRequest, response: Response):
    generic_error = {"error": "Неверный email или пароль."}

    try:
        user = await web_auth_repository.get_user_by_email(request.email)
        password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
        password_ok = verify_password(request.password, password_hash)

        if user is None or user.status != "active" or not password_ok:
            await record_event(
                operational_event_repository, module="auth", event_type="login",
                success=False, severity=EventSeverity.INFO,
                safe_message="login rejected",
            )
            return generic_error

        binding = await web_auth_repository.get_default_binding(user.id)
        if binding is None:
            # A web account with no workspace binding can't do anything -
            # fail closed the same way an unowned resource does elsewhere.
            await record_event(
                operational_event_repository, module="auth", event_type="login",
                success=False, web_user_id=user.id, severity=EventSeverity.WARNING,
                safe_message="login rejected: no workspace binding",
            )
            return generic_error

        await _start_session(response, user.id, binding.id)
        await web_auth_repository.touch_last_login(user.id)
        await record_event(
            operational_event_repository, module="auth", event_type="login",
            success=True, workspace_id=binding.workspace_id, web_user_id=user.id,
        )

        return {"email": user.email, "workspace_id": binding.workspace_id}

    except Exception:
        await record_event(
            operational_event_repository, module="auth", event_type="login",
            success=False, severity=EventSeverity.ERROR,
            safe_message="login raised an exception",
        )
        return {"error": "Не удалось выполнить вход. Попробуйте ещё раз."}


@app.post("/api/auth/register")
async def register(request: RegisterRequest, response: Response):
    """Beta registration is invite-only - see scripts/create_beta_invite.py.
    No public self-serve signup exists."""
    email = request.email.strip().lower()
    # Same normalization as email above - a token copy-pasted from a
    # terminal (e.g. the CLI's printed URL/token) very easily picks up a
    # trailing newline or stray space, which silently changes its SHA-256
    # hash and makes an otherwise-valid, unexpired invite look "invalid"
    # (see app.services.web_auth_tokens.hash_token - it hashes exactly the
    # bytes it's given, no normalization of its own).
    invite_token = request.invite_token.strip()
    invite_error = {"error": "Приглашение недействительно, уже использовано или истекло."}

    try:
        validate_password_policy(request.password)
    except WeakPasswordError as exc:
        return {"error": str(exc)}

    try:
        invite = await web_auth_repository.get_invite_by_token_hash(
            hash_token(invite_token),
        )
        if invite is None or invite.used_at is not None or invite.expires_at <= _now_iso():
            return invite_error

        if invite.email_restriction and invite.email_restriction != email:
            return {"error": "Это приглашение предназначено для другого email."}

        # Fail closed: re-check against PartnerRepository's own access
        # model at the moment of granting access, not just whatever was
        # true when the invite was created (see scripts/create_beta_invite.py) -
        # membership could have been deactivated, or the workspace
        # suspended, any time in between. An invite is a claim, never a
        # bypass of the real access model.
        try:
            workspace_context = await partner_repository.resolve_workspace_context(
                invite.telegram_user_id,
            )
        except Exception:
            return invite_error

        if workspace_context is None or workspace_context.workspace_id != invite.workspace_id:
            return invite_error

        if await web_auth_repository.get_user_by_email(email) is not None:
            return {"error": "Этот email уже зарегистрирован."}

        consumed = await web_auth_repository.consume_invite(hash_token(invite_token))
        if consumed is None:
            # Lost a race against another registration using the same
            # invite (or the invite expired in the meantime).
            return invite_error

        password_hash = hash_password(request.password)
        user = await web_auth_repository.create_user(email, password_hash)
        binding = await web_auth_repository.create_binding(
            user.id, consumed.workspace_id, consumed.telegram_user_id,
        )

        await _start_session(response, user.id, binding.id)
        await record_event(
            operational_event_repository, module="auth", event_type="register",
            success=True, workspace_id=binding.workspace_id, web_user_id=user.id,
        )

        return {"email": user.email, "workspace_id": binding.workspace_id}

    except EmailAlreadyRegisteredError as exc:
        await record_event(
            operational_event_repository, module="auth", event_type="register",
            success=False, severity=EventSeverity.INFO,
            safe_message="register rejected: email already registered",
        )
        return {"error": str(exc)}
    except Exception:
        await record_event(
            operational_event_repository, module="auth", event_type="register",
            success=False, severity=EventSeverity.ERROR,
            safe_message="register raised an exception",
        )
        return {"error": "Не удалось завершить регистрацию. Попробуйте ещё раз."}


@app.post("/api/auth/logout")
async def logout(
    request: Request, response: Response,
    principal: WebPrincipal = Depends(require_csrf),
):
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token:
        await web_auth_repository.revoke_session(hash_token(raw_token))
    _clear_auth_cookies(response)
    return {"logged_out": True}


@app.get("/api/auth/me")
async def get_me(principal: WebPrincipal = Depends(get_current_principal)):
    """Deliberately NOT gated by require_active_subscription - an account
    with an expired/past_due/suspended workspace must still be able to see
    who it is and what its access_state is (the frontend uses this to
    decide whether to render the cabinet or redirect to
    /subscription-inactive), it just can't reach product endpoints."""
    access_state = await subscription_repository.resolve_access_state(
        principal.workspace_id,
    )
    return {
        "email": principal.email,
        "workspace_id": principal.workspace_id,
        "role": principal.role,
        "access_state": access_state,
        "access_granted": is_access_granted(access_state),
        # Same check require_platform_admin (app/admin_api.py's gate) uses -
        # never reimplemented here, so the web cabinet's "Админка" link
        # (see chat.html) shows for exactly the same people who can
        # actually reach /admin, nothing decided client-side.
        "is_platform_admin": await _is_platform_admin(principal.email),
    }


# ── billing (RoboKassa) ──────────────────────────────────────────────────
#
# Deliberately NOT gated by get_active_principal/require_csrf_and_subscription
# anywhere in this section - an expired/past_due/suspended workspace is
# EXACTLY who needs to reach these endpoints to pay. Only
# get_current_principal/require_csrf (auth + membership, no subscription
# check) are used. workspace_id always comes from principal, never from the
# request body - a client can never create a payment for, or ask about, any
# workspace but its own.


@app.get("/api/billing/status")
async def billing_status(principal: WebPrincipal = Depends(get_current_principal)):
    subscription = await subscription_repository.get_for_workspace(principal.workspace_id)
    access_state = await subscription_repository.resolve_access_state(principal.workspace_id)
    return {
        "access_state": access_state,
        "access_granted": is_access_granted(access_state),
        "status": subscription.status.value if subscription is not None else None,
        "plan": subscription.plan.value if subscription is not None else None,
        "paid_until": subscription.paid_until if subscription is not None else None,
        "trial_until": subscription.trial_until if subscription is not None else None,
        "billing_configured": robokassa_config.is_configured,
        "is_test": robokassa_config.is_test,
        "standard_price_rub": (
            str(robokassa_config.standard_price_rub)
            if robokassa_config.standard_price_rub is not None else None
        ),
        "subscription_days": robokassa_config.subscription_days,
    }


@app.post("/api/billing/create-payment")
async def create_payment_endpoint(principal: WebPrincipal = Depends(require_csrf)):
    """amount/plan/description come only from server-side RoboKassaConfig
    (see BillingService.create_payment) - the request body is intentionally
    not even parsed, there is nothing for a client to influence beyond
    "pay for MY workspace, on the one plan that exists"."""
    if not robokassa_config.is_configured:
        return {"error": "Оплата временно недоступна. Обратитесь к администратору."}
    try:
        result = await billing_service.create_payment(principal.workspace_id)
    except BillingNotConfigured:
        return {"error": "Оплата временно недоступна. Обратитесь к администратору."}
    except Exception:
        log.exception("billing: create_payment failed for workspace_id=%s", principal.workspace_id)
        await record_event(
            operational_event_repository, module="billing", event_type="create_payment",
            success=False, workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
            severity=EventSeverity.ERROR, error_code="unhandled_exception",
        )
        return {"error": "Не удалось создать платёж. Попробуйте ещё раз."}
    await record_event(
        operational_event_repository, module="billing", event_type="create_payment",
        success=True, workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
        metadata={"is_test": result.is_test, "order_id": result.order.id},
    )
    return {
        "order_id": result.order.id,
        "payment_url": result.payment_url,
        "is_test": result.is_test,
        "amount": result.order.amount,
        "currency": result.order.currency,
    }


@app.get("/api/billing/orders/{order_id}")
async def get_payment_order(
    order_id: int, principal: WebPrincipal = Depends(get_current_principal),
):
    """Backs /billing/success's polling - SuccessURL is not the source of
    truth, this endpoint is. order_id (RoboKassa's InvId) comes from the
    browser's own query string, but the response only ever reveals
    anything when the order's workspace_id matches the session's own
    workspace_id - a forged/guessed order_id belonging to another
    workspace returns the same generic "not found" as one that doesn't
    exist at all."""
    order = await payment_order_repository.get_order(order_id)
    if order is None or order.workspace_id != principal.workspace_id:
        return {"error": "Заказ не найден.", "order": None}
    return {
        "order": {
            "id": order.id,
            "status": order.status.value,
            "plan": order.plan,
            "amount": order.amount,
            "currency": order.currency,
            "paid_at": order.paid_at,
        }
    }


@app.post("/api/billing/robokassa/result")
async def robokassa_result(request: Request):
    """RoboKassa's server-to-server ResultURL - no web session, no CSRF
    (RoboKassa's server can't present either): the ONLY trust boundary is
    verify_result_signature() inside BillingService.process_result_callback,
    checked against ROBOKASSA_PASSWORD2. Configure this exact path as the
    ResultURL in the RoboKassa merchant cabinet - see the deployment report
    for the full URL. Must return exactly "OK{InvId}" on success (RoboKassa
    retries otherwise) and never leak why a request was rejected."""
    form = await request.form()
    out_sum = str(form.get("OutSum", "")).strip()
    inv_id_raw = str(form.get("InvId", "")).strip()
    signature = str(form.get("SignatureValue", "")).strip()

    try:
        inv_id = int(inv_id_raw)
    except ValueError:
        log.warning("robokassa result: non-numeric InvId in callback")
        return PlainTextResponse("bad request", status_code=400)

    outcome = await billing_service.process_result_callback(
        out_sum=out_sum, inv_id=inv_id, signature=signature,
    )
    if not outcome.ok:
        # outcome.reason is a neutral code (see ResultOutcome), never the
        # raw signature/passwords - safe to log, never returned in the
        # response body.
        log.warning("robokassa result: rejected InvId=%s reason=%s", inv_id, outcome.reason)
        # workspace_id is deliberately omitted here - a rejected callback
        # (bad signature/amount/unknown order) has not been proven to
        # belong to any real workspace, so attributing it to one would be
        # a fabrication, not a fact.
        await record_event(
            operational_event_repository, module="billing", event_type="robokassa_callback",
            success=False, severity=EventSeverity.WARNING, error_code=outcome.reason,
            request_id=str(inv_id), safe_message="ResultURL rejected",
        )
        return PlainTextResponse("bad request", status_code=400)
    order = await payment_order_repository.get_order(inv_id)
    await record_event(
        operational_event_repository, module="billing", event_type="robokassa_callback",
        success=True, workspace_id=order.workspace_id if order is not None else None,
        request_id=str(inv_id),
    )
    return PlainTextResponse(f"OK{inv_id}")


class ChatRequest(BaseModel):
    message: str = ""
    conversation_id: int
    # public_id values of already-uploaded pending attachments (see
    # POST /api/attachments) to bind to this turn's user message. A
    # message can be attachments-only (message == "") but not both empty.
    attachment_ids: list[str] = Field(default_factory=list)


@app.on_event("startup")
async def startup() -> None:
    await knowledge_repository.init()
    await usage_ledger_repository.init()
    await competitor_repository.init()
    await partner_repository.init()
    await workspace_memory_repository.init()
    await artifact_repository.init()
    await web_conversation_repository.init()
    await web_attachment_repository.init()
    await web_auth_repository.init()
    await subscription_repository.init()
    await payment_order_repository.init()
    await operational_event_repository.init()
    await feedback_repository.init()
    await admin_audit_log_repository.init()
    # legacy_owner_workspace_id=None: the one-time legacy-Radar backfill is
    # already owned by the bot process (app/main.py) against the same shared
    # journal DB - this just ensures the schema exists, it never re-runs
    # that backfill from the web process.
    await workspace_signal_repository.init(None)

    try:
        await _reap_orphan_attachments()
    except Exception:
        # Best-effort housekeeping - must never block the app from starting.
        log.warning("web_api: orphan attachment reap failed at startup", exc_info=True)


async def _reap_orphan_attachments() -> None:
    """Deletes pending attachments (uploaded, never attached to a sent
    message) older than ATTACHMENT_ORPHAN_TTL, plus their physical files -
    see WebAttachmentRepository.delete_orphans_older_than()'s docstring.
    Runs once per process start; good enough for a single-server deployment
    with no separate cron/worker process."""
    cutoff = (datetime.now(timezone.utc) - ATTACHMENT_ORPHAN_TTL).isoformat()
    orphans = await web_attachment_repository.delete_orphans_older_than(cutoff)
    for orphan in orphans:
        attachment_storage.delete(
            orphan.workspace_id, orphan.conversation_id, orphan.stored_filename,
        )


def _fact_value(fact) -> str:
    if fact.value_text is not None:
        return fact.value_text

    if fact.value_number is not None:
        value = str(fact.value_number)
    elif fact.range_min is not None or fact.range_max is not None:
        value = f"{fact.range_min}–{fact.range_max}"
    else:
        value = "(значение не указано)"

    suffix = " ".join(
        str(value)
        for value in (fact.currency, fact.unit, fact.period)
        if value
    )

    return f"{value} {suffix}".strip()


def _knowledge_context(bundle: KnowledgeBundle) -> str:
    if not (
        bundle.primary_items
        or bundle.facts
        or bundle.compliance_facts
    ):
        return ""

    lines = [
        "=== ПРОВЕРЕННАЯ БАЗА ЗНАНИЙ ===",
    ]

    if bundle.primary_items:
        lines.append("\nРелевантные разделы:")

        for item in bundle.primary_items[:5]:
            content = item.content.strip()

            if len(content) > 1200:
                content = content[:1200] + "…"

            lines.append(
                f"- [{item.stable_key}] {item.title}: {content} "
                f"(источник: {item.source_ref})"
            )

    if bundle.facts:
        lines.append("\nКанонические факты:")

        for fact in bundle.facts[:30]:
            condition = (
                f"; условие: {fact.condition_text}"
                if fact.condition_text
                else ""
            )

            lines.append(
                f"- [{fact.stable_key}] "
                f"{fact.subject_key}: {_fact_value(fact)}"
                f"{condition} "
                f"(источник: {fact.source_ref})"
            )

    if bundle.compliance_facts:
        lines.append("\nОбязательные ограничения:")

        for fact in bundle.compliance_facts:
            lines.append(
                f"- [{fact.stable_key}] {_fact_value(fact)} "
                f"(источник: {fact.source_ref})"
            )

    if bundle.examples:
        lines.append("\nПроверенные примеры:")

        for example in bundle.examples[:2]:
            lines.append(
                f"- {example.title}: {example.scenario}. "
                f"{example.explanation}"
            )

    if bundle.sources:
        lines.append("\nИсточники:")

        for source in bundle.sources[:8]:
            lines.append(
                f"- {source.title} | "
                f"{source.verification_status} | "
                f"{source.source_reference}"
            )

    if bundle.potentially_ambiguous:
        lines.append(
            "\nВнимание: запрос потенциально неоднозначен."
        )

        for reason in bundle.ambiguity_reasons:
            lines.append(f"- {reason}")

    for missing in bundle.missing_definitions:
        lines.append(f"- В базе отсутствует: {missing}")

    return "\n".join(lines)


async def _requested_competitor(workspace_id: int, message: str):
    lowered = message.lower()

    markers = (
        "проанализ",
        "анализ",
        "аудит",
        "разбери",
        "сравни",
        "конкурент",
    )

    if not any(marker in lowered for marker in markers):
        return None

    competitors = await competitor_repository.list_for_workspace(
        workspace_id,
        limit=50,
    )

    for competitor in competitors:
        domain = canonical_domain(competitor.url).lower()
        label = competitor.label.lower()

        if domain and domain in lowered:
            return competitor

        if label and label in lowered:
            return competitor

    return None


def _competitor_context(intelligence) -> str:
    payload = json.dumps(
        asdict(intelligence),
        ensure_ascii=False,
        indent=2,
    )

    if len(payload) > 24000:
        payload = payload[:24000] + "\n... [контекст сокращён]"

    # Step 4/6: the preamble must never claim site data when the underlying
    # evidence is actually a Radar-signal fallback (see
    # app.services.competitor_intelligence.analyze) - the LLM is instructed
    # accordingly so it can't present a third-party mention as if it came
    # from the competitor's own site.
    if getattr(intelligence, "data_origin", None) == DATA_ORIGIN_RADAR_SIGNAL:
        preamble = (
            "Сайт конкурента прочитать не удалось. Ниже - релевантные свежие "
            "публичные упоминания конкурента (Radar), НЕ данные с его "
            "собственного сайта. Явно сообщи пользователю, что это сторонние "
            "упоминания, а не анализ сайта конкурента."
        )
    else:
        preamble = "Данные получены из публичных источников конкурента."

    return (
        "=== COMPETITOR INTELLIGENCE ===\n"
        + preamble + "\n"
        + payload
    )


def _business_profile_context(profile) -> str:
    """Same safe projection Content Factory uses for LLM prompts
    (build_assistant_context: unverified claims excluded) - reused as-is
    so editing BusinessProfile in /api/profile takes effect on the very
    next /api/chat call, without a second profile-context builder."""
    data = build_assistant_context(profile)
    lines = ["=== ПРОФИЛЬ БИЗНЕСА ==="]

    if data["business_name"]:
        lines.append(f"Название: {data['business_name']}")
    if data["short_description"]:
        lines.append(f"Описание: {data['short_description']}")
    if data["specializations"]:
        lines.append("Специализации: " + ", ".join(data["specializations"]))
    if data["destinations"]:
        lines.append("Направления: " + ", ".join(data["destinations"]))
    if data["preferred_terms"]:
        lines.append("Предпочтительные формулировки: " + ", ".join(data["preferred_terms"]))
    if data["verified_claims"]:
        lines.append(
            "Подтверждённые факты: "
            + "; ".join(claim["text"] for claim in data["verified_claims"])
        )

    if len(lines) == 1:
        return ""

    return "\n".join(lines)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "travel-ai-orchestrator-web",
    }


@app.get("/api/competitors")
async def list_competitors(principal: WebPrincipal = Depends(get_active_principal)):
    try:
        competitors = await competitor_repository.list_for_workspace(principal.workspace_id)
        last_analyzed = await competitor_repository.list_intelligence_dates_for_workspace(
            principal.workspace_id
        )

        return {
            "competitors": [
                {
                    "id": competitor.id,
                    "label": competitor.label,
                    "domain": canonical_domain(competitor.url),
                    "url": competitor.url,
                    "last_analyzed_at": last_analyzed.get(competitor.id),
                }
                for competitor in competitors
            ]
        }

    except Exception:
        return {"error": "Не удалось загрузить список конкурентов."}


class AddCompetitorRequest(BaseModel):
    url: str
    label: str = ""


@app.post("/api/competitors")
async def add_competitor_endpoint(
    request: AddCompetitorRequest,
    principal: WebPrincipal = Depends(require_csrf_and_subscription),
):
    """Web-first path for the same competitor_repository.add_competitor()
    call the Telegram "➕ Добавить конкурента" flow uses (see
    app/handlers/competitors.py) - no parallel competitor model, and the
    same CompetitorAddressError/CompetitorLabelError validation. Any
    active workspace member can add one, same as Telegram (no owner/admin
    gate there either - only BusinessProfile writes are role-restricted).

    workspace_id comes only from principal (the server-verified session,
    re-checked against workspace_memberships on every request by
    get_current_principal) - never from the request body, so a client
    can't add a competitor into someone else's workspace."""
    try:
        competitor = await competitor_repository.add_competitor(
            principal.workspace_id, request.url, label=request.label,
        )
        await record_event(
            operational_event_repository, module="competitors", event_type="add",
            success=True, workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
        )
        return {
            "competitor": {
                "id": competitor.id,
                "label": competitor.label,
                "domain": canonical_domain(competitor.url),
                "url": competitor.url,
                "last_analyzed_at": None,
            }
        }

    except (CompetitorAddressError, CompetitorLabelError) as exc:
        await record_event(
            operational_event_repository, module="competitors", event_type="add",
            success=False, workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
            severity=EventSeverity.INFO, error_code="validation_rejected",
        )
        return {"error": str(exc), "competitor": None}
    except Exception:
        await record_event(
            operational_event_repository, module="competitors", event_type="add",
            success=False, workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
            severity=EventSeverity.ERROR, error_code="unhandled_exception",
        )
        return {"error": "Не удалось добавить конкурента.", "competitor": None}


@app.get("/api/competitors/{competitor_id}/intelligence")
async def get_competitor_intelligence(
    competitor_id: int, principal: WebPrincipal = Depends(get_active_principal),
):
    try:
        competitor = await competitor_repository.get_for_workspace(
            principal.workspace_id, competitor_id,
        )

        if competitor is None:
            return {"error": "Конкурент не найден.", "competitor": None, "intelligence": None}

        intelligence = await competitor_repository.get_intelligence(
            principal.workspace_id, competitor_id,
        )

        return {
            "competitor": {
                "id": competitor.id,
                "label": competitor.label,
                "domain": canonical_domain(competitor.url),
                "url": competitor.url,
            },
            "intelligence": asdict(intelligence) if intelligence is not None else None,
        }

    except Exception:
        return {"error": "Не удалось загрузить отчёт по конкуренту."}


@app.post("/api/competitors/{competitor_id}/opportunities/{opportunity_id}/actions")
async def create_material_from_competitor_opportunity(
    competitor_id: int,
    opportunity_id: str,
    request: MaterialActionRequest,
    principal: WebPrincipal = Depends(require_csrf_and_subscription),
):
    """«Что можно сделать» -> готовый материал: тот же
    MaterialOrchestrationService.build_competitor_signal_generation_spec и тот
    же competitor_llm_provider (Content Factory), что уже использует Telegram
    (app/handlers/competitors.py:create_from_competitor_opportunity). Ни один
    из аргументов спека не придуман здесь - все берутся из уже посчитанного
    ContentOpportunity (opportunity.topic/key_thesis/own_post_angle/
    audience_value/source_title/source_url/travel_advantage_link), как и в
    Telegram. Отличие от Telegram: там черновик только показывается в чате -
    здесь он ещё и сохраняется как Artifact (create_artifact_with_initial_version),
    чтобы попасть в «Материалы», с data_origin (direct_fetch/radar_signal) в
    generation_note - fallback-анализ никогда не выдаётся за свежий direct
    fetch. travel_advantage_link уже отфильтрован источником только для
    TA-affiliated workspace (app/services/competitor_intelligence.py) -
    ta_affiliated isolation соблюдена на уровне данных, здесь ничего
    дополнительно решать не нужно."""
    if request.action not in _SIGNAL_OR_COMPETITOR_MATERIAL_ACTIONS:
        return {"error": "Неизвестное действие.", "material": None}

    try:
        competitor = await competitor_repository.get_for_workspace(
            principal.workspace_id, competitor_id,
        )
        if competitor is None:
            return {"error": "Конкурент не найден.", "material": None}

        intelligence = await competitor_repository.get_intelligence(
            principal.workspace_id, competitor_id,
        )
        if intelligence is None:
            return {"error": "Анализ конкурента ещё не готов.", "material": None}

        opportunity = next(
            (item for item in intelligence.opportunities if item.id == opportunity_id), None,
        )
        if opportunity is None:
            return {"error": "Рекомендация недоступна.", "material": None}

        await record_event(
            operational_event_repository, module="competitors", event_type="action_selected",
            success=True, workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
            metadata={
                "competitor_id": competitor_id, "opportunity_id": opportunity_id,
                "action": request.action, "data_origin": intelligence.data_origin,
            },
        )

        profile = await partner_repository.get_business_profile(principal.workspace_id)
        user_preferences = await partner_repository.get_user_preferences(
            principal.workspace_id, principal.telegram_user_id,
        )

        spec = material_orchestration_service.build_competitor_signal_generation_spec(
            principal.workspace_id, profile,
            competitor_signal=opportunity.topic, key_thesis=opportunity.key_thesis,
            own_post_angle=opportunity.own_post_angle, audience_value=opportunity.audience_value,
            source_title=opportunity.source_title, source_url=opportunity.source_url,
            travel_advantage_link=opportunity.travel_advantage_link,
            user_preferences=user_preferences, artifact_type=request.action,
        )
        provider_request = build_provider_generation_request(spec, limit=6000)

        draft = await asyncio.to_thread(
            competitor_llm_provider.generate_draft,
            source_text=provider_request.source_text,
            material_type=provider_request.material_type,
            output_format=provider_request.output_format,
            mode="ai",
        )
        if draft is None:
            await record_event(
                operational_event_repository, module="materials",
                event_type="material_created_from_competitor",
                success=False, workspace_id=principal.workspace_id,
                web_user_id=principal.web_user_id, severity=EventSeverity.INFO,
                error_code="draft_unavailable",
                metadata={
                    "competitor_id": competitor_id, "opportunity_id": opportunity_id,
                    "action": request.action,
                },
            )
            return {
                "error": "Не удалось подготовить материал. Попробуйте ещё раз.",
                "material": None,
            }

        sanitized = sanitize_draft_text(draft.text)
        artifact, version = await artifact_repository.create_artifact_with_initial_version(
            principal.workspace_id,
            artifact_type=spec.artifact_type,
            title=opportunity.topic or "Материал по конкуренту",
            content=sanitized,
            generation_note=(
                f"Конкурент: {competitor.label}; opportunity_id={opportunity_id}; "
                f"data_origin={intelligence.data_origin}"
            ),
        )

        await record_event(
            operational_event_repository, module="materials",
            event_type="material_created_from_competitor",
            success=True, workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
            metadata={
                "competitor_id": competitor_id, "opportunity_id": opportunity_id,
                "action": request.action, "artifact_id": artifact.id,
                "data_origin": intelligence.data_origin,
            },
        )

        return {
            "material": _material_payload(artifact),
            "version": _version_payload(version),
            "origin": {
                "kind": "competitor",
                "competitor_label": competitor.label,
                "topic": opportunity.topic,
                "data_origin": intelligence.data_origin,
                "analyzed_at": intelligence.analyzed_at,
            },
        }

    except Exception:
        await record_event(
            operational_event_repository, module="materials",
            event_type="material_created_from_competitor",
            success=False, workspace_id=principal.workspace_id,
            web_user_id=principal.web_user_id, severity=EventSeverity.ERROR,
            error_code="unhandled_exception",
            metadata={
                "competitor_id": competitor_id, "opportunity_id": opportunity_id,
                "action": request.action,
            },
        )
        return {"error": "Не удалось подготовить материал.", "material": None}


@app.get("/api/signals")
async def list_signals(principal: WebPrincipal = Depends(get_active_principal)):
    """Read-only: свежие сигналы Radar для текущего workspace.

    Использует ровно тот же путь чтения, что и Telegram-хэндлер
    on_find_signals() (app/handlers/menu.py) - list_for_workspace() затем
    build_workspace_signals() с теми же лимитами (200 -> 5). В отличие от
    Telegram-хэндлера здесь намеренно НЕ вызывается sync_eligible(): это
    write-операция (материализация новых interpretation-строк), а этот
    эндпоинт должен оставаться строго read-only. Синхронизация уже
    выполняется процессом бота (app/main.py, при старте и при каждом
    /find_signals) в ту же общую БД. Никакого нового LLM-вызова - и
    list_for_workspace(), и build_workspace_signals() только читают и
    фильтруют уже сохранённые данные.
    """
    try:
        records = await workspace_signal_repository.list_for_workspace(
            principal.workspace_id, limit=200,
        )
        signals = build_workspace_signals(lead_radar_config, records, limit=DISPLAY_LIMIT)

        if signals is None:
            return {"error": "Радар сигналов сейчас недоступен.", "signals": []}

        source_names = {
            record.interpretation_id: record.source_name for record in records
        }

        await record_event(
            operational_event_repository, module="signals", event_type="read",
            success=True, workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
            metadata={"count": len(signals)},
        )
        return {
            "signals": [
                {
                    "id": signal.id,
                    "title": signal.title or "(без заголовка)",
                    "category": signal.category,
                    "category_label": category_label(signal.category),
                    "source_type": signal.source_type,
                    "source_name": source_names.get(signal.id) or "",
                    "created_at": signal.created_at,
                    "score": signal.score,
                    "url": signal.url,
                    "action_reason": signal.action_reason,
                }
                for signal in signals
            ]
        }

    except Exception:
        return {"error": "Не удалось загрузить сигналы.", "signals": []}


@app.post("/api/signals/{interpretation_id}/actions")
async def create_material_from_signal(
    interpretation_id: int,
    request: MaterialActionRequest,
    principal: WebPrincipal = Depends(require_csrf_and_subscription),
):
    """Сигнал -> готовый материал: тот же
    MaterialOrchestrationService.build_radar_generation_spec и тот же
    competitor_llm_provider (Content Factory), что уже использует Telegram
    (app/handlers/menu.py:on_radar_content_selected) - тот же Source Analysis
    Quality Gate (analyze_source ДО generate_draft; артефакт не создаётся,
    если анализ недоступен - fail closed, а не "молча пропустить проверку")
    и та же санитизация черновика (sanitize_draft_text) перед сохранением.
    Никакого второго генератора и никакого нового LLM provider."""
    if request.action not in _SIGNAL_OR_COMPETITOR_MATERIAL_ACTIONS:
        return {"error": "Неизвестное действие.", "material": None}

    try:
        record = await workspace_signal_repository.get_for_workspace(
            principal.workspace_id, interpretation_id,
        )
        if record is None:
            return {"error": "Сигнал недоступен.", "material": None}

        # Та же проверка видимости, что и в /api/signals - источник должен
        # быть активен и подключён к workspace прямо сейчас, иначе сигнал
        # не пригоден для генерации, даже если строка формально существует.
        authorized = build_workspace_signals(lead_radar_config, [record], limit=1)
        if not authorized:
            return {"error": "Сигнал недоступен.", "material": None}
        signal = authorized[0]

        await record_event(
            operational_event_repository, module="signals", event_type="action_selected",
            success=True, workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
            metadata={"signal_id": interpretation_id, "action": request.action},
        )

        profile = await partner_repository.get_business_profile(principal.workspace_id)
        user_preferences = await partner_repository.get_user_preferences(
            principal.workspace_id, principal.telegram_user_id,
        )

        radar_source_text = "\n".join(
            value for value in (record.item_title, record.item_summary) if value
        )
        analysis = await asyncio.to_thread(
            competitor_llm_provider.analyze_source, source_text=radar_source_text,
        )
        if analysis is None:
            await record_event(
                operational_event_repository, module="materials",
                event_type="material_created_from_signal",
                success=False, workspace_id=principal.workspace_id,
                web_user_id=principal.web_user_id, severity=EventSeverity.INFO,
                error_code="analysis_unavailable",
                metadata={"signal_id": interpretation_id, "action": request.action},
            )
            return {
                "error": "Не удалось подготовить материал: анализ источника недоступен.",
                "material": None,
            }

        spec = material_orchestration_service.build_radar_generation_spec(
            principal.workspace_id, profile,
            title=record.item_title, summary=record.item_summary,
            source_type=record.source_type, origin_type=record.origin_type,
            url=record.item_url, category=record.ai_category or "",
            reason=record.ai_reason or signal.action_reason,
            analysis=analysis, user_preferences=user_preferences,
            artifact_type=request.action,
        )
        provider_request = build_provider_generation_request(spec)

        draft = await asyncio.to_thread(
            competitor_llm_provider.generate_draft,
            source_text=provider_request.source_text,
            material_type=provider_request.material_type,
            output_format=provider_request.output_format,
            mode="ai",
        )
        if draft is None:
            await record_event(
                operational_event_repository, module="materials",
                event_type="material_created_from_signal",
                success=False, workspace_id=principal.workspace_id,
                web_user_id=principal.web_user_id, severity=EventSeverity.INFO,
                error_code="draft_unavailable",
                metadata={"signal_id": interpretation_id, "action": request.action},
            )
            return {
                "error": "Не удалось подготовить материал. Попробуйте ещё раз.",
                "material": None,
            }

        sanitized = sanitize_draft_text(draft.text, disputed_claims=analysis.disputed_claims)
        artifact, version = await artifact_repository.create_artifact_with_initial_version(
            principal.workspace_id,
            artifact_type=spec.artifact_type,
            title=signal.title or "Материал по сигналу",
            content=sanitized,
            generation_note=f"Сигнал Radar: interpretation_id={interpretation_id}",
        )

        await record_event(
            operational_event_repository, module="materials",
            event_type="material_created_from_signal",
            success=True, workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
            metadata={
                "signal_id": interpretation_id, "action": request.action,
                "artifact_id": artifact.id,
            },
        )

        return {
            "material": _material_payload(artifact),
            "version": _version_payload(version),
            "origin": {
                "kind": "signal",
                "title": signal.title or "",
                "source_name": record.source_name or "",
                "created_at": record.created_at,
            },
        }

    except Exception:
        await record_event(
            operational_event_repository, module="materials",
            event_type="material_created_from_signal",
            success=False, workspace_id=principal.workspace_id,
            web_user_id=principal.web_user_id, severity=EventSeverity.ERROR,
            error_code="unhandled_exception",
            metadata={"signal_id": interpretation_id, "action": request.action},
        )
        return {"error": "Не удалось подготовить материал.", "material": None}


@app.get("/api/knowledge")
async def list_knowledge(principal: WebPrincipal = Depends(get_active_principal)):
    """Read-only browse of the shared Travel Advantage/MWR Life knowledge
    base - the same repository the Assistant already reads for chat answers
    (knowledge_service.retrieve()). Not workspace-scoped by design: this is
    shared reference data with no workspace_id column, exactly like the
    existing chat retrieval path. Still requires a valid session - it's
    part of the cabinet, not public.

    Tenant-gated on top of that (isolation audit fix): a workspace that
    isn't ta_affiliated (see _is_ta_affiliated()) gets an empty result
    here, never any of this TA/MWR content - independent workspaces must
    not see it just because it exists in the system.
    """
    try:
        if not await _is_ta_affiliated(principal.workspace_id):
            return {"sources": [], "items": []}

        sources = await knowledge_repository.get_sources()
        items = await knowledge_repository.list_items()
        sources_by_id = {source.id: source for source in sources}

        return {
            "sources": [
                {
                    "id": source.id,
                    "title": source.title,
                    "source_type": source.source_type,
                    "source_name": source.source_name,
                    "verification_status": source.verification_status,
                    "version": source.version,
                    "effective_date": source.effective_date,
                }
                for source in sources
            ],
            "items": [
                {
                    "stable_key": item.stable_key,
                    "category": item.category,
                    "title": item.title,
                    "content": item.content,
                    "tags": list(item.tags),
                    "source_title": (
                        sources_by_id[item.source_id].title
                        if item.source_id in sources_by_id else None
                    ),
                    "verification_status": (
                        sources_by_id[item.source_id].verification_status
                        if item.source_id in sources_by_id else None
                    ),
                }
                for item in items
            ],
        }

    except Exception:
        return {"error": "Не удалось загрузить базу знаний.", "sources": [], "items": []}


def _material_payload(artifact) -> dict:
    return {
        "id": artifact.id,
        "title": artifact.title,
        "artifact_type": artifact.artifact_type,
        "status": artifact.status,
        "created_at": artifact.created_at,
        "updated_at": artifact.updated_at,
    }


def _version_payload(version) -> dict | None:
    if version is None:
        return None
    return {
        "id": version.id,
        "version_number": version.version_number,
        "content": version.content,
        "generation_note": version.generation_note,
        "created_at": version.created_at,
    }


class MaterialUpdateRequest(BaseModel):
    content: str
    expected_version_id: int


@app.get("/api/materials")
async def list_materials(principal: WebPrincipal = Depends(get_active_principal)):
    """Read-only: реально сохранённые Artifact текущего workspace - тот же
    ArtifactRepository и та же логика, что и в Telegram «📚 Мои материалы»
    (app/handlers/materials.py)."""
    try:
        artifacts = await artifact_repository.list_artifacts(principal.workspace_id, limit=50)

        return {"materials": [_material_payload(artifact) for artifact in artifacts]}

    except Exception:
        return {"error": "Не удалось загрузить материалы.", "materials": []}


@app.get("/api/materials/{artifact_id}")
async def get_material(
    artifact_id: int, principal: WebPrincipal = Depends(get_active_principal),
):
    try:
        artifact = await artifact_repository.get_artifact(principal.workspace_id, artifact_id)

        if artifact is None:
            return {"error": "Материал не найден.", "material": None, "version": None}

        version = await artifact_repository.get_current_artifact_version(
            principal.workspace_id, artifact_id,
        )

        return {
            "material": _material_payload(artifact),
            "version": _version_payload(version),
        }

    except Exception:
        return {"error": "Не удалось загрузить материал.", "material": None, "version": None}


@app.put("/api/materials/{artifact_id}")
async def update_material(
    artifact_id: int, request: MaterialUpdateRequest,
    principal: WebPrincipal = Depends(require_csrf_and_subscription),
):
    """Редактирование материала = новая версия поверх той же модели
    Artifact/ArtifactVersion, ровно тот же паттерн, что и в Telegram Safety
    Layer edit-флоу (app/handlers/text_review.py:
    add_artifact_version_if_current) - никакого параллельного хранилища.

    expected_version_id обязателен и защищает от потери чужих правок:
    если текущая версия материала успела измениться между открытием и
    сохранением, запись не проходит и клиенту возвращается понятная ошибка
    вместо тихой перезаписи.
    """
    content = request.content.strip()

    if not content:
        return {"error": "Текст материала не должен быть пустым.", "material": None, "version": None}

    try:
        artifact = await artifact_repository.get_artifact(principal.workspace_id, artifact_id)

        if artifact is None:
            return {"error": "Материал не найден.", "material": None, "version": None}

        new_version = await artifact_repository.add_artifact_version_if_current(
            principal.workspace_id, artifact_id, request.expected_version_id, content,
            generation_note="Отредактировано в веб-кабинете",
        )

        if new_version is None:
            return {
                "error": "Материал изменился в другом месте. Обновите страницу и попробуйте снова.",
                "material": None,
                "version": None,
            }

        updated_artifact = await artifact_repository.get_artifact(
            principal.workspace_id, artifact_id,
        )
        await record_event(
            operational_event_repository, module="materials", event_type="update",
            success=True, workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
        )

        return {
            "material": _material_payload(updated_artifact),
            "version": _version_payload(new_version),
        }

    except Exception:
        return {"error": "Не удалось сохранить материал.", "material": None, "version": None}


@app.delete("/api/materials/{artifact_id}")
async def delete_material(
    artifact_id: int, principal: WebPrincipal = Depends(require_csrf_and_subscription),
):
    """Безопасное удаление: workspace isolation обеспечивается тем же
    механизмом, что и везде в ArtifactRepository (WHERE workspace_id=? AND
    id=? внутри delete_artifact - чужой artifact_id просто не совпадёт ни с
    одной строкой). Подтверждение - ответственность UI (двухшаговое
    подтверждение перед отправкой запроса), не самого эндпоинта."""
    try:
        deleted = await artifact_repository.delete_artifact(principal.workspace_id, artifact_id)

        if not deleted:
            return {"error": "Материал не найден.", "deleted": False}

        return {"deleted": True}

    except Exception:
        return {"error": "Не удалось удалить материал.", "deleted": False}


def _business_profile_payload(profile) -> dict | None:
    if profile is None:
        return None
    context = profile.context
    return {
        "business_name": profile.business_name,
        "business_type": profile.business_type,
        "short_description": profile.short_description,
        "profile_status": profile.profile_status,
        "ta_affiliated": profile.ta_affiliated,
        "specializations": list(context.specializations),
        "destinations": list(context.destinations),
        "region": context.region,
        "audiences": list(context.audiences),
        "tone": str(context.communication.get("tone") or ""),
        "public_contacts": dict(context.public_contacts),
        "verified_claims": [
            claim.text for claim in context.claims
            if claim.verification_status == "verified" and claim.text.strip()
        ],
    }


def _personal_style_payload(preferences) -> dict | None:
    if preferences is None:
        return None
    return {
        "style_description": preferences.style_description,
        "example_posts": list(preferences.example_posts),
        "avoid_phrases": list(preferences.avoid_phrases),
        "voice_sample": preferences.voice_sample,
    }


def _personal_style_prompt(preferences) -> str:
    """"Мой стиль / Голос бренда" for the freeform /api/chat path
    (app/chat_provider.py's flat personal_style string), built from the
    same Stage 3B1 fields (style_description, example_posts, avoid_phrases,
    voice_sample) that MaterialOrchestrationService already sends to
    Telegram material generation via GenerationSpec.personal_style - see
    app/services/material_orchestration.py's _personal_style_values().

    Byte-identical to plain style_description.strip() when nothing else is
    set, so this does not change behavior for the common case where only
    the ты/вы + tone sentence from onboarding exists (see
    tests/test_web_api_profile.py::test_update_personal_style_is_used_by_assistant_on_next_chat_call
    and tests/test_web_api_onboarding.py's equivalent chat-capture test).

    Each additional block carries its own "this is manner, not facts"
    warning inline, since chat_provider.generate() takes one flat string
    (not the structured [PERSONAL STYLE - DATA] section Content
    Factory/Telegram get) - old prices/dates/tour names/promos/hotels/
    countries/stats/specific offers pasted as a style example must never
    be read back as current information (see task notes: style sample is a
    source of MANNER, never of facts).
    """
    if preferences is None:
        return ""

    parts: list[str] = []

    style_description = preferences.style_description.strip()
    if style_description:
        parts.append(style_description)

    voice_sample = preferences.voice_sample.strip()
    if voice_sample:
        parts.append(
            "Образец текста пользователя для ориентира манеры речи (НЕ "
            "источник фактов — если в примере есть цены, даты, названия "
            "туров, акции, отели, страны, статистика или конкретные "
            "предложения, они могут быть устаревшими и не считаются "
            "актуальной информацией):\n" + voice_sample
        )

    if preferences.example_posts:
        examples = "\n".join(
            f"{index}. {text}"
            for index, text in enumerate(preferences.example_posts, start=1)
        )
        parts.append(
            "Примеры прошлых текстов пользователя для ориентира манеры речи "
            "(тот же принцип, что и выше: это не источник фактов, а образец "
            "стиля):\n" + examples
        )

    if preferences.avoid_phrases:
        parts.append(
            "Слова и обороты, которых нужно избегать: "
            + ", ".join(preferences.avoid_phrases)
        )

    return "\n\n".join(parts)


class BusinessProfileUpdateRequest(BaseModel):
    business_name: str
    business_type: str
    short_description: str
    specializations: list[str] = Field(default_factory=list)
    destinations: list[str] = Field(default_factory=list)
    region: str = ""
    audiences: list[str] = Field(default_factory=list)
    tone: str = ""


class PersonalStyleUpdateRequest(BaseModel):
    style_description: str
    avoid_phrases: list[str] = Field(default_factory=list)


class ExamplePostRequest(BaseModel):
    text: str


class VoiceSampleUpdateRequest(BaseModel):
    sample: str


# ── onboarding: controlled vocabulary for the "how should the Assistant
# talk to me" step - kept as short server-side allow-lists (not free text)
# so the resulting personal_style stays a clean, predictable sentence
# instead of arbitrary client-supplied prose.
ONBOARDING_TONE_LABELS: dict[str, str] = {
    "concise": "Кратко и по делу",
    "friendly": "Дружелюбно",
    "formal": "Делово",
    "expert": "Экспертно",
}
ONBOARDING_ADDRESS_LABELS: dict[str, str] = {
    "ty": "«ты»",
    "vy": "«вы»",
}


class OnboardingCompleteRequest(BaseModel):
    # who/business_name are required only for the owner/admin business-
    # profile branch below - a 'member' onboarding (no BusinessProfile
    # write access) never has them, so they default to "" rather than
    # being mandatory on the wire.
    who: str = ""
    business_name: str = ""
    short_description: str = ""
    specializations: list[str] = Field(default_factory=list)
    audiences: list[str] = Field(default_factory=list)
    region: str = ""
    tone: str
    address_form: str


_ONBOARDING_WHO_TO_BUSINESS_TYPE: dict[str, str] = {
    "ta_partner": "club_partner",
    "independent_agent": "independent_agent",
    "agency": "agency",
    "other": "other",
}


@app.get("/api/profile")
async def get_profile(principal: WebPrincipal = Depends(get_active_principal)):
    """Read-only: реальный BusinessProfile workspace + личный стиль текущего
    пользователя (WorkspaceUserPreferences) - те же данные, что уже
    показывает Telegram «⚙️ Профиль». workspace_memory сюда намеренно не
    попадает: это внутренний контекст Ассистента (см. /api/chat), а не
    пользовательское профильное поле - пользователю оно не показывается."""
    try:
        profile = await partner_repository.get_business_profile(principal.workspace_id)
        preferences = await partner_repository.get_user_preferences(
            principal.workspace_id, principal.telegram_user_id,
        )

        return {
            "business_profile": _business_profile_payload(profile),
            "personal_style": _personal_style_payload(preferences),
        }

    except Exception:
        return {
            "error": "Не удалось загрузить профиль.",
            "business_profile": None,
            "personal_style": None,
        }


@app.put("/api/profile/business")
async def update_business_profile_endpoint(
    request: BusinessProfileUpdateRequest,
    principal: WebPrincipal = Depends(require_csrf_and_subscription),
):
    """Правки бизнес-профиля идут через тот же BusinessProfileService и тот
    же revision-based optimistic concurrency, что и Telegram self-service
    «🏢 Профиль компании» (app/handlers/profile.py) - никакой параллельной
    модели профиля. resolve_workspace_context() - тот же access-control
    (owner/admin, active workspace), что использует Telegram; отсутствие
    активного membership для principal.telegram_user_id однозначно
    трактуется как запрет записи, а не как повод создать что-то новое.

    После сохранения /api/chat читает BusinessProfile заново на каждый
    запрос (без кеша) - новые значения сразу видны Ассистенту.
    """
    try:
        workspace_context = await partner_repository.resolve_workspace_context(
            principal.telegram_user_id,
        )
        if workspace_context is None or workspace_context.workspace_id != principal.workspace_id:
            return {"error": "Недостаточно прав для изменения профиля.", "business_profile": None}

        profile = await partner_repository.get_business_profile(principal.workspace_id)
        if profile is None:
            return {"error": "Профиль ещё не создан.", "business_profile": None}

        context_dict = business_context_to_dict(profile.context)
        context_dict["specializations"] = request.specializations
        context_dict["destinations"] = request.destinations
        context_dict["region"] = request.region
        context_dict["audiences"] = request.audiences
        context_dict["communication"]["tone"] = request.tone

        # ta_affiliated workspaces can't change business_type from the web
        # either - same rule as Telegram's on_profile_field_selected().
        business_type = profile.business_type if profile.ta_affiliated else request.business_type

        updated = await BusinessProfileService(partner_repository).update(
            workspace_context, profile.revision,
            business_name=request.business_name, business_type=business_type,
            short_description=request.short_description, context=context_dict,
        )

        return {"business_profile": _business_profile_payload(updated)}

    except BusinessProfileAccessError:
        return {"error": "Недостаточно прав для изменения профиля.", "business_profile": None}
    except (BusinessProfileValidationError, StaleBusinessProfileError) as exc:
        return {"error": str(exc), "business_profile": None}
    except Exception:
        return {"error": "Не удалось сохранить профиль.", "business_profile": None}


@app.put("/api/profile/style")
async def update_personal_style(
    request: PersonalStyleUpdateRequest,
    principal: WebPrincipal = Depends(require_csrf_and_subscription),
):
    """Личный стиль - через существующие PartnerRepository-методы
    (WorkspaceUserPreferences), те же, что Telegram «✍️ Мой стиль общения» /
    «🚫 Чего не использовать». Каждый метод сам перечитывает текущую запись
    и сохраняет остальные поля как есть, так что вызовы ниже не затирают
    example_posts. /api/chat читает эти же предпочтения на каждый запрос -
    сохранённый стиль сразу используется Ассистентом."""
    try:
        await partner_repository.set_user_style_description(
            principal.workspace_id, principal.telegram_user_id, request.style_description,
        )
        preferences = await partner_repository.set_user_avoid_phrases(
            principal.workspace_id, principal.telegram_user_id, request.avoid_phrases,
        )
        return {"personal_style": _personal_style_payload(preferences)}

    except Exception:
        return {"error": "Не удалось сохранить личный стиль.", "personal_style": None}


@app.post("/api/profile/style/examples")
async def add_personal_style_example(
    request: ExamplePostRequest,
    principal: WebPrincipal = Depends(require_csrf_and_subscription),
):
    text = request.text.strip()

    if not text:
        return {"error": "Текст примера не должен быть пустым.", "personal_style": None}

    try:
        preferences = await partner_repository.add_user_example_post(
            principal.workspace_id, principal.telegram_user_id, text,
        )
        return {"personal_style": _personal_style_payload(preferences)}

    except TooManyUserExamplesError as exc:
        return {"error": str(exc), "personal_style": None}
    except Exception:
        return {"error": "Не удалось сохранить пример текста.", "personal_style": None}


@app.delete("/api/profile/style/examples")
async def clear_personal_style_examples(
    principal: WebPrincipal = Depends(require_csrf_and_subscription),
):
    try:
        preferences = await partner_repository.clear_user_example_posts(
            principal.workspace_id, principal.telegram_user_id,
        )
        return {"personal_style": _personal_style_payload(preferences)}

    except Exception:
        return {"error": "Не удалось очистить примеры.", "personal_style": None}


@app.put("/api/profile/voice-sample")
async def update_voice_sample(
    request: VoiceSampleUpdateRequest,
    principal: WebPrincipal = Depends(require_csrf_and_subscription),
):
    """"Мой стиль / Голос бренда" - один цельный вставленный образец текста
    (пост, сообщение клиенту, несколько абзацев), в отличие от
    /api/profile/style/examples (несколько отдельных примеров, по одному).
    Тот же WorkspaceUserPreferences, тот же Telegram/Web workspace - см.
    app.services.user_style.UserStyleService.set_voice_sample(). Raw sample
    text is never logged (no record_event call here), matching every other
    personal-style endpoint in this file."""
    try:
        preferences = await partner_repository.set_user_voice_sample(
            principal.workspace_id, principal.telegram_user_id, request.sample,
        )
        return {"personal_style": _personal_style_payload(preferences)}

    except VoiceSampleTooLongError as exc:
        return {"error": str(exc), "personal_style": None}
    except Exception:
        return {"error": "Не удалось сохранить стиль.", "personal_style": None}


@app.delete("/api/profile/voice-sample")
async def clear_voice_sample(
    principal: WebPrincipal = Depends(require_csrf_and_subscription),
):
    try:
        preferences = await partner_repository.set_user_voice_sample(
            principal.workspace_id, principal.telegram_user_id, "",
        )
        return {"personal_style": _personal_style_payload(preferences)}

    except Exception:
        return {"error": "Не удалось очистить стиль.", "personal_style": None}


@app.post("/api/onboarding/complete")
async def complete_onboarding(
    request: OnboardingCompleteRequest,
    principal: WebPrincipal = Depends(require_csrf_and_subscription),
):
    """First-run setup for a new web binding - writes into the SAME
    BusinessProfile / WorkspaceUserPreferences the "Профиль" tab already
    edits (no parallel onboarding-data model), then flips the
    binding-scoped onboarding flag so "/" stops redirecting here.

    Role-aware (see onboarding.html's OWNER_STEPS/MEMBER_STEPS): only
    owner/admin ever attempt a BusinessProfile write here, matching the
    exact same rule PUT /api/profile/business already enforces - this
    endpoint never gets a bypass around that. A 'member' binding's request
    has no business fields to begin with (the UI never collects them), so
    who/business_name validation only applies inside the owner/admin
    branch below - it must not reject a member's (business-field-less)
    request. Personal style (tone/address form) has no role restriction
    (see UserStyleService) and always saves for everyone. Either way,
    onboarding completion itself always succeeds once CSRF+session are
    valid - a workspace permission edge case must not trap a new user on
    this page.
    """
    tone_label = ONBOARDING_TONE_LABELS.get(request.tone)
    address_label = ONBOARDING_ADDRESS_LABELS.get(request.address_form)
    if tone_label is None or address_label is None:
        return {"error": "Недопустимые значения стиля общения."}

    try:
        workspace_context = await partner_repository.resolve_workspace_context(
            principal.telegram_user_id,
        )
    except Exception:
        workspace_context = None

    can_write_business_profile = (
        workspace_context is not None
        and workspace_context.workspace_id == principal.workspace_id
        and workspace_context.role in {"owner", "admin"}
    )

    business_profile_saved = False
    if can_write_business_profile:
        business_type = _ONBOARDING_WHO_TO_BUSINESS_TYPE.get(request.who)
        if business_type is None:
            return {"error": "Недопустимое значение «кто вы»."}

        business_name = request.business_name.strip()
        if not business_name:
            return {"error": "Название/имя обязательно."}

        try:
            profile = await partner_repository.get_business_profile(principal.workspace_id)
            if profile is not None:
                context_dict = business_context_to_dict(profile.context)
                context_dict["specializations"] = request.specializations
                context_dict["audiences"] = request.audiences
                context_dict["region"] = request.region
                context_dict["communication"]["tone"] = tone_label

                # Same ta_affiliated lock as PUT /api/profile/business - an
                # already-TA-affiliated workspace can't have its type
                # changed from the web, onboarding included.
                effective_type = (
                    profile.business_type if profile.ta_affiliated else business_type
                )

                await BusinessProfileService(partner_repository).update(
                    workspace_context, profile.revision,
                    business_name=business_name, business_type=effective_type,
                    short_description=request.short_description.strip(),
                    context=context_dict,
                )
                business_profile_saved = True
        except (
            BusinessProfileAccessError,
            BusinessProfileValidationError,
            StaleBusinessProfileError,
        ):
            # Best-effort: a permission/concurrency hiccup on the shared
            # business profile must not block this user from finishing
            # their own onboarding (personal style + completion below).
            business_profile_saved = False

    style_text = f"{tone_label}. Обращайся на {address_label}."
    await partner_repository.set_user_style_description(
        principal.workspace_id, principal.telegram_user_id, style_text,
    )

    await web_auth_repository.mark_onboarding_completed(principal.binding_id)
    await record_event(
        operational_event_repository, module="onboarding", event_type="complete",
        success=True, workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
        metadata={"business_profile_saved": business_profile_saved},
    )

    profile = await partner_repository.get_business_profile(principal.workspace_id)
    preferences = await partner_repository.get_user_preferences(
        principal.workspace_id, principal.telegram_user_id,
    )

    return {
        "business_profile": _business_profile_payload(profile),
        "personal_style": _personal_style_payload(preferences),
        "business_profile_saved": business_profile_saved,
        "onboarding_completed": True,
    }


def _render_markdown(text: str) -> str:
    return markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists"],
    )


def _conversation_payload(conversation) -> dict:
    return {
        "id": conversation.id,
        "title": conversation.title,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
    }


def _attachment_payload(attachment: WebAttachment) -> dict:
    return {
        "id": attachment.public_id,
        "filename": attachment.original_filename,
        "content_type": attachment.content_type,
        "kind": attachment.kind,
        "size_bytes": attachment.size_bytes,
    }


def _message_payload(message, attachments: list[WebAttachment] = ()) -> dict:
    payload = {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at,
        "attachments": [_attachment_payload(a) for a in attachments],
    }
    # HTML is never stored (see app.domain.web_conversation) - it's rendered
    # here on read, through the exact same markdown.markdown() call /api/chat
    # uses for a live answer, so restored messages go through the same safe
    # rendering path as new ones.
    if message.role == ROLE_ASSISTANT:
        payload["content_html"] = _render_markdown(message.content)
    return payload


def _attachment_only_placeholder(count: int) -> str:
    """Stored as the message's text content when the user sends a message
    with no typed text (file-only). Keeps web_conversation_messages'
    ``CHECK (length(trim(content)) > 0)`` intact (no schema migration
    needed) and gives history something meaningful to show even before
    attachment chips render. The composer's submitMessage() in chat.html
    renders this exact same string client-side for the just-sent message,
    so live send and a later reload look identical - keep both in sync."""
    return "📎 Вложение" if count == 1 else f"📎 Вложения ({count})"


async def _read_upload_capped(upload: UploadFile, max_bytes: int) -> bytes | None:
    """Streams the upload in chunks, aborting as soon as max_bytes is
    exceeded - never trusts a declared Content-Length. Returns None on
    overflow (caller turns that into a human-readable error)."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(256 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/api/attachments")
async def upload_attachments(
    conversation_id: int = Form(...),
    files: list[UploadFile] = File(...),
    principal: WebPrincipal = Depends(require_csrf_and_subscription),
):
    """Uploads one or more files for a conversation's NEXT message - see
    /api/chat's attachment_ids for how these get bound to an actual
    message. Files are validated then written to
    data/web_uploads/<workspace_id>/<conversation_id>/<random>.<ext> -
    never under the client's original filename (see AttachmentStorage /
    WebAttachmentRepository). workspace_id/telegram_user_id come only from
    ``principal`` - never from the form body - so a file can never land in
    another workspace's conversation.

    Validates every file BEFORE writing any of them to disk: a batch that
    fails validation on file 3 of 5 leaves nothing behind from files 1-2
    either, so a rejected upload never leaves partial orphans.
    """
    try:
        conversation = await web_conversation_repository.get_conversation(
            principal.workspace_id, principal.telegram_user_id, conversation_id,
        )
        if conversation is None:
            return {"error": "Диалог не найден или недоступен.", "attachments": []}

        if not files:
            return {"error": "Файл не выбран.", "attachments": []}
        if len(files) > MAX_FILES_PER_UPLOAD:
            return {
                "error": f"Слишком много файлов за раз (максимум {MAX_FILES_PER_UPLOAD}).",
                "attachments": [],
            }

        validated: list[tuple[str, bytes, AttachmentSniff]] = []
        total_bytes = 0

        for upload in files:
            display_name = sanitize_display_filename(upload.filename or "file")
            data = await _read_upload_capped(upload, MAX_FILE_SIZE_BYTES)
            if data is None:
                return {
                    "error": (
                        f"Файл «{display_name}» больше "
                        f"{MAX_FILE_SIZE_BYTES // (1024 * 1024)} МБ."
                    ),
                    "attachments": [],
                }

            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_UPLOAD_BYTES:
                return {
                    "error": "Суммарный размер вложений слишком большой.",
                    "attachments": [],
                }

            try:
                sniff = validate_attachment(display_filename=display_name, data=data)
            except AttachmentValidationError as exc:
                await record_event(
                    operational_event_repository, module="attachments", event_type="upload",
                    success=False, workspace_id=principal.workspace_id,
                    web_user_id=principal.web_user_id, severity=EventSeverity.INFO,
                    error_code="validation_rejected", safe_message="attachment failed validation",
                )
                return {"error": str(exc), "attachments": []}

            validated.append((display_name, data, sniff))

        saved: list[WebAttachment] = []
        for display_name, data, sniff in validated:
            stored_filename = f"{secrets.token_hex(16)}.{sniff.extension}"
            await attachment_storage.write(
                principal.workspace_id, conversation_id, stored_filename, data,
            )
            try:
                record = await web_attachment_repository.create_pending(
                    workspace_id=principal.workspace_id,
                    telegram_user_id=principal.telegram_user_id,
                    conversation_id=conversation_id,
                    original_filename=display_name,
                    stored_filename=stored_filename,
                    content_type=sniff.content_type,
                    kind=sniff.kind,
                    size_bytes=len(data),
                )
            except Exception:
                attachment_storage.delete(
                    principal.workspace_id, conversation_id, stored_filename,
                )
                raise
            saved.append(record)

        await record_event(
            operational_event_repository, module="attachments", event_type="upload",
            success=True, workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
            metadata={"count": len(saved)},
        )
        return {"attachments": [_attachment_payload(a) for a in saved]}

    except Exception:
        await record_event(
            operational_event_repository, module="attachments", event_type="upload",
            success=False, workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
            severity=EventSeverity.ERROR, error_code="unhandled_exception",
            safe_message="attachment upload raised",
        )
        return {"error": "Не удалось загрузить файл. Попробуйте ещё раз.", "attachments": []}


@app.delete("/api/attachments/{public_id}")
async def delete_pending_attachment(
    public_id: str, principal: WebPrincipal = Depends(require_csrf_and_subscription),
):
    """Removing a chip in the composer before sending - only ever deletes
    a still-pending attachment (see WebAttachmentRepository.delete_pending);
    an attachment that's already part of sent message history is not
    touched, whether or not this endpoint is even called."""
    try:
        attachment = await web_attachment_repository.delete_pending(
            principal.workspace_id, principal.telegram_user_id, public_id,
        )
        if attachment is not None:
            attachment_storage.delete(
                attachment.workspace_id, attachment.conversation_id, attachment.stored_filename,
            )
        return {"deleted": attachment is not None}

    except Exception:
        return {"deleted": False}


@app.get("/api/attachments/{public_id}/content")
async def get_attachment_content(
    public_id: str, principal: WebPrincipal = Depends(get_active_principal),
):
    """Serves raw bytes for the composer/history image thumbnail preview
    only (v1 scope - see the task notes: no general-purpose file download
    endpoint, no "Files" section). ``public_id`` is a high-entropy random
    token (never the sequential row id), and every request is still
    ownership-checked against the session - not a public/guessable URL.
    A generic 404 covers "doesn't exist", "belongs to another workspace",
    and "not an image" alike, so no case leaks more than another."""
    try:
        attachment = await web_attachment_repository.get_for_workspace(
            principal.workspace_id, principal.telegram_user_id, public_id,
        )
        if attachment is None or attachment.kind != "image":
            raise HTTPException(status_code=404, detail="Файл не найден.")

        data = await attachment_storage.read(
            attachment.workspace_id, attachment.conversation_id, attachment.stored_filename,
        )
        return Response(content=data, media_type=attachment.content_type)

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="Файл не найден.")


@app.post("/api/conversations")
async def create_conversation(principal: WebPrincipal = Depends(require_csrf_and_subscription)):
    """Создаёт новый диалог Ассистента - пустой, с title по умолчанию.
    Реальный title подставится после первого сообщения (см. /api/chat)."""
    try:
        conversation = await web_conversation_repository.create_conversation(
            principal.workspace_id, principal.telegram_user_id,
        )
        return {"conversation": _conversation_payload(conversation)}

    except Exception:
        return {"error": "Не удалось создать диалог.", "conversation": None}


@app.get("/api/conversations")
async def list_conversations(principal: WebPrincipal = Depends(get_active_principal)):
    """Список диалогов текущего workspace/user, свежие сверху (по последней
    активности) - для раздела «История» в sidebar."""
    try:
        conversations = await web_conversation_repository.list_conversations(
            principal.workspace_id, principal.telegram_user_id,
        )
        return {"conversations": [_conversation_payload(c) for c in conversations]}

    except Exception:
        return {"error": "Не удалось загрузить историю диалогов.", "conversations": []}


@app.get("/api/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: int, principal: WebPrincipal = Depends(get_active_principal),
):
    """Сообщения одного диалога, в хронологическом порядке. Строго
    workspace + user scoped: get_conversation() возвращает None для чужого
    или несуществующего conversation_id, что здесь трактуется как «не
    найден», без утечки чужих данных."""
    try:
        conversation = await web_conversation_repository.get_conversation(
            principal.workspace_id, principal.telegram_user_id, conversation_id,
        )

        if conversation is None:
            return {"error": "Диалог не найден.", "conversation": None, "messages": []}

        messages = await web_conversation_repository.list_messages(
            principal.workspace_id, principal.telegram_user_id, conversation_id,
        )
        attachments_by_message = await web_attachment_repository.list_for_conversation_messages(
            principal.workspace_id, principal.telegram_user_id, conversation_id,
        )

        return {
            "conversation": _conversation_payload(conversation),
            "messages": [
                _message_payload(item, attachments_by_message.get(item.id, []))
                for item in messages
            ],
        }

    except Exception:
        return {"error": "Не удалось загрузить сообщения диалога.", "conversation": None, "messages": []}


class FeedbackSubmitRequest(BaseModel):
    conversation_id: int
    message_id: int
    rating: str
    reason: str | None = None
    comment: str = ""


@app.post("/api/feedback")
async def submit_feedback(
    request: FeedbackSubmitRequest, principal: WebPrincipal = Depends(require_csrf),
):
    """👍/👎 on a single assistant message - never gated by subscription
    (require_csrf, not require_csrf_and_subscription): giving feedback on
    an answer you already received should not itself require an active
    subscription. message_id is verified to actually belong to THIS
    session's own (workspace_id, telegram_user_id) conversation before
    anything is stored - never trusted at face value."""
    if request.rating not in {"up", "down"}:
        return {"error": "Недопустимая оценка.", "feedback": None}
    if request.reason is not None and request.reason not in FEEDBACK_REASON_CODES:
        return {"error": "Недопустимая причина.", "feedback": None}

    try:
        messages = await web_conversation_repository.list_messages(
            principal.workspace_id, principal.telegram_user_id, request.conversation_id,
            limit=500,
        )
        message = next((m for m in messages if m.id == request.message_id), None)
        if message is None or message.role != ROLE_ASSISTANT:
            return {"error": "Сообщение не найдено.", "feedback": None}

        feedback = await feedback_repository.submit(
            workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
            conversation_id=request.conversation_id, message_id=request.message_id,
            rating=FeedbackRating(request.rating), reason=request.reason,
            comment=request.comment,
        )
        await record_event(
            operational_event_repository, module="feedback", event_type="submit",
            success=True, workspace_id=principal.workspace_id, web_user_id=principal.web_user_id,
            metadata={"rating": request.rating, "reason": request.reason},
        )
        return {
            "feedback": {
                "id": feedback.id, "rating": feedback.rating.value,
                "reason": feedback.reason, "comment": feedback.comment,
            }
        }
    except Exception:
        return {"error": "Не удалось сохранить отзыв.", "feedback": None}


async def _load_provider_attachments(
    attachments: list[WebAttachment],
) -> list[AttachmentInput]:
    """Reads each attachment's bytes off disk and shapes them for
    OpenAIChatProvider.generate() - base64 for images/PDF, decoded (and
    length-capped) text for txt/md. Only ever called with THIS turn's
    attachments (see chat())."""
    items: list[AttachmentInput] = []
    for attachment in attachments:
        data = await attachment_storage.read(
            attachment.workspace_id, attachment.conversation_id, attachment.stored_filename,
        )
        if attachment.kind == "text":
            text = data.decode("utf-8", errors="replace")
            if len(text) > MAX_TEXT_FILE_CHARS:
                text = text[:MAX_TEXT_FILE_CHARS] + "\n… [файл обрезан]"
            items.append(AttachmentInput(
                kind="text", content_type=attachment.content_type,
                filename=attachment.original_filename, text=text,
            ))
        else:
            items.append(AttachmentInput(
                kind=attachment.kind, content_type=attachment.content_type,
                filename=attachment.original_filename,
                data_base64=base64.b64encode(data).decode("ascii"),
            ))
    return items


@app.post("/api/chat")
async def chat(request: ChatRequest, principal: WebPrincipal = Depends(require_csrf_and_subscription)):
    chat_started_at = time.monotonic()
    message = request.message.strip()
    attachment_ids = request.attachment_ids

    if len(attachment_ids) > MAX_FILES_PER_UPLOAD:
        return {"error": f"Слишком много вложений в одном сообщении (максимум {MAX_FILES_PER_UPLOAD})."}

    if not message and not attachment_ids:
        return {"error": "Введите вопрос или прикрепите файл."}

    try:
        conversation = await web_conversation_repository.get_conversation(
            principal.workspace_id, principal.telegram_user_id, request.conversation_id,
        )
        if conversation is None:
            return {"error": "Диалог не найден или недоступен."}

        # Каждый public_id должен быть pending-вложением ЭТОГО же
        # workspace/user/conversation - иначе отказываем всему сообщению
        # целиком (fail closed), а не молча пропускаем часть файлов.
        resolved_attachments: list[WebAttachment] = []
        for public_id in attachment_ids:
            attachment = await web_attachment_repository.get_pending_for_conversation(
                principal.workspace_id, principal.telegram_user_id,
                request.conversation_id, public_id,
            )
            if attachment is None:
                return {
                    "error": "Не удалось прикрепить файл — возможно, он уже "
                    "отправлен или недоступен."
                }
            resolved_attachments.append(attachment)

        # История этого диалога, взятая с сервера (а не от клиента) - до
        # добавления текущего сообщения. Источник истины для generate() и
        # для решения "это первое сообщение диалога?" (title).
        prior_messages = await web_conversation_repository.list_messages(
            principal.workspace_id, principal.telegram_user_id, request.conversation_id,
        )
        is_first_message = not prior_messages
        history = [
            {"role": item.role, "content": item.content} for item in prior_messages
        ]

        stored_content = message or _attachment_only_placeholder(len(resolved_attachments))

        saved_user_message = await web_conversation_repository.add_message(
            principal.workspace_id, principal.telegram_user_id, request.conversation_id,
            ROLE_USER, stored_content,
        )
        if saved_user_message is None:
            return {"error": "Диалог не найден или недоступен."}

        if resolved_attachments:
            await web_attachment_repository.attach_to_message(
                principal.workspace_id, principal.telegram_user_id, request.conversation_id,
                [a.public_id for a in resolved_attachments], saved_user_message.id,
            )

        if is_first_message:
            title_source = message or (
                resolved_attachments[0].original_filename if resolved_attachments else ""
            )
            await web_conversation_repository.set_conversation_title(
                principal.workspace_id, principal.telegram_user_id, request.conversation_id,
                derive_conversation_title(title_source),
            )

        recent_user_context = [
            item["content"]
            for item in history[-6:]
            if item["role"] == "user"
        ]

        retrieval_query = "\n".join(
            [
                *recent_user_context,
                message,
            ]
        )

        business_profile = await partner_repository.get_business_profile(principal.workspace_id)
        # Authoritative + fail-closed: BusinessProfile.ta_affiliated only,
        # no profile means not affiliated (same rule as _is_ta_affiliated()
        # above - kept inline here since business_profile is already
        # fetched for _business_profile_context below, no need for a
        # second query).
        ta_affiliated = business_profile is not None and business_profile.ta_affiliated

        bundle = (
            await knowledge_service.retrieve(retrieval_query)
            if ta_affiliated else _EMPTY_KNOWLEDGE_BUNDLE
        )
        knowledge_context = _knowledge_context(bundle)

        if business_profile is not None:
            knowledge_context = "\n\n".join(
                part for part in (
                    knowledge_context,
                    _business_profile_context(business_profile),
                )
                if part
            )

        competitor = await _requested_competitor(principal.workspace_id, message)

        if competitor is not None:
            try:
                intelligence = await competitor_intelligence_service.analyze(
                    competitor, ta_affiliated=ta_affiliated,
                )

                await competitor_repository.save_intelligence(
                    principal.workspace_id,
                    intelligence,
                )

                knowledge_context = "\n\n".join(
                    part for part in (
                        knowledge_context,
                        _competitor_context(intelligence),
                    )
                    if part
                )
                await record_event(
                    operational_event_repository, module="competitors", event_type="analyze",
                    success=True, workspace_id=principal.workspace_id,
                    web_user_id=principal.web_user_id,
                    metadata={"data_origin": intelligence.data_origin},
                )

            except CompetitorIntelligenceUnavailable as exc:
                knowledge_context = "\n\n".join(
                    part for part in (
                        knowledge_context,
                        "=== COMPETITOR INTELLIGENCE ===\n"
                        f"Свежий анализ недоступен: {exc}. "
                        "Не выдавай общие знания модели за свежие данные.",
                    )
                    if part
                )
                await record_event(
                    operational_event_repository, module="competitors", event_type="analyze",
                    success=False, workspace_id=principal.workspace_id,
                    web_user_id=principal.web_user_id, severity=EventSeverity.INFO,
                    error_code="unavailable", safe_message="no fresh source or matching signal",
                )

        preferences = await partner_repository.get_user_preferences(
            principal.workspace_id,
            principal.telegram_user_id,
        )

        personal_style = _personal_style_prompt(preferences)

        memory_record = await workspace_memory_repository.get(principal.workspace_id)
        workspace_memory_text = (
            memory_record.summary.strip() if memory_record is not None else ""
        )

        if len(workspace_memory_text) > MAX_WORKSPACE_MEMORY_CHARS:
            workspace_memory_text = (
                workspace_memory_text[:MAX_WORKSPACE_MEMORY_CHARS] + "…"
            )

        # THIS turn's attachments only - see AttachmentInput/generate()'s
        # docstring in app/chat_provider.py for why prior turns' files are
        # never resent.
        provider_attachments = await _load_provider_attachments(resolved_attachments)

        try:
            chat_result = await asyncio.to_thread(
                chat_provider.generate,
                message=message,
                history=history[-12:],
                knowledge_context=knowledge_context,
                personal_style=personal_style,
                workspace_memory=workspace_memory_text,
                attachments=provider_attachments,
            )
        except Exception:
            await record_llm_call(
                usage_ledger_repository,
                workspace_id=principal.workspace_id,
                telegram_user_id=principal.telegram_user_id,
                module="web_chat",
                provider="openai",
                model="gpt-5.6-terra",
                usage=None,
                status=UsageStatus.FAILURE,
            )
            await record_event(
                operational_event_repository, module="chat", event_type="message",
                success=False, workspace_id=principal.workspace_id,
                web_user_id=principal.web_user_id, severity=EventSeverity.ERROR,
                latency_ms=int((time.monotonic() - chat_started_at) * 1000),
                error_code="provider_error", safe_message="chat provider call failed",
                metadata={"provider": "openai", "model": "gpt-5.6-terra"},
            )
            raise

        await record_llm_call(
            usage_ledger_repository,
            workspace_id=principal.workspace_id,
            telegram_user_id=principal.telegram_user_id,
            module="web_chat",
            provider="openai",
            model="gpt-5.6-terra",
            usage=chat_result.usage,
            status=UsageStatus.SUCCESS,
        )
        await record_event(
            operational_event_repository, module="chat", event_type="message",
            success=True, workspace_id=principal.workspace_id,
            web_user_id=principal.web_user_id,
            latency_ms=int((time.monotonic() - chat_started_at) * 1000),
            metadata={"provider": "openai", "model": "gpt-5.6-terra"},
        )

        answer = chat_result.text

        clean_answer = (
            answer
            .replace("&amp;#x20;", " ")
            .replace("&amp;#32;", " ")
            .replace("&amp;nbsp;", " ")
            .replace("&#x20;", " ")
            .replace("&#32;", " ")
            .replace("&nbsp;", " ")
            .replace("\u00a0", " ")
        )

        answer_html = _render_markdown(clean_answer)

        # Оригинальный текст ответа, не HTML - HTML восстанавливается тем же
        # markdown.markdown() при чтении истории (GET .../messages),
        # никогда не хранится как источник истины.
        saved_assistant_message = await web_conversation_repository.add_message(
            principal.workspace_id, principal.telegram_user_id, request.conversation_id,
            ROLE_ASSISTANT, clean_answer,
        )

        updated_conversation = await web_conversation_repository.get_conversation(
            principal.workspace_id, principal.telegram_user_id, request.conversation_id,
        )

        return {
            "answer": clean_answer,
            "answer_html": answer_html,
            "message_id": saved_assistant_message.id if saved_assistant_message is not None else None,
            "model": "gpt-5.6-terra",
            "knowledge_used": bool(knowledge_context),
            "knowledge_sources": [
                {
                    "title": source.title,
                    "reference": source.source_reference,
                    "verification_status": source.verification_status,
                }
                for source in bundle.sources[:8]
            ],
            "conversation": (
                _conversation_payload(updated_conversation)
                if updated_conversation is not None else None
            ),
        }

    except Exception:
        await record_event(
            operational_event_repository, module="chat", event_type="message",
            success=False, workspace_id=principal.workspace_id,
            web_user_id=principal.web_user_id, severity=EventSeverity.ERROR,
            latency_ms=int((time.monotonic() - chat_started_at) * 1000),
            error_code="unhandled_exception", safe_message="chat endpoint raised",
        )
        return {
            "error": "Не удалось получить ответ AI. Попробуйте ещё раз."
        }


async def _valid_session_context(request: Request):
    """None if unauthenticated/expired/revoked/disabled - same checks as
    get_current_principal(), but for page routes that need to branch on
    "logged in or not" without raising a 401 (this serves HTML, not JSON)."""
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        return None
    ctx = await web_auth_repository.get_session_context(hash_token(raw_token))
    if (
        ctx is None
        or ctx.revoked_at is not None
        or ctx.expires_at <= _now_iso()
        or ctx.user_status != "active"
    ):
        return None
    return ctx


async def _has_valid_session(request: Request) -> bool:
    return await _valid_session_context(request) is not None


async def _onboarding_pending(binding_id: int) -> bool:
    binding = await web_auth_repository.get_binding_by_id(binding_id)
    return binding is not None and binding.onboarding_completed_at is None


async def _subscription_inactive(ctx) -> bool:
    state = await subscription_repository.resolve_access_state(ctx.workspace_id)
    return not is_access_granted(state)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Unauthenticated visitors never see the cabinet - a plain redirect
    to /login, not a 401 (this is a browser page, not a JSON API call).
    A first-time (or otherwise not-yet-onboarded) binding is sent to
    /onboarding instead of the cabinet - see _onboarding_pending(). An
    expired/past_due/suspended workspace is sent to
    /subscription-inactive instead - checked before onboarding, since
    there's no point collecting onboarding answers for a workspace that
    can't use the product yet."""
    ctx = await _valid_session_context(request)
    if ctx is None:
        return RedirectResponse(url="/login", status_code=303)
    if await _subscription_inactive(ctx):
        return RedirectResponse(url="/subscription-inactive", status_code=303)
    if await _onboarding_pending(ctx.binding_id):
        return RedirectResponse(url="/onboarding", status_code=303)
    return Path(
        "app/templates/chat.html"
    ).read_text(encoding="utf-8")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if await _has_valid_session(request):
        return RedirectResponse(url="/", status_code=303)
    return Path(
        "app/templates/login.html"
    ).read_text(encoding="utf-8")


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    if await _has_valid_session(request):
        return RedirectResponse(url="/", status_code=303)
    return Path(
        "app/templates/register.html"
    ).read_text(encoding="utf-8")


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request):
    """Reachable both for a brand-new binding (redirected here by "/") and
    by a completed one opening the URL by hand - either way we just serve
    the page; onboarding.html itself loads current values from
    /api/profile and always allows saving again (see task: reopening
    /onboarding after completion shows current values, not a hard block).
    An expired/past_due/suspended workspace is redirected to
    /subscription-inactive instead, same rule as "/" - onboarding writes
    through the same subscription-gated product endpoints
    (/api/onboarding/complete), so there's nothing useful to do here
    without an active subscription."""
    ctx = await _valid_session_context(request)
    if ctx is None:
        return RedirectResponse(url="/login", status_code=303)
    if await _subscription_inactive(ctx):
        return RedirectResponse(url="/subscription-inactive", status_code=303)
    return Path(
        "app/templates/onboarding.html"
    ).read_text(encoding="utf-8")


@app.get("/subscription-inactive", response_class=HTMLResponse)
async def subscription_inactive_page(request: Request):
    """The "подписка неактивна" state - reachable only with a valid
    session; redirects straight back to "/" once the subscription is
    granted again (nothing to show here in that case), so a stale
    bookmark/tab never traps an otherwise-active user on this page."""
    ctx = await _valid_session_context(request)
    if ctx is None:
        return RedirectResponse(url="/login", status_code=303)
    if not await _subscription_inactive(ctx):
        return RedirectResponse(url="/", status_code=303)
    return Path(
        "app/templates/subscription_inactive.html"
    ).read_text(encoding="utf-8")


@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    """The built-in help/instructions page - reachable in EVERY subscription
    state (no subscription, trial, beta, active, expired, suspended), same
    rule as /billing: only a valid session is required, never a subscription
    check. This is deliberate - a user who can't pay or whose access lapsed
    is exactly who most needs to find billing/support instructions, and an
    expired/suspended workspace must never be locked out of self-serve help.
    The page itself is static content and links out to /billing for
    payment - it never calls any subscription-gated product endpoint, so it
    can't be used to bypass the paywall."""
    ctx = await _valid_session_context(request)
    if ctx is None:
        return RedirectResponse(url="/login", status_code=303)
    return Path(
        "app/templates/help.html"
    ).read_text(encoding="utf-8")


@app.get("/billing", response_class=HTMLResponse)
async def billing_page(request: Request):
    """The "Подписка" page - reachable with ANY subscription state,
    including expired/past_due/suspended (unlike "/" and "/onboarding")
    - this is exactly where a user in that state needs to land to pay.
    Only a valid session is required, never a subscription check."""
    ctx = await _valid_session_context(request)
    if ctx is None:
        return RedirectResponse(url="/login", status_code=303)
    return Path(
        "app/templates/billing.html"
    ).read_text(encoding="utf-8")


@app.get("/billing/success", response_class=HTMLResponse)
async def billing_success_page(request: Request):
    """RoboKassa's SuccessURL redirect target - NOT the source of truth
    (see POST /api/billing/robokassa/result's docstring). This page never
    activates anything; its script calls GET /api/billing/orders/{id} to
    read the CURRENT, already-server-confirmed state, scoped to the
    logged-in session's own workspace."""
    ctx = await _valid_session_context(request)
    if ctx is None:
        return RedirectResponse(url="/login", status_code=303)
    return Path(
        "app/templates/billing_success.html"
    ).read_text(encoding="utf-8")


@app.get("/billing/fail", response_class=HTMLResponse)
async def billing_fail_page(request: Request):
    """RoboKassa's FailURL redirect target - purely informational, never
    mutates any order/subscription state. The base RoboKassa scheme's Fail
    redirect isn't reliably signed, so nothing from this request is ever
    trusted for a write - see the report's security notes."""
    ctx = await _valid_session_context(request)
    if ctx is None:
        return RedirectResponse(url="/login", status_code=303)
    return Path(
        "app/templates/billing_fail.html"
    ).read_text(encoding="utf-8")


# ── Beta Control Center pages ────────────────────────────────────────
#
# Same require_platform_admin gate as the JSON API, applied by hand here
# since these are plain HTMLResponse routes, not Depends()-based: no
# session -> /login (same as every other page route); session but not a
# platform admin -> 404, never a distinct "forbidden" page, so a regular
# authenticated user gets no signal that /admin exists at all.

async def _require_admin_page_session(request: Request):
    ctx = await _valid_session_context(request)
    if ctx is None:
        return RedirectResponse(url="/login", status_code=303)
    if not await _is_platform_admin(ctx.email):
        raise HTTPException(status_code=404)
    return None


_ADMIN_PAGES = {
    "/admin": "admin_dashboard.html",
    "/admin/workspaces": "admin_workspaces.html",
    "/admin/billing": "admin_billing.html",
    "/admin/errors": "admin_errors.html",
    "/admin/activity": "admin_activity.html",
    "/admin/feedback": "admin_feedback.html",
    "/admin/health": "admin_health.html",
    "/admin/audit-log": "admin_audit_log.html",
}


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard_page(request: Request):
    redirect = await _require_admin_page_session(request)
    if redirect is not None:
        return redirect
    return Path(f"app/templates/{_ADMIN_PAGES['/admin']}").read_text(encoding="utf-8")


@app.get("/admin/workspaces", response_class=HTMLResponse)
async def admin_workspaces_page(request: Request):
    redirect = await _require_admin_page_session(request)
    if redirect is not None:
        return redirect
    return Path(f"app/templates/{_ADMIN_PAGES['/admin/workspaces']}").read_text(encoding="utf-8")


@app.get("/admin/workspaces/{workspace_id}", response_class=HTMLResponse)
async def admin_workspace_detail_page(workspace_id: int, request: Request):
    redirect = await _require_admin_page_session(request)
    if redirect is not None:
        return redirect
    return Path("app/templates/admin_workspace_detail.html").read_text(encoding="utf-8")


@app.get("/admin/billing", response_class=HTMLResponse)
async def admin_billing_page(request: Request):
    redirect = await _require_admin_page_session(request)
    if redirect is not None:
        return redirect
    return Path(f"app/templates/{_ADMIN_PAGES['/admin/billing']}").read_text(encoding="utf-8")


@app.get("/admin/errors", response_class=HTMLResponse)
async def admin_errors_page(request: Request):
    redirect = await _require_admin_page_session(request)
    if redirect is not None:
        return redirect
    return Path(f"app/templates/{_ADMIN_PAGES['/admin/errors']}").read_text(encoding="utf-8")


@app.get("/admin/activity", response_class=HTMLResponse)
async def admin_activity_page(request: Request):
    redirect = await _require_admin_page_session(request)
    if redirect is not None:
        return redirect
    return Path(f"app/templates/{_ADMIN_PAGES['/admin/activity']}").read_text(encoding="utf-8")


@app.get("/admin/feedback", response_class=HTMLResponse)
async def admin_feedback_page(request: Request):
    redirect = await _require_admin_page_session(request)
    if redirect is not None:
        return redirect
    return Path(f"app/templates/{_ADMIN_PAGES['/admin/feedback']}").read_text(encoding="utf-8")


@app.get("/admin/health", response_class=HTMLResponse)
async def admin_health_page(request: Request):
    redirect = await _require_admin_page_session(request)
    if redirect is not None:
        return redirect
    return Path(f"app/templates/{_ADMIN_PAGES['/admin/health']}").read_text(encoding="utf-8")


@app.get("/admin/audit-log", response_class=HTMLResponse)
async def admin_audit_log_page(request: Request):
    redirect = await _require_admin_page_session(request)
    if redirect is not None:
        return redirect
    return Path(f"app/templates/{_ADMIN_PAGES['/admin/audit-log']}").read_text(encoding="utf-8")
