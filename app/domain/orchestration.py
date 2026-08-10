from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Any, Mapping

from app.domain.content import ARTIFACT_TYPES


ACTION_CREATE_ARTIFACT = "create_artifact"
SUPPORTED_GENERATION_ACTIONS = frozenset({ACTION_CREATE_ARTIFACT})
SUPPORTED_OUTPUT_FORMATS = frozenset({"telegram", "vk"})
_SECRET_FIELD_NAMES = frozenset(
    {
        "apikey", "token", "accesstoken", "refreshtoken", "password",
        "secret", "credential", "credentials",
    }
)


class GenerationSpecValidationError(ValueError):
    pass


@dataclass(frozen=True)
class GenerationSpec:
    action_type: str
    artifact_type: str
    objective: str
    output_format: str
    business_context: Mapping[str, Any]
    trusted_source_facts: Mapping[str, Any]
    untrusted_source_content: str
    verified_claims: tuple[Mapping[str, Any], ...]
    unverified_claims: tuple[Mapping[str, Any], ...]
    constraints: tuple[str, ...]
    profile_revision: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "business_context", freeze_json_value(self.business_context))
        object.__setattr__(
            self, "trusted_source_facts", freeze_json_value(self.trusted_source_facts)
        )
        object.__setattr__(self, "verified_claims", freeze_json_value(self.verified_claims))
        object.__setattr__(
            self, "unverified_claims", freeze_json_value(self.unverified_claims)
        )
        object.__setattr__(self, "constraints", freeze_json_value(self.constraints))
        validate_generation_spec(self)


def freeze_json_value(value: Any) -> Any:
    """Return a defensive, recursively immutable snapshot of a JSON-safe value."""
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


def thaw_json_value(value: Any) -> Any:
    """Convert an already validated frozen value to a JSON-serializable copy."""
    if isinstance(value, Mapping):
        return {key: thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json_value(item) for item in value]
    return value


def validate_generation_spec(spec: GenerationSpec) -> None:
    if spec.action_type not in SUPPORTED_GENERATION_ACTIONS:
        raise GenerationSpecValidationError("Неизвестный action_type")
    if spec.artifact_type not in ARTIFACT_TYPES:
        raise GenerationSpecValidationError("Неизвестный artifact_type")
    if spec.output_format not in SUPPORTED_OUTPUT_FORMATS:
        raise GenerationSpecValidationError("Неизвестный output_format")
    if not isinstance(spec.objective, str) or not spec.objective.strip():
        raise GenerationSpecValidationError("objective не должен быть пустым")
    if not isinstance(spec.business_context, Mapping):
        raise GenerationSpecValidationError("business_context должен быть mapping")
    if not isinstance(spec.trusted_source_facts, Mapping):
        raise GenerationSpecValidationError("trusted_source_facts должен быть mapping")
    if not isinstance(spec.untrusted_source_content, str):
        raise GenerationSpecValidationError("untrusted_source_content должен быть строкой")
    if spec.profile_revision is not None and (
        type(spec.profile_revision) is not int or spec.profile_revision < 1
    ):
        raise GenerationSpecValidationError("profile_revision должен быть >= 1 либо None")
    _validate_string_tuple(spec.constraints, "constraints")
    verified = _validate_claims(spec.verified_claims, "verified_claims", "verified")
    unverified = _validate_claims(
        spec.unverified_claims, "unverified_claims", "unverified"
    )
    if verified & unverified:
        raise GenerationSpecValidationError(
            "Один claim не может быть одновременно verified и unverified"
        )
    for value in (
        spec.business_context,
        spec.trusted_source_facts,
        spec.verified_claims,
        spec.unverified_claims,
    ):
        _reject_secret_fields(value)


def _validate_claims(
    claims: tuple[Mapping[str, Any], ...], field: str, expected_status: str
) -> set[str]:
    if type(claims) is not tuple:
        raise GenerationSpecValidationError(f"{field} должен быть tuple")
    texts: set[str] = set()
    for claim in claims:
        if not isinstance(claim, Mapping):
            raise GenerationSpecValidationError(f"{field} содержит не-mapping claim")
        if set(claim) != {"text", "verification_status", "evidence_reference"}:
            raise GenerationSpecValidationError(f"{field} содержит неверную структуру claim")
        text = claim["text"]
        evidence = claim["evidence_reference"]
        if not isinstance(text, str) or not text.strip():
            raise GenerationSpecValidationError(f"{field}.text не должен быть пустым")
        if claim["verification_status"] != expected_status:
            raise GenerationSpecValidationError(f"{field} содержит неверный статус")
        if evidence is not None and not isinstance(evidence, str):
            raise GenerationSpecValidationError(
                f"{field}.evidence_reference должен быть строкой либо None"
            )
        texts.add(text.strip())
    return texts


def _validate_string_tuple(value: tuple[str, ...], field: str) -> None:
    if type(value) is not tuple or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise GenerationSpecValidationError(f"{field} должен быть tuple непустых строк")


def _reject_secret_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise GenerationSpecValidationError("Ключи mappings должны быть строками")
            normalized = key.strip().lower().replace("_", "").replace("-", "")
            if normalized in _SECRET_FIELD_NAMES:
                raise GenerationSpecValidationError("GenerationSpec содержит secret field")
            _reject_secret_fields(item)
    elif isinstance(value, tuple):
        for item in value:
            _reject_secret_fields(item)
    elif value is not None and type(value) not in {bool, int, float, str}:
        raise GenerationSpecValidationError("GenerationSpec содержит не-JSON тип")
