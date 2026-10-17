# Travel Advantage — official enrollment/checkout page (observed 2026-10-05)

Статус: сведения подтверждены владельцем как `verified_official`, наблюдение с официальной
live-страницы enrollment/checkout Travel Advantage на дату 2026-10-05. Это не выгруженный PDF
Compensation Plan — только то, что отображает текущий checkout-интерфейс на указанную дату.
Данный документ включает дополнительную live-проверку, полученную после первого наблюдения
(уточнение по VIP 180 и по полям Total Due Today).

## VIP

Monthly Plan. One-Time Activation: $20. Membership: $19.97. Total Due Today (как отдельное
поле на checkout): $19.97. Поле Total Due Today отображается равным membership fee, то есть
отдельно показанная строка activation $20 в это поле видимо не включена — интерфейс в этом
смысле внутренне противоречив (неясно, списывается ли activation fee сегодня или нет).
Отдельного подтверждённого факта о фактическом первом списании VIP на данный момент нет;
ниже зафиксированы только буквально отображаемые значения полей, без самостоятельного
расчёта итоговой суммы.

## VIP 180

VIP 180 АКТИВЕН на текущем live-checkout. One-Time Activation: $20. Membership: $99.00 /
6 months. Total Due Today (как отдельное поле на checkout): $99. Та же внутренняя
неоднозначность между отдельно показанной activation-строкой и полем Total Due Today, что
и у VIP monthly (см. выше) — буквально отображаемые значения зафиксированы отдельно, итоговая
сумма самостоятельно не рассчитывается.

ОБНОВЛЕНИЕ ОТНОСИТЕЛЬНО РАНЕЕ ЗАФИКСИРОВАННОГО ПРОТИВОРЕЧИЯ: источник
`ta.source.partner_training_delta.2026_08` (partner-training-official-delta-2026-08.md,
раздел "Compliance and current information") утверждает, что "VIP180 и Elite180 являются
legacy variants и не используются в текущей рабочей модели". Эта дополнительная live-проверка
2026-10-05 подтверждает, что VIP 180 сейчас АКТИВЕН на официальном checkout — то есть
утверждение источника partner_training_delta о VIP180 следует считать устаревшим
(superseded) относительно текущего live-состояния. Это наблюдение НЕ переверифицирует статус
Elite180 — про Elite180 отдельного подтверждения в рамках этой проверки нет.

## Elite

Monthly Plan. Membership: $119.97/month. One-time activation: $120. Total due today
(как отображается на checkout): $239.97.

## Turbo Travel Advantage

$249.97, one-time. Представлен на checkout как Turbo Upgrade / add-on, предлагаемый Elite
Members. Это не отдельный самостоятельный ежемесячный membership plan — это разовая
надстройка/upgrade к существующему Elite membership. Ранее импортированный факт
`ta.membership.elite_turbo.total_price.usd` (598.97) описывал Elite Turbo как отдельный
тариф с общей стоимостью — это описание не соответствует текущему live-состоянию (Turbo —
addon, а не отдельный план) и должно считаться неактуальным (superseded) относительно этой
проверки.

## Cancellation / Refund

Membership можно отменить. При отмене в течение 14 дней после activation возможен полный
refund. Если в течение этого 14-дневного периода пользователь совершил booking или purchase
через Travel Advantage, право на этот refund теряется.
