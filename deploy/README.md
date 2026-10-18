# Деплой Remember

Примеры запуска приложения на выделенной VM.

## Вариант 1 — Docker Compose

```bash
# из корня репозитория
cp .env.example .env      # заполнить секреты
docker compose -f deploy/docker-compose.yml up -d --build
```

- Контекст сборки — корень проекта, Dockerfile — `deploy/Dockerfile`.
- База SQLite хранится в `./data` (монтируется в `/data`).
- Порт `8000`, рестарт — `unless-stopped`.

## Вариант 2 — systemd

Предполагается, что проект лежит в `/opt/remember` и создан venv:

```bash
sudo useradd --system --home /opt/remember --shell /usr/sbin/nologin remember
sudo mkdir -p /opt/remember && sudo chown remember:remember /opt/remember
python -m venv /opt/remember/.venv
/opt/remember/.venv/bin/pip install -e "."
sudo cp deploy/remember.service /etc/systemd/system/remember.service
sudo cp .env /etc/remember.env   # ключи/значения без кавычек, chmod 600
sudo systemctl daemon-reload
sudo systemctl enable --now remember
```

Если venv лежит в другом месте (например, `/opt/remember/venv`) — поправьте
`ExecStart` в `remember.service`. Если venv не используется — замените `ExecStart`
на системный python: `ExecStart=/usr/bin/python3 -m app`.

Первый запуск создаёт БД и администратора `admin` с паролем из `ADMIN_PASSWORD`.
Проверка статуса и логов:

```bash
systemctl status remember
journalctl -u remember -f
```

## TLS и reverse-proxy

Приложение слушает HTTP на `8000`. Перед ним поставьте реверс-прокси с TLS
(Caddy/nginx) и проксируйте `/` на `127.0.0.1:8000`. Caddy:

```
remember.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Cookie сессии ставится с флагом `secure`, только когда запрос пришёл по HTTPS:
прокси должен передавать заголовок `X-Forwarded-Proto: https`, иначе cookie
будет обычной (для локальной разработки по http это работает автоматически).