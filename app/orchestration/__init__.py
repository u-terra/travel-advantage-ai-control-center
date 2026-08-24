"""LLM-first orchestration layer — Phase 1, shadow mode only.

Ничего здесь не влияет на ответ пользователю: старый keyword/regex router
(``app.routing``) остаётся единственным, кто реально маршрутизирует запрос.
Этот пакет параллельно строит своё решение и логирует сравнение со старым —
см. ``app.orchestration.shadow.run_shadow_orchestration``.
"""

from __future__ import annotations
