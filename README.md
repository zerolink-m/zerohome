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
- Несколько узлов с одной базой. Приём потока, детекция движения и часть фоновых задач берутся узлом в аренду и не запускаются на каждом узле для всех камер. Каждая нода обслуживает зрителей HLS, fMP4 и WebRTC локально и получает поток от владельца ingest. Режим включается `CLUSTER_ENABLED`.

## Установка

### Docker

Образ: `ghcr.io/zerolink-m/zerohome`. В шаблонах по умолчанию тег `stable`.

Для чистого Debian/Ubuntu amd64 сервера из клонированного публичного репозитория запустите интерактивный установщик от root:

```sh
sudo bash deploy/setup.sh
```

Он проверит, что установка новая, предложит встроенную или внешнюю MySQL, создаст конфиги с правами `0600`, развернёт checkout в `/opt/zerohome`, а конфиги — в `/etc/zerohome`, установит Docker из официального apt-репозитория только если Docker отсутствует, затем вызовет `deploy/update.sh` для backup/migration-lock/migration/startup и предложит создать первого администратора локально внутри контейнера. Приложение остаётся привязано к `127.0.0.1:8000`; reverse proxy/TLS настраиваются отдельно. Установщик не поддерживает перенос существующей БД и откажется перезаписывать найденную установку, конфиги или systemd unit/timer. Проверочный режим: `sudo bash deploy/setup.sh --dry-run`.

Обычный запуск использует стандартные каталоги выше. Если `/opt/zerohome` уже занят посторонними файлами, можно осознанно выбрать другие абсолютные пути. Каталог установки должен отсутствовать; каталог конфигурации может существовать только пустым. Пути должны состоять из букв, цифр, `.`, `_`, `-` и `/` без пробелов. Например:

```sh
sudo bash deploy/setup.sh --install-dir /srv/zerohome --config-dir /etc/zerohome-prod
```

Установщик создаст update systemd unit с выбранным каталогом приложения и передаст updater-у `deployment.conf` из выбранного каталога конфигурации. Сами конфиги останутся в нём (`app.conf` и `deployment.conf`); существующие файлы и системные unit/timer он не перезаписывает.

#### Восстановление после остановки на создании администратора

Если setup уже создал конфиги и приложение стало healthy, но остановился до bootstrap первого администратора, не запускайте `setup.sh` повторно: установщик откажется перезаписывать созданные конфиги. Выполните bootstrap вручную из уже подготовленной установки. Для стандартных путей и встроенной БД:

```sh
INSTALL_DIR=/opt/zerohome
CONFIG_DIR=/etc/zerohome
sudo docker compose --env-file "$CONFIG_DIR/deployment.conf" \
  -f "$INSTALL_DIR/templates/compose.yaml" exec -it app \
  python -m deploy.bootstrap_admin
```

После успешного создания администратора установите и включите автообновление:

```sh
INSTALL_DIR=/opt/zerohome
CONFIG_DIR=/etc/zerohome
sudo tee /etc/systemd/system/zerohome-update.service >/dev/null <<EOF
[Unit]
Description=Backup, migrate and update ZeroHome stable image
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$INSTALL_DIR
Environment=ZEROHOME_DEPLOY_CONFIG=$CONFIG_DIR/deployment.conf
ExecStart=$INSTALL_DIR/deploy/update.sh
TimeoutStartSec=30min
UMask=0077
EOF
sudo install -m 0644 "$INSTALL_DIR/deploy/zerohome-update.timer" \
  /etc/systemd/system/zerohome-update.timer
sudo systemctl daemon-reload
sudo systemctl enable --now zerohome-update.timer
```

Для каталогов, выбранных через `--install-dir` и `--config-dir`, задайте в командах выше фактические значения, например `INSTALL_DIR=/srv/zerohome` и `CONFIG_DIR=/etc/zerohome-prod`. В unit обязательно передайте `ZEROHOME_DEPLOY_CONFIG=$CONFIG_DIR/deployment.conf`, чтобы updater читал конфигурацию именно оттуда. Если используется внешняя БД, bootstrap запускайте с `-f "$INSTALL_DIR/templates/compose.external-db.yaml"`; в `[Service]` также добавьте `Environment=ZEROHOME_EXTERNAL_DB=1`.

Для внешней БД установщик требует отдельное подтверждение резервной копии перед миграцией. Автообновление для неё включается только после второго явного подтверждения; иначе остаётся выключенным. Для встроенной MySQL update timer включается после успешного первого admin-bootstrap.

Два отдельных файла в `templates/`. Их не объединяют и не запускают вместе.

- `templates/compose.yaml` — приложение и встроенная MySQL.
- `templates/compose.external-db.yaml` — приложение и уже работающая внешняя база. Контейнера базы в этом файле нет.

В окружении Compose задают параметры подключения к БД: `DB_NAME`, `DB_USER`, `DB_PASSWORD`, а для внешней БД также `DB_HOST` и при необходимости `DB_PORT`. Для встроенной базы нужен отдельный `MYSQL_ROOT_PASSWORD`, который не передаётся приложению. Параметры приложения хранятся в БД; `ZEROHOME_CONFIG` задаёт только файл окружения контейнера, по умолчанию `/etc/zerohome/app.conf`.

Идентификатор ноды генерируется при первом старте и хранится в отдельном named volume; этот volume нельзя копировать между узлами. Настройки кластера и интеграций задаются в БД. Общий `CLUSTER_SECRET` нужен, когда приём медиа между узлами не ограничен loopback: секрет проверяет рукопожатие и не шифрует трафик. Схему мигрирует один узел. На нём `ZEROHOME_MIGRATE` не задают или ставят `1`, на остальных — `ZEROHOME_MIGRATE=0`. Внешняя база выбирается переменной `ZEROHOME_EXTERNAL_DB=1`. Скрипт `deploy/update.sh` сам берёт `templates/compose.yaml` или, при внешней базе, `templates/compose.external-db.yaml`.

Ручной запуск без установщика (для оператора, который сам подготовил root-owned конфиги в `/etc/zerohome` и checkout в `/opt/zerohome`) со встроенной базой:

```sh
docker compose --env-file /etc/zerohome/deployment.conf -f templates/compose.yaml pull db
sudo ./deploy/update.sh
```

Для вручную подготовленных альтернативных каталогов передайте updater-у `ZEROHOME_DEPLOY_CONFIG` с полным путём к `deployment.conf`:

```sh
sudo env ZEROHOME_DEPLOY_CONFIG=/etc/zerohome-prod/deployment.conf /srv/zerohome/deploy/update.sh
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

Нужны MySQL и программы `ffmpeg` и `ffprobe` в `PATH`. Runtime настройки загружаются из БД; process environment используется для подключения к БД и адреса прослушивания (`HOST`, `PORT`). `.env` автоматически не читается. Для явного одноразового переноса runtime-полей из существующего dotenv-файла используйте `deploy.import_legacy_settings` в dry-run режиме, затем с `--apply`; подробная upgrade-процедура приведена в [руководстве по Docker](docs/docker-deployment.md#перенос-настроек-и-ключей).

Новая регистрация всегда создаёт пользователя с ролью `user`. Первый администратор создаётся отдельной локальной командой после миграции и старта приложения; команда блокируется, если admin уже существует. Она читает пароль скрыто и не открывает self-promotion API.

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
