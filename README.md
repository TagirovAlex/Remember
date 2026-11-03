# Remember — учёт серверов, подписок и платежей

![License: MIT](https://img.shields.io/badge/License-MIT-2EA44F) ![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white) ![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white) ![Uvicorn](https://img.shields.io/badge/Uvicorn-499848) ![Jinja2](https://img.shields.io/badge/Jinja2-B41717?logo=jinja&logoColor=white) ![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy_2-D71F00) ![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white) ![Pydantic](https://img.shields.io/badge/Pydantic_2-EA454C) ![aiogram](https://img.shields.io/badge/aiogram_3-2CA5E0)

![APScheduler](https://img.shields.io/badge/APScheduler-6BA81E) ![httpx/SOCKS5](https://img.shields.io/badge/httpx/SOCKS5-5C5C5C) ![Pillow](https://img.shields.io/badge/Pillow-1C7ED6) ![Argon2](https://img.shields.io/badge/Argon2-7B2CBF) ![pytest](https://img.shields.io/badge/pytest-0A9EDC?logo=pytest&logoColor=white) ![Ruff](https://img.shields.io/badge/Ruff-F04E30?logo=ruff&logoColor=white) ![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white) ![Telegram Bot API](https://img.shields.io/badge/Telegram_Bot_API-26A5E4?logo=telegram&logoColor=white) ![Deploy: Caddy/nginx](https://img.shields.io/badge/Deploy-Caddy/nginx-2C2C2C)

Единая система для учёта арендуемых GPU-серверов, VM-хостов, подписок на сервисы
и разовых расходов. Отвечает на два вопроса: **«что где живёт»** и **«что и когда
платить»**. Напоминания приходят в Telegram (доступ через SOCKS5-прокси),
веб-интерфейс живёт на поддомене основного домена.

## Возможности

- **Серверы** (GPU / VM / dedicated): провайдер, локация, IP, спецификация,
  развёрнутые сервисы, стоимость и график оплаты.
- **Подписки** на сервисы: стоимость, период, автопродление, дата следующего платежа.
- **Разовые платежи** — отдельная сущность: название, сумма, дата, категория.
- **Картинки для сущностей**: у серверов, подписок и расходов можно загрузить
  квадратную картинку (не более 256×256, файл до 2 МБ, PNG/JPEG/WebP);
  хранится в `data/images/` как WebP, показывается в списках и на дашборде,
  загружается/удаляется в форме редактирования.
- **История платежей** с покрываемым периодом.
- **Telegram-дайджест (утро)**: платежи на сегодня, ближайшие 7 дней (настраивается),
  просроченные.
- **Вечернее напоминание** в тот же день, если есть просроченные (время настраивается,
  включается/выключается).
- **Кнопка «Оплачено ✓»** (в боте и вебе) — запись в историю и автоматический
  перенос `next_due` без сдвига графика. Повторное подтверждение в тот же день
  не сдвигает дату ещё раз (защита от двойного клика).
- **Веб-интерфейс**: дашборд со сводкой и суммами, CRUD, отметка оплаты,
  история платежей, настройки.
- **Пользователи**: несколько учётных записей, роли `admin`/`user`
  (управление пользователями и бэкапами — только admin), в истории платежей
  видно, кто отметил оплату.
- **Многопользовательность**: у каждого пользователя свои серверы, подписки,
  расходы и платежи (изолированы; чужие данные недоступны). Саморегистрация
  через `/register`. Каждый привязывает свой Telegram (ввод id в настройках +
  подтверждение через бота) — дайджесты и команды бота работают по его данным.
- **Тёмная/светлая тема** — переключается в шапке, выбор хранится на пользователе.
- **Архивация данных**: ручное создание/скачивание/удаление бэкапов SQLite
  + автоматический бэкап по расписанию с ротацией.
- **Управление ботом из интерфейса**: включить/выключить Telegram-бота на лету
  (без перезапуска) в `/settings`; токен — только в окружении (`BOT_TOKEN`).
  В `/settings` виден статус процесса («✅ Запущен», «⏳ Запускается…»,
  «⏹ Остановлен» или «❌ Ошибка: <текст>») и при подключении — API-строка
  «✅ Online — @username (имя)». Сбой polling-таска (сеть/API) не роняет
  процесс — веб и планировщик продолжают работать, бот переходит в статус ошибки.
- **Прокси к Telegram** — SOCKS5: хост/порт/логин/пароль задаются в настройках
  (БД, редактируются в `/settings`); пароль в интерфейсе не отображается.
- **Все настройки — в интерфейсе** (хранятся в БД, в коде ничего не захардкожено).
  Секреты (BOT_TOKEN, пароль админа) — только через окружение; пароль прокси
  хранится в БД и не выводится в веб.

## Принятые решения

| Область | Решение |
| --- | --- |
| Язык/стек | Python 3.11+, FastAPI, Jinja2, SQLAlchemy 2 (async), SQLite, aiogram 3, APScheduler, pydantic-settings |
| Хостинг приложения | Один из существующих VM; TLS — Caddy/nginx на реверс-прокси (вне кода) |
| Валюта | Одна — рубли (`amount_rub`); все суммы и отчёты считаются только в рублях |
| Доступ к веб-интерфейсу | Логин по `username` + паролю (argon2), сессионная cookie; роли `admin`/`user`. Срок жизни сессии настраивается (`session_ttl_days`, 1–30, default 1); смена пароля (своя или админом) увеличивает `users.session_epoch` и инвалидирует прежние сессии |
| Автоперенос даты после оплаты | От **исходной** `next_due` (+период), а не от даты фактической оплаты, чтобы график не «полз». Один платёж — один период; повторное подтверждение в тот же день не сдвигает дату |
| Тема интерфейса | Светлая/тёмная, выбор хранится на пользователе (`users.theme`) |
| Картинки сущностей | Файлы в `data/images/` (`<entity_type>_<id>.webp`, WebP, до 256×256, кач. 85); в БД — только имя файла; центр-кроп до квадрата, EXIF-поворот |
| Бэкапы | Копия SQLite через `sqlite3.backup()` в `data/backups/`, ротация по `backup_keep`; автобэкап по расписанию работает и без бота |
| Активные/неактивные позиции | Сервер может быть выключен (`is_active`), подписка — `active/paused/archived`; неактивные не участвуют в отчётах |
| Прокси к Telegram | SOCKS5 (библиотеки `httpx[socks]` + `aiogram`) |
| Секреты | Только переменные окружения / `.env`; реальный `.env` в `.gitignore` |
| Защита входа | Пароли — argon2; сессии подписаны и привязаны к `session_epoch`; rate-limit на `/login` и `/register`; logout только POST; security-заголовки (CSP, nosniff, X-Frame-Options) |
| Запуск | Один процесс: uvicorn (FastAPI) + aiogram polling + APScheduler |
| Деплой | Docker Compose или systemd (пример в `deploy/`) |
| GUI-валюта сводки | Всё суммируется в рублях |

## Структура проекта

```
app/
  main.py            # точка входа: FastAPI + бот + планировщик (python -m app)
  config.py          # pydantic-settings: только секреты и окружение
  db.py              # async engine, session, init_db
  models.py          # ORM: Server, Subscription, OneTimeExpense, Payment, Setting, BotSubscriber, User
  schemas.py         # pydantic-схемы для валидации форм и настроек
  security.py        # argon2-хэши паролей, подпись сессий
  auth.py            # пользователи, логин, сессии, роли
  services/
    settings.py      # типизированный доступ к настройкам из БД (+метаданные для UI)
    billing.py       # чистые функции: rollover next_due, digest, mark_paid
    notify.py        # форматирование сообщений для Telegram
    backup.py        # бэкапы SQLite: create/list/prune/resolve
    images.py        # картинки сущностей: save/delete/validate (Pillow, WebP)
  routers/
    web.py           # страницы (Jinja2)
    api.py           # JSON API + mark-paid
  scheduler.py       # APScheduler: утро/вечер/бэкап, настройки из БД
  bot/
    bot.py           # aiogram: сессия, proxy, polling
    handlers.py      # команды и callbacks бота
    keyboards.py     # inline-кнопки «Оплачено ✓» и карточек
  templates/         # Jinja2
  static/            # css/js
tests/               # pytest (business logic — чисто, без БД/сети)
deploy/              # Dockerfile, docker-compose.yml, пример systemd
data/                # SQLite-файл, бэкапы и картинки (gitignored)
AGENTS.md            # правила многоагентной разработки
.env.example         # шаблон окружения
```

## Запуск (локальная разработка)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # заполнить секреты
python -m app          # web (uvicorn) + бот + планировщик в одном процессе
```

При первом запуске создаётся БД (`data/remember.db`). Первый админ создаётся
автоматически: логин `admin`, пароль из `ADMIN_PASSWORD` (env) — либо мигрирует
из предыдущей версии (если в БД уже был задан пароль админа). Дальше пароль
и пользователей меняют в вебе (`/settings`, `/users`).

## Деплой (VM с доменом)

Варианты в `deploy/` (подробности — `deploy/README.md`):

```bash
# Docker Compose (из корня проекта)
cp .env.example .env    # заполнить BOT_TOKEN, ADMIN_PASSWORD
docker compose -f deploy/docker-compose.yml up -d --build

# или systemd
# - положить код в /opt/remember, создать /etc/remember.env
# - скопировать deploy/remember.service в /etc/systemd/system/
```

Рекомендуемое окружение: приложение слушает `HOST:PORT` (по умолчанию
`0.0.0.0:8000`; если 8000 занят, задайте, например, `PORT=8001`), наружу — TLS
на реверс-прокси (Caddy/nginx) с поддоменом; сессионная cookie автоматически
помечается `secure`, когда приходит заголовок `X-Forwarded-Proto: https`.
Приложение в одном процессе: веб + бот (polling) + планировщик.

## Telegram-бот

Доступные команды:

| Команда | Действие |
| --- | --- |
| `/start`, `/help` | Справка (и главное меню) |
| `/menu` | Главное inline-меню: подписки, серверы, оплаты, добавление, дашборд |
| `/subscribe` | Подписать текущий чат на дайджесты (по умолчанию включено на /start) |
| `/unsubscribe` | Отписаться от дайджестов |
| `/today` | Платежи на сегодня |
| `/soon` | Платежи на ближайшие N дней |
| `/overdue` | Просроченные платежи |
| `/stats` | Сводка за текущий месяц: оплачено, топ категорий, прогноз до конца месяца |
| `/addexpense` | Быстрое добавление разового расхода (название → сумма → категория → дата) |
| `/history [N]` | Последние платежи (N до 30, по умолчанию 10) |
| `/find <запрос>` | Поиск по названию/провайдеру серверов, подписок и расходов |
| `/add` | Быстрое добавление сервера/подписки |
| `/list` | Списки серверов и подписок |
| `/export` | Выгрузить все данные пользователя в текстовом файле (CSV) |
| `/cancel` | Отменить текущее действие (сбросить пошаговый ввод) |

Inline-кнопка «Оплачено ✓» подтверждает платёж: пишет историю и сдвигает `next_due`.
Повторное нажатие в тот же день не сдвигает дату повторно.

## Веб-интерфейс

- `/` — дашборд: сводка, суммы, просрочка, ближайшие платежи, «кто отметил оплату»
- `/servers`, `/subscriptions`, `/expenses` — CRUD соответствующих сущностей
- `/payments` — история платежей (с колонкой «Кто»)
- `/reports` — отчёты: сводка за месяц и произвольный период (суммы, категории)
- `/users` — управление пользователями (только admin)
- `/backups` — бэкапы: создать, скачать, удалить (только admin)
- `/settings` — настройки приложения. Глобальные разделы (бот, параметры,
  SOCKS5-прокси) доступны только admin; привязка Telegram и смена пароля — каждому
- `/login`, `/register`, `/logout`

## Настройки (в БД, редактируются в UI)

| Ключ | По умолчанию | Описание |
| --- | --- | --- |
| `morning_enabled` | `true` | Утренний дайджест включён |
| `morning_time` | `09:00` | Время утреннего дайджеста |
| `evening_reminder_enabled` | `true` | Вечернее напоминание включено |
| `evening_reminder_time` | `19:00` | Время вечернего напоминания |
| `remind_soon_days` | `7` | Горизонт «ближайших» платежей (1–30) |
| `session_ttl_days` | `1` | Срок жизни веб-сессии (1–30) |
| `bot_enabled` | `false` | Telegram-бот включён (живой старт/стоп из `/settings`, без рестарта) |
| `proxy_enabled` | `false` | SOCKS5-прокси включён |
| `proxy_host` | `` | Хост прокси |
| `proxy_port` | `` | Порт прокси (по умолчанию 1080) |
| `proxy_user` | `` | Логин прокси (если есть) |
| `proxy_password` | `` | Пароль прокси (не отображается в UI) |
| `backup_enabled` | `false` | Автоматический бэкап по расписанию |
| `backup_time` | `03:00` | Время автоматического бэкапа |
| `backup_keep` | `14` | Сколько бэкапов хранить (1–100) |
| `tz` | из env `TZ` | Часовой пояс для «сегодня» |

Прочее управление: чаты-подписчики — через бота; пользователи и бэкапы — в вебе.

## Тесты и проверка

```bash
python -m pytest -q
ruff check .
```

Бизнес-логика (rollover, digest, суммы) изолирована в чистых функциях — тестируется
без БД и сети. См. `AGENTS.md` о том, как ведутся изменения в этом репозитории.

## Лицензия

Проект распространяется под лицензией MIT. См. файл [LICENSE](LICENSE).

Код можно свободно использовать, модифицировать и распространять при условии
сохранения уведомления об авторстве (copyright notice) в копиях.