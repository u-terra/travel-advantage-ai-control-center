# Help PDF — screenshot plan (post beta-scope rewrite)

Companion shot list for a future PDF export of `/help`. This replaces no
existing document — `docs/SCREENSHOTS_CHECKLIST.md` is a separate, older
plan for Telegram-bot portfolio screenshots and is unrelated.

For each shot: what must be visible, where the callout arrow points, and
the one sentence it should explain to the reader.

| # | Screen | What must be visible | Arrow points to | Explains |
|---|--------|----------------------|------------------|----------|
| S01 | Главный кабинет | Sidebar with Ассистент / Сигналы / Конкуренты / Материалы / История / Профиль / Подписка / Помощь; empty chat state | Sidebar nav as a whole | This is the home base — one place for every task. |
| S02 | Профиль | Business-type field (партнёр TA / турагент / турагентство / экскурсовод), style/tone field | The business-type selector | Filling this in makes every answer aware of how you work. |
| S03 | «Мой стиль» | The voice-sample textarea in Profile, with a pasted demo paragraph and the "Сохранить стиль" button | The textarea and the save button | Pasting a few paragraphs is all it takes — no prompt needed. |
| S04 | Поле Ассистента | Chat input with a real example question typed in (e.g. "Какие направления предложить семье с ребёнком?") | The input field | Just ask in plain words, like messaging a colleague. |
| S05 | Готовый ответ | Assistant's reply rendered in the chat thread | The reply bubble | This is the kind of ready-to-send answer you get back. |
| S06 | Загрузка файла | Paperclip icon next to the input, file picker open | The paperclip icon | Click here to attach a booking PDF, screenshot, or doc. |
| S07 | Файл прикреплён | Attached file chip shown next to the outgoing message | The file chip | Once it appears next to your message, it's been read. |
| S08 | «Сигналы и идеи» | List of signal cards with source + freshness date on each | The source/date on one card | Every signal shows where it came from and how fresh it is. |
| S09 | Кнопка «Подготовить пост» | A signal card with the "Подготовить пост" / "Сообщение клиентам" buttons visible | Those buttons | One click turns a signal into a draft — no manual copy-paste. |
| S10 | Созданный материал | The resulting material open in "Материалы", written in the user's saved style | The saved-style indicator / tone of the text | The draft already sounds like the user, automatically. |
| S11 | Конкуренты | A competitor card with an analysis date and a freshness note | The freshness date/badge | Shows exactly how recent the competitor data is. |
| S12 | Действие из анализа конкурента | The "Что можно сделать" block under a competitor report, with its action buttons | The "Что можно сделать" block | Turns a competitor finding directly into a post or message. |
| S13 | История | List of past conversations/materials | A past entry | Nothing is lost — every past result is easy to find again. |
| S14 | Подписка | Billing page showing plan status and price pulled from account state (no price hardcoded in this guide) | The status/price area | One subscription covers both Web and Telegram. |
| S15 | Help (desktop) | Full `/help` page: sidebar TOC + hero with the "Увидел → понял → предложил → сделал" formula | The formula banner | The whole product in one sentence. |
| S16 | Help (mobile) | Mobile `/help` view with the hamburger TOC toggle open | The hamburger button | Same guide, adapted for a phone screen. |

## Notes for whoever shoots these

- Use a demo/test workspace — never a real partner's live data or texts.
- S03's pasted sample must be an obviously generic demo paragraph (per
  `help.html`'s own disclaimer: examples are anonymized, not anyone's
  real posts).
- S14 must never show a hardcoded number baked into this plan or the PDF
  copy — capture whatever the account's actual billing state shows at
  shoot time.
