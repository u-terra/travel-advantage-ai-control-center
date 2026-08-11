from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from types import MappingProxyType
from typing import Any, Mapping

from app.domain.content import ARTIFACT_TYPES


class GenerationAction(StrEnum):
    CREATE_ARTIFACT = "create_artifact"


class OutputFormat(StrEnum):
    TELEGRAM = "telegram"
    VK = "vk"


class GenerationSpecValidationError(ValueError):
    pass


@dataclass(frozen=True)
class GenerationSpec:
    action_type: GenerationAction
    artifact_type: str
    objective: str
    audience: tuple[str, ...]
    output_format: OutputFormat
    source_facts: Mapping[str, Any]
    trusted_business_context: Mapping[str, Any]
    untrusted_source_content: str
    tone_preferences: Mapping[str, Any]
    verified_claims_allowed: tuple[Mapping[str, Any], ...]
    unverified_claims_requiring_caution: tuple[Mapping[str, Any], ...]
    constraints: tuple[str, ...]
    profile_revision_used: int | None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "action_type", GenerationAction(self.action_type))
            object.__setattr__(self, "output_format", OutputFormat(self.output_format))
        except (TypeError, ValueError) as exc:
            raise GenerationSpecValidationError("Неизвестный action_type/output_format") from exc
        for field in (
            "audience", "source_facts", "trusted_business_context",
            "tone_preferences", "verified_claims_allowed",
            "unverified_claims_requiring_caution", "constraints",
        ):
            object.__setattr__(self, field, freeze_json_value(getattr(self, field)))
        validate_generation_spec(self)


def freeze_json_value(value: Any) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise GenerationSpecValidationError("Float должен быть конечным")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise GenerationSpecValidationError("Ключи mappings должны быть строками")
            frozen[key] = freeze_json_value(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json_value(item) for item in value)
    raise GenerationSpecValidationError(
        f"Неподдерживаемый тип GenerationSpec: {type(value).__name__}"
    )


def validate_generation_spec(spec: GenerationSpec) -> None:
    if type(spec.action_type) is not GenerationAction:
        raise GenerationSpecValidationError("Неизвестный action_type")
    if spec.artifact_type not in ARTIFACT_TYPES:
        raise GenerationSpecValidationError("Неизвестный artifact_type")
    if type(spec.output_format) is not OutputFormat:
        raise GenerationSpecValidationError("Неизвестный output_format")
    if not isinstance(spec.objective, str) or not spec.objective.strip():
        raise GenerationSpecValidationError("objective не должен быть пустым")
    if not isinstance(spec.untrusted_source_content, str):
        raise GenerationSpecValidationError("untrusted_source_content должен быть строкой")
    if spec.profile_revision_used is not None and (
        type(spec.profile_revision_used) is not int or spec.profile_revision_used < 1
    ):
        raise GenerationSpecValidationError("profile_revision_used должен быть >= 1 либо None")
    _validate_string_tuple(spec.audience, "audience")
    _validate_string_tuple(spec.constraints, "constraints")
    for field in ("source_facts", "trusted_business_context", "tone_preferences"):
        value = getattr(spec, field)
        if not isinstance(value, Mapping):
            raise GenerationSpecValidationError(f"{field} должен быть mapping")
        _reject_secret_fields(value)
    verified = _validate_claims(
        spec.verified_claims_allowed, "verified_claims_allowed", "verified"
    )
    unverified = _validate_claims(
        spec.unverified_claims_requiring_caution,
        "unverified_claims_requiring_caution", "unverified",
    )
    if verified & unverified:
        raise GenerationSpecValidationError(
            "Один claim не может быть одновременно verified и unverified"
        )


def _validate_claims(claims: tuple[Any, ...], field: str, status: str) -> set[str]:
    if type(claims) is not tuple:
        raise GenerationSpecValidationError(f"{field} должен быть tuple")
    texts: set[str] = set()
    for claim in claims:
        if not isinstance(claim, Mapping) or set(claim) != {
            "text", "verification_status", "evidence_reference"
        }:
            raise GenerationSpecValidationError(f"{field} содержит неверный claim")
        text = claim["text"]
        evidence = claim["evidence_reference"]
        if not isinstance(text, str) or not text.strip():
            raise GenerationSpecValidationError(f"{field}.text не должен быть пустым")
        if claim["verification_status"] != status:
            raise GenerationSpecValidationError(f"{field} содержит неверный статус")
        if evidence is not None and not isinstance(evidence, str):
            raise GenerationSpecValidationError(
                f"{field}.evidence_reference должен быть строкой либо None"
            )
        _reject_secret_fields(claim)
        texts.add(text.strip())
    return texts


def _validate_string_tuple(value: tuple[str, ...], field: str) -> None:
    if type(value) is not tuple or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise GenerationSpecValidationError(f"{field} должен быть tuple непустых строк")


_SECRET_FIELDS = frozenset({
    "apikey", "token", "accesstoken", "refreshtoken", "password",
    "secret", "credential", "credentials", "telegramuserid", "memberid",
})


def _reject_secret_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = key.strip().lower().replace("_", "").replace("-", "")
            if normalized in _SECRET_FIELDS:
                raise GenerationSpecValidationError("GenerationSpec содержит запрещённое поле")
            _reject_secret_fields(item)
    elif isinstance(value, tuple):
        for item in value:
            _reject_secret_fields(item)
