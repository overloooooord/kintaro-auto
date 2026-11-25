# kintaro-auto

Конвейер автоматического прогрева, уникализации медиа и залива видео/каруселей на твои аккаунты через **Kintaro API v1** (`https://service.kintaroapi.com`).

## Что закрывает защита от теневых банов (клиентская сторона)

1. **Жёсткая изоляция `1 аккаунт = 1 x-device = 1 постоянный x-proxy = 1 region`** — слепок устройства сохраняется атомарно в `state/accounts/<id>.json` после `POST /v1/device/register` и обновляется при каждом ответе сервера (`signin`, `update_hosts`, `token_beat`).
2. **Жизненный цикл сессии и отлёжка** — `update_hosts` + `token_beat`, блокировка мгновенного залива в первые минуты после первичного логина (`min_session_age_minutes_before_first_post`), контроль интервала между постами и дневного лимита.
3. **Органический прогрев перед публикацией (`warmup`)** — чтение ленты `GET /v1/feed/recommendations`, отправка `POST /v1/post/view` с реалистичным `playtime` в миллисекундах, вероятностное чтение комментариев, редкие лайки и подписки со случайными паузами.
4. **Аппаратная уникализация каждого крео перед отправкой (`ffmpeg` + `Pillow`)**:
   - **Видео (`mp4` до 50 МБ):** полная зачистка метаданных (`-map_metadata -1`), микро-кроп с возвратом исходного чётного разрешения (`lanczos`), незаметный сдвиг яркости/контраста/насыщенности, микро-джиттер скорости видео и аудио (`setpts` + `atempo`), случайный `CRF`, свежий `creation_time`.
   - **Фото (`jpg`/`png`, до 10 слайдов по 10 МБ):** удаление EXIF/ICC профилей, микро-кроп, лёгкий цветовой сдвиг и мутация отдельных пикселей для смены хеша.
5. **Уникализация текста и валидация упоминаний** — поддержка спинтакса `{Вариант А|Вариант Б}` и автоматическая постановка обязательного пробела после каждого `@username ` согласно спецификации Kintaro API (`description_mentions`).
6. **Мониторинг теней (`check-shadows`)** — сверка `GET /v1/account/posts` и публичного `GET /v1/post/get` с проверкой нулевых просмотров спустя заданное время.
7. **Мгновенный HTTP-аудит (`audit`) и 3-канальное снятие огранов (`appeal` / `unban`)** — за `0$` Kintaro без лишних запусков браузера опрашивает региональный мобильный шлюз кластера аккаунта (`api16-normal-no1a.tiktokv.eu` и др.) и TikTok Studio по кукам сессии:
   - **Мобильный шлюз уведомлений (`group=47/661`, `/aweme/v1/notice/multi/` + `/api/violation/v1/get_violation_detail`):** достаёт точный статус (`Effecting`, `Appealing`, `AppealSuccess`, `AppealFailed`), `AppealId`, источник `bric_social`, проверяет плашку `Update your account region` и подаёт апелляцию каскадом из 3 каналов: `POST /api/violation/v1/commit_appeal` → `POST /aweme/v2/appeal/commit/` → фоллбэк в headless Chrome `BytedanceWebview` (`POST /tns-api/appeal/post-commit`).
   - **Постовые ограны в TikTok Studio (`/tiktok/creator/manage/item_list/v1/`):** проверяет до 50 последних постов через `/mod/v1/getPenaltyDetails/` и `/api/moderation/penalties/gettransparencypagecontent`, снимает флаги `For You feed` с человеческой паузой `8.5–17.5с` между постами и по флагу `--hide-rejected` автоматически переводит отклонённые (`FAILED`) посты в `Private`, чтобы не словить 30-дневный бан `punish_type=2048` на весь аккаунт.

> **Важно:** ни один скрипт на стороне клиента не может гарантировать 0% теней, если используются грязные датацентровые прокси (не совпадающие по GEO с `region`), спам-крео под фильтр модерации или убитые аккаунты. Инструмент полностью убирает технические триггеры со стороны API, сессий и дублей медиа.

## Быстрый старт

```bash
cp config.example.toml config.toml
cp accounts.example.json accounts.json
cp queue.example.json queue.json
```

1. Впиши свой ключ `x-api-key` в `config.toml` (`api.api_key`) или экспортируй `export KINTARO_API_KEY="..."`.
2. Заполни `accounts.json` своими аккаунтами, прокси (`protocol://user:pass@host:port`) и двухбуквенным `region` под GEO прокси.
   - Если у тебя уже есть готовый JSON сессии `x-device` от Kintaro — укажи `"auth_method": "device_only"` и вставь объект в `"device"`.
   - Если входишь по логину/почте/телефону — укажи `"username"` / `"email"` / `"mobile"`, `"password"` и при наличии 2FA секрет `"totp_token"`.

## Команды

```bash
# Импорт аккаунтов из JSON в state/accounts/
uv run kintaro-auto import-accounts accounts.json

# Добавление одной строки: поддерживается и `username|password|email|totp` (TikTok 2FA),
# и Outlook-Hub формат `login:pass:outlook_email:outlook_pass:rec_email:rec_pass:hotmail_2fa`
uv run kintaro-auto add-line "user|pass|mail@example.com|TOTPSECRET" -p "socks5://user:pass@host:port" -r US

# Массовый импорт .txt пачки аккаунтов (оба формата)
uv run kintaro-auto import-lines accounts.txt -p "socks5://user:pass@host:port" -r US

# Всё-в-одном: подключить Outlook Hub / TOTP -> войти в TikTok -> сразу снять все ограны с аккаунта и постов
uv run kintaro-auto unban "login:pass:mail@outlook.com:mail_pass:rec@polosmail.com:rec_pass:HOTMAIL2FA" -p "socks5://user:pass@host:port"
uv run kintaro-auto unban accounts.txt -p "socks5://user:pass@host:port"

# Отдельная проверка почты и чтение 6-значного кода через встроенный Outlook Hub (Microsoft Graph API)
uv run kintaro-auto outlook-code "login:pass:mail@outlook.com:mail_pass:rec@polosmail.com:rec_pass:HOTMAIL2FA" --wait-new

# Импорт готовой папки сессий x-device (*.json)
uv run kintaro-auto import-sessions /path/to/sessions -p "socks5://user:pass@host:port" -r US

# Авторизация (при HTTP 403 спросит код из почты/SMS прямо в консоли)
uv run kintaro-auto auth

# Если запускал с --no-interactive и получил 403 challenge:
uv run kintaro-auto confirm-code acc_01 123456

# Сводная таблица статусов и кулдаунов
uv run kintaro-auto status

# Отдельный прогрев всех аккаунтов
uv run kintaro-auto warmup

# Разовый залив видео (с автопрогревом и уникализацией)
uv run kintaro-auto upload-video media/clip1.mp4 -d "{Зацени|Смотри до конца} #fyp"

# Разовый залив фото-карусели с автопоиском музыки
uv run kintaro-auto upload-photo slide1.jpg slide2.jpg -d "Карусель дня" --music-search "phonk"

# Полный автоконвейер по очереди задач (с ожиданием кулдаунов в фоне)
uv run kintaro-auto run-queue queue.json --daemon

# Проверка залитых постов на 0 просмотров / скрытие
uv run kintaro-auto check-shadows

# Мгновенный HTTP-аудит всех сессий (RegCountry, CurrentRegion, bric_social, group=47/661 и посты в Studio)
uv run kintaro-auto audit

# Разовый прогон 3-канального снятия огранов + авто-перевод отклонённых (FAILED) постов в Private
uv run kintaro-auto appeal --hide-rejected

# Бесконечный фоновый сторож по кукам (анти-фрод рандом 95–165 сек между циклами без повторных логинов)
uv run kintaro-auto appeal --watch --hide-rejected

# Локальный тест уникализатора медиа без API-ключа
uv run kintaro-auto uniquify-test media/clip1.mp4
```
