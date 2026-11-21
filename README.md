# ZeroCams

## Оглавление

- [Описание проекта](#описание-проекта)
- [Возможности](#возможности)
- [Установка](#установка)
  - [Docker](#docker)
  - [Обычная установка](#обычная-установка)
- [Другое](#другое)

## Описание проекта

ZeroCams — сервер видеонаблюдения. Он ведёт камеры, отдаёт просмотр по HLS и WebRTC, детектирует движение, отправляет уведомления в Telegram и подключает устройства (выключатели и облако Tuya).

## Возможности

- Камеры и просмотр: список и настройки камер, поток, HLS, WebRTC, снимок и PTZ. HLS и WebRTC по умолчанию выключены (`HLS_ENABLED`, `WEBRTC_ENABLED`).
- Детекция движения с фотографией события.
- Уведомления в Telegram, в том числе с фотографией.
- Шаринг доступа: приглашения и выдача доступа к камерам и устройствам.
- Несколько узлов с одной базой. Приём потока, детекция движения и часть фоновых задач берутся узлом в аренду и не запускаются на каждом узле для всех камер. Режим включается `CLUSTER_ENABLED`.

## Установка

### Docker

Образ: `ghcr.io/zerolink-m/zerohome`. В шаблонах по умолчанию тег `stable`.

Два отдельных файла в `templates/`. Их не объединяют и не запускают вместе.

- `templates/compose.yaml` — приложение и встроенная MySQL.
- `templates/compose.external-db.yaml` — приложение и уже работающая внешняя база. Контейнера базы в этом файле нет.

В окружении Compose задают `DB_NAME`, `DB_USER` и `DB_PASSWORD`. Для встроенной базы дополнительно нужен `MYSQL_ROOT_PASSWORD`: в контейнер приложения он не попадает, хост базы там фиксирован как сервис `db`. Для внешней базы задают `DB_HOST` и при необходимости `DB_PORT`. Файл настроек приложения — `ZEROHOME_CONFIG`; если он не задан, шаблон указывает на `/etc/zerohome/app.conf`.

Для нескольких узлов у каждого свой `NODE_ID`. Общий `CLUSTER_SECRET` нужен, когда приём медиа между узлами не ограничен loopback: секрет проверяет рукопожатие и не шифрует трафик. Схему мигрирует один узел. На нём `ZEROHOME_MIGRATE` не задают или ставят `1`, на остальных — `ZEROHOME_MIGRATE=0`. Внешняя база выбирается переменной `ZEROHOME_EXTERNAL_DB=1`. Скрипт `deploy/update.sh` сам берёт `templates/compose.yaml` или, при внешней базе, `templates/compose.external-db.yaml`.

Каталог в unit-файле таймера — `/opt/zerohome`. Первый запуск со встроенной базой из этого каталога:

```sh
docker compose --env-file /etc/zerohome/deployment.conf -f templates/compose.yaml pull db
sudo ./deploy/update.sh
```

Внешняя база:

```sh
ZEROHOME_EXTERNAL_DB=1 sudo -E ./deploy/update.sh
```

Остальные узлы, без своей миграции:

```sh
ZEROHOME_EXTERNAL_DB=1 ZEROHOME_MIGRATE=0 sudo -E ./deploy/update.sh
```

Автообновление образа `stable` само не включается. Его включает таймер `deploy/zerohome-update.timer`. На узле, который мигрирует базу, таймер ставят как есть. На остальных тот же таймер, но с `ZEROHOME_MIGRATE=0`: скрипт обновит только контейнер приложения и не тронет базу.

```sh
sudo install -m 0644 deploy/zerohome-update.service deploy/zerohome-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zerohome-update.timer
```

### Обычная установка

Команды ниже — из корня исходников. В образе и в CI зависимости ставятся так: из `requirements.txt` убираются строки, которые начинаются с `cuda-`, `nvidia-`, `torch==`, `torchvision==` и `triton==`, torch и torchvision берутся с CPU-индекса, остальное — из отфильтрованного списка. Файл `requirements-cpu.txt` эти команды создают на месте, в репозитории его нет.

```sh
python -m venv venv
venv/bin/python - <<'PY'
from pathlib import Path
excluded = ("cuda-", "nvidia-", "torch==", "torchvision==", "triton==")
lines = Path("requirements.txt").read_text().splitlines()
Path("requirements-cpu.txt").write_text(
    "\n".join(line for line in lines if not line.lower().startswith(excluded)) + "\n"
)
PY
venv/bin/python -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.1+cpu torchvision==0.29.1+cpu
venv/bin/python -m pip install -r requirements-cpu.txt
venv/bin/python -m pip check
```

Нужны MySQL и программы `ffmpeg` и `ffprobe` в `PATH` (имена задаются `FFMPEG_BINARY` и `FFPROBE_BINARY`). Настройки читаются из окружения и из файла `.env`; имена переменных — в `.env.example`. Без значений не стартуют как минимум `DB_PASSWORD`, `JWT_SECRET_SESSION`, `JWT_SECRET_SHARE`, `SHARE_TOKEN_MIN_TTL_SECONDS` и `SHARE_TOKEN_MAX_TTL_SECONDS`.

Схема:

```sh
venv/bin/alembic upgrade head
```

Запуск. `main.py` вызывает uvicorn, хост и порт берутся из `HOST` и `PORT`.

```sh
venv/bin/python main.py
```

## Другое

Подробности развёртывания, обновления и нескольких узлов — в [docs/docker-deployment.md](docs/docker-deployment.md). Описание HTTP API — в [API.md](API.md).

Репликацию базы и резервную копию внешней базы оператор делает сам. Скрипт обновления не снимает дамп внешней базы и не поднимает для неё контейнер.
