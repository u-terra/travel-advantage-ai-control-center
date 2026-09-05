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
