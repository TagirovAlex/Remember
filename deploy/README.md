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
- Порт по умолчанию `8000` (задаётся `PORT` в `.env`), рестарт — `unless-stopped`.
  Если меняете `PORT`, обновите проброс порта в `docker-compose.yml`.

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

Приложение слушает `HOST:PORT` из окружения; по умолчанию — `0.0.0.0:8000`
(в `/etc/remember.env` или `PORT=8001`, если 8000 уже занят другим сервисом).
Перед ним поставьте реверс-прокси с TLS (Caddy/nginx) и проксируйте `/` на
`127.0.0.1:<PORT>`. Caddy для порта `8001`:

```
remember.example.com {
    reverse_proxy 127.0.0.1:8001
}
```

nginx для порта `8001`:

```
location / {
    proxy_pass http://127.0.0.1:8001;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Cookie сессии ставится с флагом `secure`, только когда запрос пришёл по HTTPS:
прокси должен передавать заголовок `X-Forwarded-Proto: https`, иначе cookie
будет обычной (для локальной разработки по http это работает автоматически).

## Обновление

Код на сервере — обычный git-клон, поэтому обновление сводится к `pull` и
рестарту:

```bash
cd /opt/remember
git pull
sudo systemctl restart remember
sudo journalctl -u remember -n 50 --no-pager
```

`git pull` не трогает `.env`, `data/` и `venv/` — они в `.gitignore`.
Перед обновлением стоит сделать бэкап БД на странице `/backups`.