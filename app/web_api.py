from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import markdown
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.chat_provider import AttachmentInput, ChatConfig, OpenAIChatProvider
from app.config import load_settings
from app.domain.business_profiles import (
    BusinessProfileValidationError,
    StaleBusinessProfileError,
)
from app.domain.competitor_discovery import canonical_domain
from app.domain.usage import UsageStatus
from app.domain.web_attachment import WebAttachment
from app.domain.web_auth import WebPrincipal
from app.domain.web_conversation import ROLE_ASSISTANT, ROLE_USER
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.competitor_repository import (
    CompetitorAddressError,
    CompetitorLabelError,
    CompetitorRepository,
)
from app.repositories.knowledge_repository import KnowledgeRepository
from app.repositories.partner_repository import (
    PartnerRepository,
    TooManyUserExamplesError,
    business_context_to_dict,
)
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
from app.services.knowledge_service import KnowledgeBundle, KnowledgeService
from app.services.lead_radar import LeadRadarConfig, build_workspace_signals, category_label
from app.services.llm.factory import create_llm_provider
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
)


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
            return generic_error

        binding = await web_auth_repository.get_default_binding(user.id)
        if binding is None:
            # A web account with no workspace binding can't do anything -
            # fail closed the same way an unowned resource does elsewhere.
            return generic_error

        await _start_session(response, user.id, binding.id)
        await web_auth_repository.touch_last_login(user.id)

        return {"email": user.email, "workspace_id": binding.workspace_id}

    except Exception:
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

        return {"email": user.email, "workspace_id": binding.workspace_id}

    except EmailAlreadyRegisteredError as exc:
        return {"error": str(exc)}
    except Exception:
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
    return {
        "email": principal.email,
        "workspace_id": principal.workspace_id,
        "role": principal.role,
    }


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

    return (
        "=== COMPETITOR INTELLIGENCE ===\n"
        "Данные получены из публичных источников конкурента.\n"
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
async def list_competitors(principal: WebPrincipal = Depends(get_current_principal)):
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
    principal: WebPrincipal = Depends(require_csrf),
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
        return {"error": str(exc), "competitor": None}
    except Exception:
        return {"error": "Не удалось добавить конкурента.", "competitor": None}


@app.get("/api/competitors/{competitor_id}/intelligence")
async def get_competitor_intelligence(
    competitor_id: int, principal: WebPrincipal = Depends(get_current_principal),
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


@app.get("/api/signals")
async def list_signals(principal: WebPrincipal = Depends(get_current_principal)):
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
        signals = build_workspace_signals(lead_radar_config, records, limit=5)

        if signals is None:
            return {"error": "Радар сигналов сейчас недоступен.", "signals": []}

        source_names = {
            record.interpretation_id: record.source_name for record in records
        }

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


@app.get("/api/knowledge")
async def list_knowledge(principal: WebPrincipal = Depends(get_current_principal)):
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
async def list_materials(principal: WebPrincipal = Depends(get_current_principal)):
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
    artifact_id: int, principal: WebPrincipal = Depends(get_current_principal),
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
    principal: WebPrincipal = Depends(require_csrf),
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

        return {
            "material": _material_payload(updated_artifact),
            "version": _version_payload(new_version),
        }

    except Exception:
        return {"error": "Не удалось сохранить материал.", "material": None, "version": None}


@app.delete("/api/materials/{artifact_id}")
async def delete_material(
    artifact_id: int, principal: WebPrincipal = Depends(require_csrf),
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
    }


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
async def get_profile(principal: WebPrincipal = Depends(get_current_principal)):
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
    principal: WebPrincipal = Depends(require_csrf),
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
    principal: WebPrincipal = Depends(require_csrf),
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
    principal: WebPrincipal = Depends(require_csrf),
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
    principal: WebPrincipal = Depends(require_csrf),
):
    try:
        preferences = await partner_repository.clear_user_example_posts(
            principal.workspace_id, principal.telegram_user_id,
        )
        return {"personal_style": _personal_style_payload(preferences)}

    except Exception:
        return {"error": "Не удалось очистить примеры.", "personal_style": None}


@app.post("/api/onboarding/complete")
async def complete_onboarding(
    request: OnboardingCompleteRequest,
    principal: WebPrincipal = Depends(require_csrf),
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
    principal: WebPrincipal = Depends(require_csrf),
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

        return {"attachments": [_attachment_payload(a) for a in saved]}

    except Exception:
        return {"error": "Не удалось загрузить файл. Попробуйте ещё раз.", "attachments": []}


@app.delete("/api/attachments/{public_id}")
async def delete_pending_attachment(
    public_id: str, principal: WebPrincipal = Depends(require_csrf),
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
    public_id: str, principal: WebPrincipal = Depends(get_current_principal),
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
async def create_conversation(principal: WebPrincipal = Depends(require_csrf)):
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
async def list_conversations(principal: WebPrincipal = Depends(get_current_principal)):
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
    conversation_id: int, principal: WebPrincipal = Depends(get_current_principal),
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
async def chat(request: ChatRequest, principal: WebPrincipal = Depends(require_csrf)):
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

        preferences = await partner_repository.get_user_preferences(
            principal.workspace_id,
            principal.telegram_user_id,
        )

        personal_style = (
            preferences.style_description.strip()
            if preferences is not None
            else ""
        )

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
        await web_conversation_repository.add_message(
            principal.workspace_id, principal.telegram_user_id, request.conversation_id,
            ROLE_ASSISTANT, clean_answer,
        )

        updated_conversation = await web_conversation_repository.get_conversation(
            principal.workspace_id, principal.telegram_user_id, request.conversation_id,
        )

        return {
            "answer": clean_answer,
            "answer_html": answer_html,
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


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Unauthenticated visitors never see the cabinet - a plain redirect
    to /login, not a 401 (this is a browser page, not a JSON API call).
    A first-time (or otherwise not-yet-onboarded) binding is sent to
    /onboarding instead of the cabinet - see _onboarding_pending()."""
    ctx = await _valid_session_context(request)
    if ctx is None:
        return RedirectResponse(url="/login", status_code=303)
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
    /onboarding after completion shows current values, not a hard block)."""
    if not await _has_valid_session(request):
        return RedirectResponse(url="/login", status_code=303)
    return Path(
        "app/templates/onboarding.html"
    ).read_text(encoding="utf-8")
