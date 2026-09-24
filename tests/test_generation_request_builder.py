from __future__ import annotations

from dataclasses import replace
import json

import pytest

from app.domain.orchestration import GenerationAction, GenerationSpec, OutputFormat
from app.services.generation_request_builder import (
    MIN_USEFUL_SUMMARY_LENGTH,
    SOURCE_ANALYSIS_REQUEST_LIMIT,
    SourceAnalysisRequestTooLargeError,
    build_client_reply_provider_request,
    build_provider_generation_request,
    build_source_analysis_provider_request,
    safe_material_title,
    source_content_is_sufficient,
)


# Content Factory's own /internal/generate hard cap (confirmed live against
# production: HTTP 400 "Исходный текст слишком длинный (N символов).
# Максимум — 6000." for anything longer, returned BEFORE any LLM call).
_CONTENT_FACTORY_MAX_SOURCE_TEXT_LENGTH = 6000


def _spec(
    *,
    source_facts=None,
    trusted_business_context=None,
    verified_claims_allowed=(),
    unverified_claims_requiring_caution=(),
    constraints=("Черновик требует ручной проверки перед использованием.",),
    untrusted_source_content="Исходный текст",
) -> GenerationSpec:
    return GenerationSpec(
        action_type=GenerationAction.CREATE_ARTIFACT,
        artifact_type="post",
        objective="Создать черновик материала по выбранному и разобранному источнику.",
        audience=("Путешественники",),
        output_format=OutputFormat.TELEGRAM,
        source_facts=source_facts or {},
        trusted_business_context=trusted_business_context or {},
        untrusted_source_content=untrusted_source_content,
        tone_preferences={},
        personal_style={},
        verified_claims_allowed=verified_claims_allowed,
        unverified_claims_requiring_caution=unverified_claims_requiring_caution,
        constraints=constraints,
        profile_revision_used=None,
    )


# --- Investigation: build_provider_generation_request is NOT section-aware ---

def test_generic_builder_only_trims_untrusted_source_content():
    """Investigation finding: только untrusted_source_content когда-либо
    обрезается по частям — все остальные секции (включая CONSTRAINTS и
    SOURCE FACTS) либо входят целиком, либо (в крайнем случае переполнения
    даже при пустом untrusted-контенте) обрезаются сырым срезом [:limit] по
    всей строке, что может разорвать JSON. Это и есть причина, почему для
    source-analysis flow нужна отдельная, section-aware функция."""
    long_constraint = "X" * 5000
    spec = _spec(constraints=(long_constraint,), untrusted_source_content="Y" * 5000)
    request = build_provider_generation_request(spec, limit=5600)
    # CONSTRAINTS (первая часть prefix) осталась ЦЕЛОЙ — обрезан только хвост
    # (untrusted content, вплоть до пустоты), а если и этого не хватает —
    # обрезается сырой конец всей строки, а не конкретная секция.
    assert long_constraint in request.source_text
    assert len(request.source_text) <= 5600


def test_generic_builder_can_corrupt_structure_when_prefix_alone_overflows():
    """Investigation finding: если ДАЖЕ пустой untrusted_source_content не
    помещается в limit (сам prefix длиннее limit), функция не отказывает и не
    компактизирует секции — она молча возвращает обрезанную с конца строку,
    которая может обрывать JSON-массив/объект посередине."""
    long_constraint = "X" * 5000
    spec = _spec(constraints=(long_constraint,), untrusted_source_content="")
    request = build_provider_generation_request(spec, limit=3000)
    assert len(request.source_text) == 3000
    # Сырой срез оборвал CONSTRAINTS-секцию посередине строки X*5000.
    assert request.source_text.endswith("X")
    assert not request.source_text.rstrip().endswith('"]')  # JSON-массив не закрыт


# --- Fix 1: build_source_analysis_provider_request ---

def test_default_limit_is_under_content_factory_cap_with_safety_margin():
    assert SOURCE_ANALYSIS_REQUEST_LIMIT < _CONTENT_FACTORY_MAX_SOURCE_TEXT_LENGTH
    assert _CONTENT_FACTORY_MAX_SOURCE_TEXT_LENGTH - SOURCE_ANALYSIS_REQUEST_LIMIT >= 100


def test_normal_case_is_unchanged_when_it_already_fits():
    spec = _spec(
        source_facts={"key_facts": ("Факт",), "disputed_claims": ("Спорное",)},
        untrusted_source_content="Небольшой текст источника",
    )
    via_generic = build_provider_generation_request(spec, limit=SOURCE_ANALYSIS_REQUEST_LIMIT)
    via_source_analysis = build_source_analysis_provider_request(spec)
    assert via_source_analysis == via_generic


def test_overflow_case_drops_only_low_priority_source_facts_and_stays_under_limit():
    """Regression: payload того же класса, который раньше давал >6000 для
    реального прод-кейса (Анталия) — здесь воспроизведено искусственно
    (специально большие content_angles/warnings), чтобы гарантированно
    заставить prefix один превысить лимит и проверить, что удаляются именно
    низкоприоритетные поля, а не факты."""
    source_facts = {
        "summary": "Кейс клиента",
        "key_facts": ("Факт про 47 тыс. вместо 113 тыс.", "Факт про гостей-пассажиров"),
        "disputed_claims": ("Без партнёрства и взносов",),
        "audience_value": "Ценность",
        "content_angles": tuple(f"Идея подачи номер {i} " + "текст " * 20 for i in range(10)),
        "recommended_formats": tuple(f"формат-{i}" for i in range(10)),
        "target_audiences": tuple(f"аудитория-{i} " + "описание " * 10 for i in range(10)),
        "warnings": tuple(f"предупреждение-{i} " + "детали " * 10 for i in range(10)),
    }
    spec = _spec(
        source_facts=source_facts,
        constraints=("Базовый constraint.", "Атрибуция кейса." * 100, "Масштаб единичного случая." * 80),
        untrusted_source_content="Оригинальный текст публикации автора.",
    )
    # Убеждаемся, что это действительно overflow-сценарий: обычный builder
    # с тем же лимитом не помещается без обрезки контента.
    plain = build_provider_generation_request(spec, limit=SOURCE_ANALYSIS_REQUEST_LIMIT)
    assert "Оригинальный текст публикации автора." not in plain.source_text

    request = build_source_analysis_provider_request(spec)
    assert len(request.source_text) <= SOURCE_ANALYSIS_REQUEST_LIMIT
    # Факты и constraints сохранены дословно.
    assert "47 тыс. вместо 113 тыс." in request.source_text
    assert "гостей-пассажиров" in request.source_text
    assert "Без партнёрства и взносов" in request.source_text
    assert "Атрибуция кейса." in request.source_text
    assert "Масштаб единичного случая." in request.source_text
    # Низкоприоритетные поля были удалены (не помещались).
    assert "Идея подачи номер" not in request.source_text
    assert "формат-0" not in request.source_text
    assert "аудитория-0" not in request.source_text
    assert "предупреждение-0" not in request.source_text


# --- Targeted review before 2a681a8 deploy: overflow that survives the
# low-priority-field drop. Fix 1 only handles "prefix overflows, dropping
# content_angles/recommended_formats/target_audiences/warnings brings it back
# under limit". If the PROTECTED part of prefix alone (key_facts,
# disputed_claims, verified/unverified claims, trusted_business_context,
# constraints) is still too large after that drop, the function used to fall
# straight through to build_provider_generation_request(), which — same as
# test_generic_builder_can_corrupt_structure_when_prefix_alone_overflows above
# — silently returns a raw [:limit] slice that can cut any protected section
# (including CONSTRAINTS) mid-string and break the JSON structure. ---

def test_overflow_surviving_low_priority_drop_fails_closed_not_corrupted():
    """When even the protected prefix (here: huge key_facts) doesn't fit
    under the limit after dropping low-priority SOURCE FACTS fields, the
    function must fail closed with a clear error instead of silently
    returning a structurally corrupted (raw-sliced) source_text."""
    huge_key_facts = tuple(
        f"Факт номер {i}: важная деталь кейса, которая не должна теряться. " * 5
        for i in range(120)
    )
    source_facts = {
        "summary": "Кейс клиента",
        "key_facts": huge_key_facts,
        "disputed_claims": ("Без партнёрства и взносов",),
        "content_angles": ("Идея",),
        "recommended_formats": ("формат",),
        "warnings": ("предупреждение",),
    }
    spec = _spec(
        source_facts=source_facts,
        constraints=("Атрибуция кейса.",),
        untrusted_source_content="Оригинальный текст публикации автора.",
    )
    # Контрастная проверка: без fail-closed защиты обычный builder на этом же
    # лимите молча вернул бы сырой обрубок, разрывающий JSON/секции.
    plain = build_provider_generation_request(spec, limit=SOURCE_ANALYSIS_REQUEST_LIMIT)
    assert len(plain.source_text) == SOURCE_ANALYSIS_REQUEST_LIMIT
    with pytest.raises(SourceAnalysisRequestTooLargeError):
        build_source_analysis_provider_request(spec)


def test_overflow_surviving_low_priority_drop_fails_closed_via_huge_constraints():
    """Same overflow class as above, driven by huge constraints instead of
    huge key_facts — constraints are never dropped/reduced by this function,
    so an oversized constraints section alone must also fail closed rather
    than fall through to the corrupting raw-slice path."""
    source_facts = {
        "summary": "Кейс клиента",
        "key_facts": ("Факт про 47 тыс. вместо 113 тыс.",),
        "disputed_claims": ("Без партнёрства и взносов",),
        "content_angles": ("Идея",),
        "recommended_formats": ("формат",),
        "warnings": ("предупреждение",),
    }
    spec = _spec(
        source_facts=source_facts,
        constraints=("Атрибуция кейса." * 500,),
        untrusted_source_content="Оригинальный текст публикации автора.",
    )
    with pytest.raises(SourceAnalysisRequestTooLargeError):
        build_source_analysis_provider_request(spec)


def test_other_flows_are_not_affected_by_the_new_limit():
    """build_provider_generation_request (используется free-text/radar/client
    reply) не тронут — дефолтный limit=11_000, никакого дропа полей."""
    from inspect import signature

    sig = signature(build_provider_generation_request)
    assert sig.parameters["limit"].default == 11_000


# --- Real-world regression: the exact Antalya production case ---

_REAL_ORIGINAL_TEXT = (
    "Свежий кейс! \n"
    "Отправили брата с женой в путешествие в Анталию, за отель отдали 47 т.р. вместо 113 на Букинге, трансфер тоже в 3 раза дешевле. \n"
    "В общем от начальной суммы путешествия сэкономили им почти 90 тысяч. \n"
    "Они до сих пор не верят: А, что так можно было?\n\n"
    "Хотя ни брат, ни его жена партнерами клуба не являются (но уже очень хотят ими стать), они записаны в аккаунт, как гости-пассажиры! \n"
    "Прилетели довольные, теперь всем рассказывают, что сказочно отдохнули и уже планируют поездку снова!  \n"
    "И, оказалось, что вот так тоже можно! \n"
    "Без баллов, без парнерства, без взносов, без заморочек, со скидками в 67%…просто папа решил сделать ребенку 🎁 подарок и записал его в свой аккаунт! \n"
    "Чудеса!🎉"
)

_REAL_KEY_FACTS = (
    "Упоминается поездка в Анталию.",
    "Указано, что за отель отдали 47 т.р. вместо 113 на Букинге.",
    "Указано, что трансфер был в 3 раза дешевле.",
    "Сообщается, что с начальной суммы путешествия сэкономили почти 90 тысяч.",
    "Указано, что брат и его жена не являются партнерами клуба.",
    "Указано, что они записаны в аккаунт как гости-пассажиры.",
    "Сообщается, что они прилетели довольные и планируют поездку снова.",
    "Утверждается, что можно записать ребенка в свой аккаунт как подарок.",
    "Упоминаются скидки в 67% и отсутствие баллов, партнерства, взносов и заморочек.",
)

_REAL_DISPUTED_CLAIMS = (
    "Текст не подтверждает документально сравнение цен с Букингом.",
    "Текст не подтверждает, что экономия почти 90 тысяч достигнута именно за счет указанных условий.",
    "Текст не подтверждает, что описанная схема доступна всем и работает без ограничений.",
    "Текст содержит оценочные и рекламно-утвердительные формулировки вроде «чудеса» и «сказочно отдохнули».",
    "Текст не дает внешне проверяемых деталей о клубе, правилах записи гостей или условиях скидок.",
)


def test_real_antalya_production_case_stays_under_content_factory_limit():
    """Точное воспроизведение кейса, который в production давал HTTP 400 от
    Content Factory (изначально 6114 символов > 6000, review-фикс a3852dc):
    реальный original_text (694 символа), реальные key_facts/disputed_claims
    из production БД, реальный workspace-профиль (пустой, incomplete) и
    текущий набор constraints (с тех пор выросший ещё раз - "Мой стиль /
    Голос бренда" добавил voice_sample в [PERSONAL STYLE - DATA] и его
    facts-guard). build_provider_generation_request(spec) (лимит 11000) не
    режет ничего и всегда воспроизводит реальный prod-баг как есть - его
    точная длина ниже это просто фиксирует текущее значение, а не
    содержательное требование. Содержательное требование —
    build_source_analysis_provider_request должен укладываться в лимит
    Content Factory, что и проверяется ниже."""
    from app.services.material_orchestration import MaterialOrchestrationService
    from app.domain.business_profiles import BusinessProfile, BusinessContext
    from types import MappingProxyType, SimpleNamespace

    business_context = BusinessContext(
        specializations=(), destinations=(), audiences=(), markets=(),
        positioning=MappingProxyType({"statement": "", "value_proposition": "", "differentiators": ()}),
        communication=MappingProxyType({
            "banned_formulations": (), "cta_preference": "", "emoji_preference": "",
            "formality": "", "preferred_terms": (), "tone": "Спокойный и профессиональный",
        }),
        goals=(),
        content_preferences=MappingProxyType({"channels": (), "formats": (), "preferred_topics": (), "prohibited_topics": ()}),
        public_contacts=MappingProxyType({"booking_url": "", "email": "", "phone": "", "telegram": "", "vk": "", "website": ""}),
        claims=(),
    )
    profile = BusinessProfile(
        1, 1, "Travel Advantage AI Ecosystem", "club_partner",
        "Внутреннее партнёрское рабочее пространство.", "incomplete", 1, 1,
        business_context, "now", "now",
    )
    source = SimpleNamespace(id=10, workspace_id=1, original_text=_REAL_ORIGINAL_TEXT)
    analysis = SimpleNamespace(
        source_id=10, workspace_id=1,
        summary=(
            "В тексте описан якобы свежий кейс поездки в Анталию, где отель и "
            "трансфер были оформлены дешевле, чем на Booking, а также "
            "утверждается, что гости могли быть записаны в аккаунт без "
            "партнерства и взносов."
        ),
        key_facts=_REAL_KEY_FACTS,
        disputed_claims=_REAL_DISPUTED_CLAIMS,
        audience_value=(
            "Практическая ценность средняя: материал может заинтересовать "
            "туристов как пример возможной экономии на отеле и трансфере, но "
            "требует проверки условий, правил и реальности заявленных скидок."
        ),
        target_audiences=(
            "туристы, ищущие экономию на поездках", "семьи, планирующие отдых в Анталии",
            "пользователи, интересующиеся скидками на отели и трансферы",
            "аудитория, рассматривающая клубные/аккаунтные форматы бронирования",
        ),
        content_angles=(
            "пример экономии на поездке в Анталию", "сравнение цены отеля с Booking",
            "дешевый трансфер как часть общей экономии",
            "возможность бронирования для гостей без партнерства",
            "условия скидок и способы записи гостей в аккаунт",
        ),
        recommended_formats=("кейс-разбор", "пост с разбором экономии", "FAQ по условиям бронирования", "сравнение вариантов покупки тура/отеля"),
        warnings=(
            "Источник носит рекламно-эмоциональный характер.",
            "Числа и скидки следует перепроверять по первичным данным.",
            "Нужна проверка правил клуба, прежде чем использовать материал как рекомендацию.",
        ),
    )

    spec = MaterialOrchestrationService().build_generation_spec(
        1, source, analysis, profile, artifact_type="post", output_format="telegram",
    )

    # Подтверждаем сам баг: старый способ (общий builder, лимит 11000) даёт
    # именно тот запрос, который production реально отправлял и получал 400
    # (длина растёт при любом росте constraints - см. docstring теста; сам
    # факт "> 6000" и есть баг, а не конкретное число).
    old_request = build_provider_generation_request(spec)
    assert len(old_request.source_text) > _CONTENT_FACTORY_MAX_SOURCE_TEXT_LENGTH

    # Новый способ укладывается в реальный лимит Content Factory.
    fixed_request = build_source_analysis_provider_request(spec)
    assert len(fixed_request.source_text) <= _CONTENT_FACTORY_MAX_SOURCE_TEXT_LENGTH
    assert len(fixed_request.source_text) <= SOURCE_ANALYSIS_REQUEST_LIMIT

    text = fixed_request.source_text
    # Все обязательные факты кейса сохранены (через key_facts, который никогда
    # не обрезается).
    assert "47 т.р. вместо 113 на Букинге" in text
    assert "трансфер был в 3 раза дешевле" in text
    assert "записаны в аккаунт как гости-пассажиры" in text
    assert "отсутствие баллов, партнерства, взносов" in text
    # Attribution/scope constraints сохранены.
    assert "по словам автора" in text
    assert "универсальное обещание" in text
    # Trusted context присутствует (пусть и пустой/ограниченный для этого workspace).
    assert "TRUSTED BUSINESS CONTEXT - DATA" in text


# --- signal -> post fail-closed gate (requirement 7: the summary must
# carry NEW, non-promotional information - long raw length alone, whether
# from a padded title or a promotional summary, must not be enough) ---

def test_source_content_is_sufficient_rejects_empty_and_near_empty_summary():
    assert source_content_is_sufficient("Заголовок", "") is False
    assert source_content_is_sufficient("Заголовок", "   ") is False
    assert source_content_is_sufficient("Заголовок", "Коротко") is False


def test_source_content_is_sufficient_rejects_just_under_the_threshold():
    summary = "x" * (MIN_USEFUL_SUMMARY_LENGTH - 1)
    assert source_content_is_sufficient("Заголовок", summary) is False


def test_source_content_is_sufficient_accepts_a_real_summary():
    summary = (
        "В Тбилиси спрос на туры вырос на 30% за последний месяц, "
        "путешественники бронируют туры на ноябрьские праздники."
    )
    assert source_content_is_sufficient("Спрос на туры в Грузию", summary) is True


def test_source_content_is_sufficient_rejects_the_live_tripster_signal():
    """Live production example: 243-character summary that formally clears
    any plain length threshold, but is actually just the title repeated
    plus a promo CTA for the source's own brand ("Трипстере" - Cyrillic
    transliteration of "Tripster") - no concrete fact for a standalone
    post survives removing both."""
    title = (
        "Пока одни достают осенние свитера и куртки, другие достают "
        "загранпаспорт..."
    )
    summary = (
        "Пока одни достают осенние свитера и куртки, другие достают "
        "загранпаспорт. У каждого свой способ справляться с окончанием "
        "лета.\n\nГлавное, что и те, и другие, всегда могут найти местного "
        "гида на Трипстере. В соседнем районе или в другой стране \U0001F438"
    )
    assert len(summary) == 243
    assert source_content_is_sufficient(title, summary, "Tripster") is False


def test_source_content_is_sufficient_rejects_live_signal_22378_with_channel_prefixed_source_name():
    """Bug fix: production stores source_name as "Telegram Tripster" (the
    ingestion channel prepended to the brand), not bare "Tripster" - live
    signal 22378 wrongly returned True because the un-stripped "telegram
    tripster" phrase never matched the brand-only mention "Трипстере" in
    the summary. Same title/summary as the test above, but with the real
    production source_name."""
    title = (
        "Пока одни достают осенние свитера и куртки, другие достают "
        "загранпаспорт..."
    )
    summary = (
        "Пока одни достают осенние свитера и куртки, другие достают "
        "загранпаспорт. У каждого свой способ справляться с окончанием "
        "лета.\n\nГлавное, что и те, и другие, всегда могут найти местного "
        "гида на Трипстере. В соседнем районе или в другой стране \U0001F438"
    )
    assert source_content_is_sufficient(title, summary, "Telegram Tripster") is False


def test_source_content_is_sufficient_a_long_title_cannot_rescue_a_thin_summary():
    """Bug fix #1: a long Tripster-style title must not pad a thin/empty
    summary past the threshold - the gate must judge the summary's own
    (non-title-repeat, non-promotional) content."""
    long_title = (
        "Пока одни достают осенние свитера и куртки, другие достают "
        "загранпаспорт..."
    )
    assert len(long_title) >= MIN_USEFUL_SUMMARY_LENGTH
    assert source_content_is_sufficient(long_title, "") is False
    assert source_content_is_sufficient(long_title, "Скидки.") is False


# --- signal -> post: Artifact title must never be a raw, possibly-truncated
# signal title (requirement 2) ---

def test_safe_material_title_keeps_a_complete_title():
    assert safe_material_title(
        "Раннее бронирование туров в Турцию", fallback="Материал по сигналу",
    ) == "Раннее бронирование туров в Турцию"


def test_safe_material_title_falls_back_for_truncated_title():
    truncated = (
        "Пока одни достают осенние свитера и куртки, другие достают "
        "загранпаспорт..."
    )
    assert safe_material_title(truncated, fallback="Материал по сигналу") == "Материал по сигналу"
    assert safe_material_title("Обрывается на полуслове…", fallback="F") == "F"


def test_safe_material_title_falls_back_for_empty_title():
    assert safe_material_title("", fallback="Материал по сигналу") == "Материал по сигналу"
    assert safe_material_title("   ", fallback="Материал по сигналу") == "Материал по сигналу"


# --- Quality gate fix (live Vietnam example): a summary can be long,
# non-title-repeat and non-promotional and STILL carry zero usable fact -
# pure generic travel-marketing description naming nothing specific.
# source_content_is_sufficient must reject it (content sufficiency, not
# post-generation text cleanup) while still accepting a genuinely concrete
# signal (Phuket-style travel warning with regions/levels/numbers). ---

def test_source_content_is_sufficient_rejects_the_live_vietnam_signal():
    """Live example: 'Вьетнам снова попал в туристическую повестку: The
    Times опубликовало подборку самых красивых и аутентичных мест страны.'
    -> a post that can only discuss the article ('список в пересказе не
    приведён') instead of being one. No digit, no named place/rule/event -
    just enthusiasm about an unspecified list."""
    title = "Вьетнам снова в туристической повестке"
    summary = (
        "Опубликована подборка самых красивых и аутентичных мест страны. "
        "Отдельно отмечены живописная природа и старинные исторические "
        "центры, а список конкретных объектов в публикации не приведён."
    )
    assert len(summary) >= MIN_USEFUL_SUMMARY_LENGTH
    assert source_content_is_sufficient(title, summary, "Турправда") is False


def test_source_content_is_sufficient_accepts_the_live_phuket_signal():
    """Same shape of check must still let a genuinely concrete signal
    through: specific regions, a numeric warning level, and a concrete
    rule/time - exactly what the Vietnam example lacked."""
    title = "МИД повысил уровень опасности для туристов на Пхукете"
    summary = (
        "Уровень опасности повышен до 3 из 5 для Пхукета, Краби и Самуи. "
        "Туристам рекомендуется избегать протестных районов и соблюдать "
        "комендантский час после 22:00."
    )
    assert source_content_is_sufficient(title, summary, "Турправда") is True


# Live production case (radar_signal_id=1287): the Vietnam signal actually
# ingested by the source is NOT the fully-generic no-places version above -
# it names three real resort towns, just in a paragraph SEPARATE from (and
# unrelated to) the unlisted The Times rating. source_content_is_sufficient
# must accept this signal (concrete place names ARE present - see
# _has_mid_sentence_capitalized_word) - the fix for this live bug is at the
# generation-prompt level (see material_orchestration._RADAR_CONSTRAINTS'
# unsupported-attribution rule), not at this content-sufficiency gate: the
# gate is correct here, the earlier "list of beautiful places" no-details
# test above is a genuinely different (fully generic) signal, not the same
# production case under a different name.
def test_source_content_is_sufficient_accepts_the_live_vietnam_signal_1287():
    title = "Вьетнам снова в туристической повестке"
    summary = (
        "The Times опубликовало рейтинг самых красивых и аутентичных мест "
        "Вьетнама. Конкретные пункты рейтинга в доступном пересказе не "
        "перечислены.\n\n"
        "Отдельно эксперт Турпрома составил путеводитель по экскурсиям для "
        "туристов, отдыхающих на пляжах Нячанга, Муйне или Фукуока."
    )
    assert source_content_is_sufficient(title, summary, "Турправда") is True


def test_source_content_is_sufficient_rejects_generic_filler_even_without_a_brand_mention():
    """The gate must catch generic-description-only content on its own
    concreteness signal (digit / mid-sentence proper noun), not rely on
    the source-name/brand-paragraph removal to accidentally clear it."""
    title = "Новое направление привлекает туристов"
    summary = (
        "Направление вошло в подборку самых популярных мест сезона. "
        "Путешественники отмечают удобство и разнообразие впечатлений."
    )
    assert source_content_is_sufficient(title, summary) is False


def test_source_content_is_sufficient_a_digit_alone_is_enough_concreteness():
    title = "Цены на отдых меняются"
    summary = (
        "Стоимость размещения выросла на 12% за последний месяц по "
        "сравнению с прошлым сезоном для похожих направлений."
    )
    assert source_content_is_sufficient(title, summary) is True


def test_source_content_is_sufficient_a_named_place_alone_is_enough_concreteness():
    title = "Новые прямые рейсы"
    summary = (
        "Авиакомпания запустила прямые рейсы из Москвы в Дананг впервые "
        "за несколько лет, что должно упростить поездки туда."
    )
    assert source_content_is_sufficient(title, summary) is True


# --- Fix 2: build_client_reply_provider_request (client-reply-only) ---
#
# Live prod bug (client-reply, follow-up to commit 4aa294e): a REAL (not
# test-fixture-sized) Business Profile alone pushes _build_prefix() past
# 6000 chars for a client-reply spec. build_provider_generation_request()
# then falls back to its raw [:limit] slice (see the investigation tests
# above) - since CONSTRAINTS is the LAST section of the prefix and the
# client's own message comes even later (after the whole prefix, via
# _MARKER), that raw slice silently dropped the OTA/inventory/always-
# cheaper/default-CTA bans from 4aa294e and the client's message itself.

_LIVE_PROD_CLIENT_QUESTION = (
    "А зачем мне Travel Advantage, если на Trip.com всё проще и можно "
    "оплатить российской картой?"
)


def _client_reply_spec(**overrides) -> "GenerationSpec":
    kwargs = dict(
        constraints=(
            "Черновик требует ручной проверки перед отправкой.",
            "Реальное преимущество конкурента не оспаривай и не "
            "обесценивай — сначала коротко признай (например: «Да, у "
            "Trip.com действительно проще оплата»). Не утверждай, что "
            "Travel Advantage всегда дешевле. Без жаргона «inventory», "
            "«OTA», «ecosystem», «pipeline», «provider». Не добавляй в "
            "конец шаблонное «сообщите даты — подберу», если это не "
            "вытекает из вопроса.",
        ),
        untrusted_source_content=_LIVE_PROD_CLIENT_QUESTION,
    )
    kwargs.update(overrides)
    spec = _spec(**kwargs)
    return replace(spec, artifact_type="client_message")


def _huge_secondary_context() -> dict:
    return {f"fact_{i}": "Подробный проверенный факт " * 20 for i in range(20)}


def _claim(text: str, *, verification_status: str = "verified") -> dict:
    return {
        "text": text, "verification_status": verification_status,
        "evidence_reference": None,
    }


def test_client_reply_packer_matches_generic_builder_when_everything_fits():
    """Common case (small profile, well under budget): identical output to
    the generic builder - this fix must not change normal-case behavior,
    only the overflow path."""
    spec = _client_reply_spec(
        trusted_business_context={"name": "Travel Business"},
        verified_claims_allowed=(_claim("Verified claim"),),
    )
    via_generic = build_provider_generation_request(spec, limit=6000)
    via_client_reply = build_client_reply_provider_request(spec, limit=6000)
    assert via_client_reply == via_generic


def test_client_reply_packer_keeps_message_and_constraints_under_budget_pressure():
    """The core fix: even when secondary context alone would overflow the
    generic builder into its raw [:limit] fallback, OBJECTIVE, CONSTRAINTS
    and the client's full message must survive intact, and the result must
    still respect the limit."""
    spec = _client_reply_spec(
        trusted_business_context=_huge_secondary_context(),
        source_facts=_huge_secondary_context(),
        verified_claims_allowed=tuple(
            _claim("Verified fact " * 10 + str(i)) for i in range(20)
        ),
        unverified_claims_requiring_caution=tuple(
            _claim("Unverified fact " * 10 + str(i), verification_status="unverified")
            for i in range(20)
        ),
    )
    # Prove this scenario really does overflow the generic builder's
    # protected-content guarantee first, otherwise this test proves nothing:
    # at limit=6000 the generic raw-slice fallback already drops the
    # client's message and/or part of CONSTRAINTS.
    generic = build_provider_generation_request(spec, limit=6000)
    assert (
        _LIVE_PROD_CLIENT_QUESTION not in generic.source_text
        or any(c not in generic.source_text for c in spec.constraints)
    )

    request = build_client_reply_provider_request(spec, limit=6000)
    assert len(request.source_text) <= 6000
    assert _LIVE_PROD_CLIENT_QUESTION in request.source_text
    for constraint in spec.constraints:
        assert constraint in request.source_text
    assert spec.objective in request.source_text


def test_client_reply_packer_drops_lowest_priority_secondary_sections_first():
    """SOURCE FACTS (priority 7) must be dropped before TRUSTED BUSINESS
    CONTEXT (priority 5) when both can't fit - per this fix's declared
    priority order (4: personal style/tone, 5: trusted business context,
    6: verified claims, 7: source facts, 8: audience/unverified claims)."""
    spec = _client_reply_spec(
        trusted_business_context={"positioning": "Компактный, но узнаваемый бизнес-контекст."},
        source_facts=_huge_secondary_context(),
    )
    request = build_client_reply_provider_request(spec, limit=6000)
    assert "[TRUSTED BUSINESS CONTEXT - DATA]" in request.source_text
    assert "[SOURCE FACTS - DATA]" not in request.source_text


def test_client_reply_packer_never_raises_for_an_oversized_spec():
    """Unlike build_source_analysis_provider_request's fail-closed
    SourceAnalysisRequestTooLargeError, a client-reply draft must always be
    produced - the caller has a real person waiting on an answer."""
    spec = _client_reply_spec(
        trusted_business_context=_huge_secondary_context(),
        source_facts=_huge_secondary_context(),
        untrusted_source_content="Очень длинное сообщение клиента. " * 300,
    )
    request = build_client_reply_provider_request(spec, limit=6000)
    assert len(request.source_text) <= 6000


def test_client_reply_packer_truncates_message_only_as_absolute_last_resort():
    """If OBJECTIVE+CONSTRAINTS+the client's message alone can't fit even
    with every secondary section dropped (a pathologically long pasted
    client message, not a large Business Profile), the message - never
    OBJECTIVE/CONSTRAINTS - is what gets trimmed, using the same shrink
    loop the generic builder already uses for source content."""
    spec = _client_reply_spec(untrusted_source_content="Очень длинное сообщение клиента. " * 300)
    request = build_client_reply_provider_request(spec, limit=1500)
    assert len(request.source_text) <= 1500
    for constraint in spec.constraints:
        assert constraint in request.source_text
    assert spec.objective in request.source_text


def test_client_reply_packer_rejects_a_non_positive_limit():
    with pytest.raises(ValueError):
        build_client_reply_provider_request(_client_reply_spec(), limit=0)
