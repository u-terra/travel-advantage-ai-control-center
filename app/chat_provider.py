from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass

from app.domain.usage import LLMUsage


_RESPONSES_URL = "https://api.openai.com/v1/responses"


@dataclass(frozen=True)
class ChatConfig:
    api_key: str
    model: str = "gpt-5.6-terra"
    timeout_seconds: float = 180.0


@dataclass(frozen=True)
class ChatResult:
    text: str
    usage: LLMUsage | None


class OpenAIChatProvider:
    def __init__(self, config: ChatConfig) -> None:
        self.config = config

    def generate(
        self,
        *,
        message: str,
        history: list[dict[str, str]] | None = None,
        knowledge_context: str | None = None,
        personal_style: str | None = None,
        workspace_memory: str | None = None,
    ) -> ChatResult:
        input_items: list[dict[str, str]] = []

        for item in (history or [])[-12:]:
            role = item.get("role")
            content = item.get("content")

            if (
                role in {"user", "assistant"}
                and isinstance(content, str)
                and content.strip()
            ):
                input_items.append({
                    "role": role,
                    "content": content,
                })

        input_items.append({
            "role": "user",
            "content": message,
        })

        instructions = (
            "Ты — ORCHESTRAVEL, рабочий AI-ассистент для людей, "
            "работающих в туризме. "
            "Отвечай по-русски, содержательно, естественно и профессионально. "
            "Не сокращай ответ ради ограничений Telegram. "
            "Давай сначала прямой ответ на вопрос, затем углубляйся настолько, "
            "насколько это действительно полезно. "
            "Используй заголовки, списки и таблицы, когда они улучшают понимание. "
            "Не превращай обычный вопрос в рекламный пост и не добавляй шаблонный CTA. "
            "В рекомендациях всегда различай уровень действий. "
            "Сначала давай то, что может практически сделать сам пользователь: "
            "партнёр, турагент или специалист по туризму. "
            "Рекомендации, которые требуют решений владельца платформы, продукта "
            "или корпоративного маркетинга, выноси отдельно и явно помечай "
            "как рекомендации для самой платформы. "
            "Не советуй пользователю менять продукт, интерфейс или процессы компании, "
            "если он сам не может на них влиять."
        )

        if personal_style:
            instructions += (
                "\n\nЛИЧНЫЙ СТИЛЬ ПОЛЬЗОВАТЕЛЯ:\n"
                + personal_style.strip()
                + "\nЛичный стиль обязателен для формы обращения. "
                "Если пользователь просит обращаться на «ты», обращайся к нему "
                "только на «ты» во всём ответе: используй «ты», «тебе», «твой», "
                "глаголы второго лица единственного числа. "
                "Не переходи на «вы» из-за делового, аналитического или официального тона. "
                "Остальные стилевые предпочтения соблюдай, если они не конфликтуют "
                "с требованиями точности и безопасности."
            )

        if knowledge_context:
            instructions += (
                "\n\nНиже может быть проверенная база знаний и/или "
                "Competitor Intelligence по публичным источникам. "
                "Факты Travel Advantage и MWR Life из базы знаний считай авторитетными. "
                "Данные Competitor Intelligence используй как внешние доказательства. "
                "Не придумывай отсутствующие факты и отделяй данные от своих выводов."
                "\n\n"
                + knowledge_context
            )

        if workspace_memory:
            instructions += (
                "\n\nWORKSPACE MEMORY (рабочий контекст проекта):\n"
                + workspace_memory.strip()
                + "\nЭто рабочая память проекта: текущие приоритеты, договорённости, "
                "статус задач. Она НЕ является источником проверенных фактов о "
                "Travel Advantage или MWR Life — для проверенных фактов используй "
                "только базу знаний выше."
            )

        payload = {
            "model": self.config.model,
            "instructions": instructions,
            "input": input_items,
            "reasoning": {"effort": "low"},
            "max_output_tokens": 4000,
        }

        req = urllib.request.Request(
            _RESPONSES_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
        )

        with urllib.request.urlopen(
            req,
            timeout=self.config.timeout_seconds,
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        parts: list[str] = []

        for item in data.get("output", []):
            if item.get("type") != "message":
                continue

            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    text = content.get("text")

                    if isinstance(text, str):
                        parts.append(text)

        result = "\n".join(parts).strip()

        if not result:
            raise RuntimeError("OpenAI returned no output text")

        usage = None
        usage_data = data.get("usage")

        if isinstance(usage_data, dict):
            def as_int(value):
                return value if isinstance(value, int) else None

            usage = LLMUsage(
                input_tokens=as_int(usage_data.get("input_tokens")),
                output_tokens=as_int(usage_data.get("output_tokens")),
                total_tokens=as_int(usage_data.get("total_tokens")),
            )

        return ChatResult(
            text=result,
            usage=usage,
        )
