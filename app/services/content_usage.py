"""Content → work memory: фиксация факта публикации/использования Artifact.

Transport-independent сервис (без aiogram) поверх ArtifactRepository и
WorkRepository. Правило "Artifact used ⇒ ровно один completed content
work_item" не зашито в Telegram handler — оно живёт здесь, чтобы будущий
VK/другой транспорт мог вызвать тот же mark_used() без дублирования логики.

Идемпотентность без настоящей кросс-repository транзакции: ArtifactRepository
и WorkRepository — независимые SQLite-соединения, атомарной транзакции между
ними в текущей архитектуре нет, и переделывать storage architecture это не
задача этого этапа. Вместо этого artifacts.status используется как CAS-замок
(см. ArtifactRepository.mark_used_if_not_already) — переход в 'used'
получает ровно один вызов, и только он создаёт work_item. Остаточное окно
гонки описано в docstring mark_used ниже.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.content import Artifact
from app.domain.work import WorkItem
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.work_repository import WorkRepository


@dataclass(frozen=True)
class ContentUsageResult:
    outcome: str  # "marked_used" | "already_used" | "archived" | "not_found"
    artifact: Artifact | None
    work_item: WorkItem | None


class ContentUsageService:
    def __init__(
        self, artifact_repository: ArtifactRepository, work_repository: WorkRepository,
    ) -> None:
        self._artifacts = artifact_repository
        self._work = work_repository

    async def mark_used(self, workspace_id: int, artifact_id: int) -> ContentUsageResult:
        """Отмечает Artifact опубликованным/использованным.

        Fail-closed: artifact не найден в этом workspace (чужой/несуществующий)
        или уже archived — ничего не меняется, work_item не создаётся.

        Идемпотентно: повторный вызов для уже 'used' artifact не создаёт
        второй work_item, а возвращает уже существующий.

        Остаточное окно гонки: между CAS-переходом artifacts.status (владеет
        ровно один вызов, см. mark_used_if_not_already) и созданием
        соответствующего work_item есть короткий промежуток, в котором
        artifact уже 'used', а work_item ещё не создан. Конкурентный вызов,
        попавший ровно в этот промежуток, не станет владельцем перехода
        (transitioned=False) и не найдёт work_item — тогда он создаст его сам.
        Два параллельных вызова могли бы оба попасть в эту ветку и создать
        два work_item, но только если оба выполняются в микросекундном окне
        между UPDATE и INSERT первого вызова — для последовательных тапов
        одного пользователя в Telegram это не реалистичный сценарий.
        """
        artifact = await self._artifacts.get_artifact(workspace_id, artifact_id)
        if artifact is None:
            return ContentUsageResult(outcome="not_found", artifact=None, work_item=None)
        if artifact.status == "archived":
            return ContentUsageResult(outcome="archived", artifact=artifact, work_item=None)

        updated_artifact, transitioned = await self._artifacts.mark_used_if_not_already(
            workspace_id, artifact_id,
        )
        if transitioned:
            work_item = await self._work.create_work_item(
                workspace_id, kind="content", lifecycle="done",
                ref_type="artifact", ref_id=artifact_id, next_step="",
            )
            return ContentUsageResult(
                outcome="marked_used", artifact=updated_artifact, work_item=work_item,
            )

        existing = await self._work.find_work_item_by_ref(
            workspace_id, ref_type="artifact", ref_id=artifact_id,
            kind="content", lifecycle="done",
        )
        if existing is not None:
            return ContentUsageResult(
                outcome="already_used", artifact=updated_artifact, work_item=existing,
            )

        # Остаточное окно гонки, описанное в docstring выше.
        work_item = await self._work.create_work_item(
            workspace_id, kind="content", lifecycle="done",
            ref_type="artifact", ref_id=artifact_id, next_step="",
        )
        return ContentUsageResult(
            outcome="marked_used", artifact=updated_artifact, work_item=work_item,
        )
