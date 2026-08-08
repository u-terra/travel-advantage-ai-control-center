"""Provider-agnostic LLM-слой Оркестратора.

Импорт — из подмодулей, как принято в остальном проекте:

- ``app.services.llm.base`` — интерфейс ``LLMProvider``;
- ``app.services.llm.models`` — нормализованные модели ответа;
- ``app.services.llm.factory`` — выбор провайдера по ``LLM_PROVIDER``;
- ``app.services.llm.openai_provider`` — текущий адаптер OpenAI.

Пакет намеренно ничего не реэкспортирует: транспорт
(``app.services.content_factory``) импортирует модели отсюда, а фабрика
импортирует транспорт, поэтому реэкспорт в этом файле создал бы цикл.
"""
