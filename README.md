# kintaro-auto

Конвейер автоматического прогрева, уникализации медиа и залива видео/каруселей на твои аккаунты через **Kintaro API v1** (`https://service.kintaroapi.com`).

## Что закрывает защита от теневых банов (клиентская сторона)

1. **Жёсткая изоляция `1 аккаунт = 1 x-device = 1 постоянный x-proxy = 1 region`** — слепок устройства сохраняется атомарно в `state/accounts/<id>.json` после `POST /v1/device/register` и обновляется при каждом ответе сервера (`signin`, `update_hosts`, `token_beat`).
2. **Жизненный цикл сессии и отлёжка** — `update_hosts` + `token_beat`, блокировка мгновенного залива в первые минуты после первичного логина (`min_session_age_minutes_before_first_post`), контроль интервала между постами и дневного лимита.
3. **Органический прогрев перед публикацией (`warmup`)** — чтение ленты `GET /v1/feed/recommendations`, отправка `POST /v1/post/view` с реалистичным `playtime` в миллисекундах, вероятностное чтение комментариев, редкие лайки и подписки со случайными паузами.
4. **Аппаратная уникализация каждого крео перед отправкой (`ffmpeg` + 6-пресетный Pillow Anti-pHash / Anti-ViT движок)**:
   - **Видео (`mp4` до 50 МБ):** полная зачистка метаданных (`-map_metadata -1`), микро-кроп с возвратом исходного чётного разрешения (`lanczos`), незаметный сдвиг яркости/контраста/насыщенности, микро-джиттер скорости видео и аудио (`setpts` + `atempo`), случайный `CRF`, Android `mp42` профиль + pure-Python ISO-BMFF `free`-atom фоллбэк без `ffmpeg`.
   - **Фото (`jpg`/`png`/`webp`, до 10 слайдов по 10 МБ):** 6 пресетов — **`minimal_snow`** (дефолт под US-реки: микро-кроп `0.8–1.5% / 1.2–2.2%`, наклон `0.25°`, `115–175` снежинок, шум матрицы `1.2`, JPEG `90–95`), `minimal`, `stealth` (без снега), `medium`, `boosted` (3-слойный снег + боке + ветер), `ultra_hard` (3D-перспектива + плавающая карточка на размытом фоне + поворот Hue + световой блик + RGB-сдвиг).
5. **Уникализация текста, рандомизация тегов и валидация упоминаний** — спинтакс `{Вариант А|Вариант Б}`, `{RAND4}` / `{RAND6}`, уникальная перестановка порядка хэштегов на каждый пост (`randomize_hashtags`), и автоматический пробел после каждого `@username ` (`description_mentions`).
6. **Холодный перезапуск TikTok (`restart-app`) и 5-поточный анти-теневой пролив (`campaign`)**:
   - Автоочистка старых постов (`clean-posts`), пул уникальных треков под ГЕО (`build_music_pool`, например `Fortnite`), пауза `14–20с`, холодный перезапуск приложения каждые `N` постов (ротация `session_ids.session` + `new_session` + `update_hosts` + `device/report(launch)` + `token_beat` + `account/self`) и **3-этапный Auto-Appeal 2048** (до залива, после каждого поста, пост-заливная ловля волн до 60с тишины + автосортировка в `state/sorted_accounts/{clean,ogran_na_proverke,ogran_ne_snyat,errors}/`).
   - Поддержка **Outlook Hub** (Microsoft Graph API + 2FA + recovery IMAP) **и** классического **IMAP** (Firstmail, Rambler, Mail.ru, Yandex, GMX, NotLetters) с параллельным запуском почты + `/v1/device/register`.
   - 21 SIM/GEO профиль (`US, GB, CA, AU, IT, DE, FR, ES, BR, PT, MX, CL, AR, CO, PE, PL, KZ, UA, PH, ID, VN`) + опциональный sticky-pinning на `bpproxy.at`.
7. **Мониторинг теней (`check-shadows`), Competitor Spy (`spy`) и живой TOTP (`totp`)** — сверка `GET /v1/account/posts` и публичного `GET /v1/post/get`, анализ охватов/хэштегов/вирусных Music ID конкурентов и RFC 6238 генератор 6-значных 2FA кодов.
8. **Мгновенный HTTP-аудит (`audit`) и 3-канальное снятие огранов (`appeal` / `unban`)** — за `0$` Kintaro без лишних запусков браузера опрашивает региональный мобильный шлюз кластера аккаунта (`no1a`, `alisg`, `useast1a/2a/5`) и TikTok Studio по кукам сессии:
   - **Мобильный шлюз уведомлений (`group=47/661`, `/aweme/v1/notice/multi/` + `/api/violation/v1/get_violation_detail`):** достаёт все `0..10+` distinct `punish_id` (дедупликация строго по `punish_id`), точный статус (`Effecting`, `Appealing`, `AppealSuccess`, `AppealFailed`), `AppealId`, источник `bric_social` и подаёт апелляцию каскадом из 3 каналов: `POST /api/violation/v1/commit_appeal` → `POST /aweme/v2/appeal/commit/` → фоллбэк в headless Chrome `BytedanceWebview` (`POST /tns-api/appeal/post-commit`) + мгновенная перепроверка авто-одобрения.
   - **Постовые ограны в TikTok Studio:** отключены по умолчанию (фокус на `2048 Account ineligible for recommendation`); включаются опционально через `--include-studio`.

9. **Веб-интерфейс KINTARO FARM OS v4.0 (`kintaro-auto web`)** — локальный центр управления на FastAPI + Live SSE (`http://127.0.0.1:8080`):
   - **Студия Анти-Тень Кампаний:** 5-поточный залив `6×` постов, выбор пресета уникализатора (`minimal_snow`..`ultra_hard`) + **живой предпросмотр До / После**, рандом хэштегов, авто-музыка, рестарт TikTok каждые N постов, 3-этапный Auto-Appeal 2048.
   - **Инспектор аккаунта:** холодный перезапуск ТТ, очистка старых постов, аудит/снятие волн 2048, смена прокси, редактор Nickname/Bio/Link, чтение Outlook OTP в 1 клик, экспорт `cookies.json` / `x-device.json`, таблица постов Studio.
   - **Competitor Spy + Live TOTP + Радар теней + Массовый тестер прокси + Редактор `config.toml`**.

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

# Запустить веб-панель управления (Control & Unban Center: мониторинг, Outlook Hub импорт, залив, SSE лог)
uv run kintaro-auto web --port 8080

# Мгновенный HTTP-аудит всех сессий (RegCountry, CurrentRegion, bric_social, group=47/661 и посты в Studio)
uv run kintaro-auto audit

# Разовый прогон 3-канального снятия огранов + авто-перевод отклонённых (FAILED) постов в Private
uv run kintaro-auto appeal --hide-rejected

# Бесконечный фоновый сторож по кукам (анти-фрод рандом 95–165 сек между циклами без повторных логинов)
uv run kintaro-auto appeal --watch --hide-rejected

# Локальный тест уникализатора медиа без API-ключа (поддерживает --mode minimal_snow / ultra_hard)
uv run kintaro-auto uniquify-test media/slide1.jpg --mode minimal_snow

# 5-поточная анти-теневая кампания (6 постов + minimal_snow + рестарт ТТ после 3 + 3-этапный Auto-Appeal 2048)
uv run kintaro-auto campaign -m slide1.jpg -m slide2.jpg -d "#piratetoken #piratetokenshop #blackknight #pirateshop #fyp" --threads 5 --copies 6 --photo-mode minimal_snow --restart-every 3 --music Fortnite

# Холодный перезапуск приложения TikTok и очистка старых постов
uv run kintaro-auto restart-app
uv run kintaro-auto clean-posts

# Анализ конкурента (охваты, хэштеги, вирусная музыка) и живой TOTP 2FA код
uv run kintaro-auto spy @competitor_handle
uv run kintaro-auto totp JBSWY3DPEHPK3PXP
```
