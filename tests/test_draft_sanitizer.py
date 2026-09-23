from __future__ import annotations

from app.services.draft_sanitizer import sanitize_draft_text


# --- ассистентские концовки (только в конце текста) ---

def test_trailing_assistant_ending_is_removed():
    text = (
        "Отличная тема для поста про Карелию. "
        "Съездить туда стоит каждому. "
        "Могу сравнить варианты поездки в Карелию."
    )
    result = sanitize_draft_text(text)
    assert "Могу сравнить варианты" not in result
    assert "Съездить туда стоит каждому." in result


def test_trailing_if_you_want_ending_is_removed():
    text = "Карелия — отличное направление для осени. Если хотите, могу подготовить подборку маршрутов."
    result = sanitize_draft_text(text)
    assert "Если хотите" not in result
    assert "Карелия — отличное направление для осени." in result


def test_assistant_ending_word_in_the_middle_is_not_touched():
    # "могу" не в конце — это не паттерн ассистентской концовки, резать нельзя.
    text = "Я могу долго рассказывать про Карелию, но вот главное: маршрут начинается от Питера."
    result = sanitize_draft_text(text)
    assert result == text


# --- внутренние мета-фразы о процессе (в любом месте текста) ---

def test_meta_process_phrase_sentence_is_removed():
    text = "Цены на туры выросли. Эту деталь лучше перепроверить отдельно. Планируйте бюджет заранее."
    result = sanitize_draft_text(text)
    assert "перепроверить" not in result
    assert "Цены на туры выросли." in result
    assert "Планируйте бюджет заранее." in result


# --- signal/post quality fix: draft discussing the signal instead of being
# the post ("в этом сигнале цепляет...") ---

def test_discusses_the_signal_phrase_is_removed():
    text = (
        "Осень — время собирать чемоданы, а не свитера. "
        "В этом сигнале цепляет контраст между сезонами. "
        "Уже сейчас можно забронировать перелёт по низкой цене."
    )
    result = sanitize_draft_text(text)
    assert "в этом сигнале" not in result.lower()
    assert "Осень — время собирать чемоданы, а не свитера." in result
    assert "Уже сейчас можно забронировать перелёт по низкой цене." in result


def test_source_reports_phrase_is_removed():
    text = "Источник сообщает о росте спроса на зимние туры. Билеты стоит бронировать заранее."
    result = sanitize_draft_text(text)
    assert "источник сообщает" not in result.lower()
    assert "Билеты стоит бронировать заранее." in result


def test_meta_process_phrase_by_source_post_is_removed():
    text = "Новый маршрут открылся в этом сезоне. По исходному посту это ещё не подтверждено официально."
    result = sanitize_draft_text(text)
    assert "по исходному посту" not in result.lower()
    assert "Новый маршрут открылся в этом сезоне." in result


# --- обычный CTA не должен резаться ---

def test_normal_cta_with_the_word_mozhno_survives():
    text = "Съездить в Карелию можно уже этим летом. Бронируйте билеты заранее, пока цены не выросли."
    result = sanitize_draft_text(text)
    assert result == text


def test_normal_cta_with_the_word_khotite_mid_sentence_survives():
    text = "Если хотите увидеть петроглифы своими глазами — маршрут начинается от Беломорска."
    result = sanitize_draft_text(text)
    assert result == text


# --- disputed claims: fail-safe исключение из текста ---

def test_disputed_claim_sentence_is_removed():
    text = "Петроглифы в Карелии старше египетских пирамид. Место точно стоит увидеть своими глазами."
    result = sanitize_draft_text(
        text, disputed_claims=("Петроглифы старше египетских пирамид",)
    )
    assert "пирамид" not in result
    assert "Место точно стоит увидеть своими глазами." in result


def test_disputed_claim_paraphrase_is_also_removed():
    # Незначительный перифраз того же утверждения — high word-overlap, тоже режем.
    text = "Эти петроглифы старше, чем египетские пирамиды, между прочим. Место потрясающее."
    result = sanitize_draft_text(
        text, disputed_claims=("Петроглифы старше египетских пирамид",)
    )
    assert "пирамид" not in result
    assert "Место потрясающее." in result


def test_unrelated_sentence_is_not_removed_by_disputed_claim():
    text = "Петроглифы в Карелии старше египетских пирамид. Добраться можно поездом Арктика из Москвы."
    result = sanitize_draft_text(
        text, disputed_claims=("Петроглифы старше египетских пирамид",)
    )
    assert "Добраться можно поездом Арктика из Москвы." in result


def test_empty_disputed_claims_does_not_touch_text():
    text = "Петроглифы в Карелии старше египетских пирамид."
    assert sanitize_draft_text(text, disputed_claims=()) == text


# --- комбинация всех правил на реалистичном черновике ---

def test_combined_real_like_draft():
    text = (
        "Беломорские петроглифы старше египетских пирамид.\n"
        "Добраться можно поездом Арктика до Беломорска с остановкой в Питере.\n"
        "Эту деталь лучше перепроверить отдельно.\n"
        "Могу сравнить варианты поездки в Карелию."
    )
    result = sanitize_draft_text(
        text, disputed_claims=("петроглифы старше египетских пирамид",)
    )
    assert "пирамид" not in result
    assert "перепроверить" not in result
    assert "Могу сравнить" not in result
    assert "Добраться можно поездом Арктика до Беломорска с остановкой в Питере." in result


def test_empty_text_returns_empty_text():
    assert sanitize_draft_text("") == ""


# --- Quality fix: signal/competitor -> material contract regressions -----
#
# Production showed two leaks into a finished, publication-ready post:
# (1) internal meta-commentary about source reliability ("Остальное в
#     исходном тексте — шутка и личная оценка, на них лучше не опираться."),
#     phrased differently from the narrow pre-existing markers;
# (2) an AI-voiced trailing CTA ("Могу сравнить варианты...") that survived
#     because it was followed by a decoration-only line (hashtags), which
#     stopped the trailing-cut scan before it ever reached the AI sentence.

def test_source_reliability_commentary_is_removed() -> None:
    text = (
        "Раннее бронирование Турции подешевело на треть.\n"
        "Остальное в исходном тексте — шутка и личная оценка, на них лучше не опираться.\n"
        "Планируйте поездку заранее, пока действует цена."
    )
    result = sanitize_draft_text(text)
    assert "исходном тексте" not in result
    assert "лучше не опираться" not in result
    assert "Раннее бронирование Турции подешевело на треть." in result
    assert "Планируйте поездку заранее, пока действует цена." in result


def test_unconfirmed_internal_comment_is_removed() -> None:
    text = (
        "Цены на туры в Египет снизились в этом сезоне.\n"
        "Точную скидку не удалось подтвердить, поэтому в тексте её не будет.\n"
        "Уточняйте актуальные даты у менеджера."
    )
    result = sanitize_draft_text(text)
    assert "не удалось подтвердить" not in result.lower()
    assert "Цены на туры в Египет снизились в этом сезоне." in result
    assert "Уточняйте актуальные даты у менеджера." in result


def test_assistant_offer_hidden_behind_trailing_hashtags_is_still_removed() -> None:
    """The real production shape: an AI-voiced CTA followed by a hashtag
    line. The hashtags must survive; the AI CTA sentence right before them
    must not."""
    text = (
        "Раннее бронирование Турции подешевело на треть.\n"
        "Планируйте поездку заранее, пока действует цена.\n"
        "Могу сравнить варианты поездки, если нужно.\n"
        "#Турция #ОтпускМечты"
    )
    result = sanitize_draft_text(text)
    assert "Могу сравнить" not in result
    assert "#Турция #ОтпускМечты" in result
    assert "Раннее бронирование Турции подешевело на треть." in result


def test_assistant_offer_hidden_behind_trailing_emoji_is_still_removed() -> None:
    text = (
        "Новый маршрут открылся в этом сезоне.\n"
        "Могу подготовить сравнение маршрутов.\n"
        "🌴✈️"
    )
    result = sanitize_draft_text(text)
    assert "Могу подготовить" not in result
    assert "🌴✈️" in result
    assert "Новый маршрут открылся в этом сезоне." in result


def test_trailing_hashtags_alone_are_never_removed() -> None:
    """Decoration lines must never be stripped themselves - only skipped
    over while looking for a real AI-tail sentence behind them."""
    text = "Раннее бронирование Турции подешевело на треть.\n#Турция #ОтпускМечты"
    result = sanitize_draft_text(text)
    assert result == text


def test_real_content_sentence_after_hashtags_stops_the_scan() -> None:
    """Decoration-skipping must not turn into blanket trailing removal: a
    genuine content sentence anywhere in the trailing run still stops the
    cut, exactly as before."""
    text = (
        "Раннее бронирование Турции подешевело на треть.\n"
        "Бронируйте у нас — поможем выбрать даты.\n"
        "#Турция"
    )
    result = sanitize_draft_text(text)
    assert result == text


# --- Quality fix: extended AI self-offer forms (word-count cap removed) --
#
# Production showed "Если хотите, могу помочь сравнить варианты поездки в
# Японию по датам и погоде." surviving sanitization: it's structurally the
# same AI self-offer as the short form already covered above, just longer
# because of the added specifics ("по датам и погоде") - the old
# _MAX_ASSISTANT_ENDING_WORDS=10 word cap rejected it purely for being
# longer than 10 words, and "могу помочь" wasn't recognized as an offer verb
# at all (only "помогу", a different word form). Fixed with a narrow
# structural anchor (sentence must OPEN with "Могу"/"Если хотите, могу") -
# no long list of exact phrases, no length limit - while normal commercial
# CTAs (which never open a sentence with "Могу"/"Если хотите") are
# untouched regardless of length.

def test_extended_if_you_want_i_can_help_compare_offer_is_removed() -> None:
    text = (
        "Раннее бронирование Японии подешевело на треть.\n"
        "Планируйте поездку заранее, пока действует цена.\n"
        "Если хотите, могу помочь сравнить варианты поездки в Японию по датам и погоде."
    )
    result = sanitize_draft_text(text)
    assert "могу помочь" not in result.lower()
    assert "Раннее бронирование Японии подешевело на треть." in result
    assert "Планируйте поездку заранее, пока действует цена." in result


def test_i_can_prepare_a_selection_offer_is_removed() -> None:
    text = (
        "Новые направления открылись этой весной.\n"
        "Могу подготовить подборку вариантов под ваш бюджет и даты."
    )
    result = sanitize_draft_text(text)
    assert "могу подготовить" not in result.lower()
    assert "Новые направления открылись этой весной." in result


def test_write_to_us_we_will_pick_an_option_cta_survives() -> None:
    """Normal brand/agent CTA, addressed to the reader in "мы"/imperative
    voice, never opens with "Могу"/"Если хотите" - must never be cut."""
    text = "Пишите — подберём подходящий вариант."
    assert sanitize_draft_text(text) == text


def test_if_planning_a_trip_write_to_us_cta_survives() -> None:
    text = "Если планируете поездку — напишите."
    assert sanitize_draft_text(text) == text


def test_write_if_you_want_to_pick_a_trip_cta_survives() -> None:
    """"Если хотите" appears mid-sentence here, not at sentence start
    ("Напишите" opens it) - the trigger is anchored to sentence-start
    specifically so this normal CTA is never touched."""
    text = "Напишите, если хотите подобрать поездку."
    assert sanitize_draft_text(text) == text


def test_if_planning_japan_lets_discuss_dates_cta_survives() -> None:
    text = "Если планируете Японию — обсудим даты и маршрут."
    assert sanitize_draft_text(text) == text


# --- "Подготовить пост" quality fix: generic filler phrases removed ---

def test_generic_filler_phrase_this_is_a_great_reason_is_removed() -> None:
    text = (
        "Цены на билеты в Японию выросли на 15%. "
        "Это отличный повод забронировать поездку заранее. "
        "Планируйте бюджет с учётом новых тарифов."
    )
    result = sanitize_draft_text(text)
    assert "отличный повод" not in result.lower()
    assert "Цены на билеты в Японию выросли на 15%." in result
    assert "Планируйте бюджет с учётом новых тарифов." in result


def test_generic_filler_phrase_important_to_note_is_removed() -> None:
    text = "Важно отметить, что сезон скидок начался раньше обычного. Билеты уже подешевели на треть."
    result = sanitize_draft_text(text)
    assert "важно отметить" not in result.lower()
    assert "Билеты уже подешевели на треть." in result


def test_generic_filler_phrase_in_the_middle_is_removed() -> None:
    text = (
        "Раннее бронирование Бали подешевело. "
        "Стоит отметить, что это происходит впервые за два года. "
        "Планируйте поездку на ноябрь."
    )
    result = sanitize_draft_text(text)
    assert "стоит отметить" not in result.lower()
    assert "Раннее бронирование Бали подешевело." in result
    assert "Планируйте поездку на ноябрь." in result
