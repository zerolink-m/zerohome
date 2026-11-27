import asyncio
import json

import tinytuya

from app.core.codes import BAD_REQUEST, SERVER_ERROR
from app.core.config import settings
from app.core.errors import AppError
from app.models.switch import Switch
from app.schemas.switch import CustomAction, CustomResponse, CustomSyncAction, tuya_cloud_endpoint
from app.services import custom_switch_http, tuya_client_pool
from app.services.tuya import TuyaError

async def _with_timeout(coro):
    try:
        return await asyncio.wait_for(coro, timeout=settings.SWITCH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise AppError(SERVER_ERROR, "Устройство не отвечает") from exc


def _tuya_local_set_state_sync(meta: dict, state: int) -> None:
    device = tinytuya.OutletDevice(meta["device_id"], meta["ip"], meta["local_key"])
    device.set_version(float(meta.get("version", "3.3")))
    device.set_socketPersistent(False)
    result = device.turn_on() if state == 1 else device.turn_off()
    # TinyTuya can confirm the command with an empty ACK (None).
    if isinstance(result, dict) and ("Error" in result or "Err" in result):
        raise AppError(SERVER_ERROR, "Tuya Local не выполнил команду")


async def _tuya_cloud_client(meta: dict):
    """Get (or lazily create) the pooled TuyaCloudClient for this switch's
    credentials. Credentials live in the switch's controlmeta, mirrored from
    the parent Connection at sync time."""
    try:
        endpoint = tuya_cloud_endpoint(meta["endpoint"])
    except ValueError as exc:
        raise AppError(BAD_REQUEST, str(exc)) from exc
    return await tuya_client_pool.get_client(
        access_id=meta["access_id"],
        access_secret=meta["access_secret"],
        endpoint=endpoint,
    )


def _tuya_cloud_dp_codes(meta: dict) -> list[str]:
    """Which DP codes to toggle when this row's state is set.

    Master row's meta carries `dp_codes: [switch_1, switch_2, ...]` — a batch
    of every channel of the physical device. Child row's meta carries a
    single `dp_code: switch_N`. Older/user-added rows without either key fall
    back to `switch_1` for backwards compatibility with the pre-multichannel
    schema.
    """
    codes = meta.get("dp_codes")
    if isinstance(codes, list) and codes:
        return [c for c in codes if isinstance(c, str) and c]
    single = meta.get("dp_code")
    if isinstance(single, str) and single:
        return [single]
    return ["switch_1"]


async def _tuya_cloud_set_state(meta: dict, state: int) -> None:
    commands = [{"code": code, "value": bool(state)} for code in _tuya_cloud_dp_codes(meta)]
    if not commands:
        # _tuya_cloud_dp_codes never returns empty (falls back to switch_1),
        # but guard anyway — the new client does not validate empty batches
        # and would POST {"commands": []} to Tuya.
        raise AppError(SERVER_ERROR, "Нет DP-кодов для отправки команды")
    client = await _tuya_cloud_client(meta)
    try:
        await client.send_commands(meta["device_id"], commands)
    except TuyaError as exc:
        raise AppError(SERVER_ERROR, str(exc)) from exc


async def _tuya_cloud_get_state(meta: dict) -> int:
    client = await _tuya_cloud_client(meta)
    try:
        items = await client.get_device_status(meta["device_id"])
    except TuyaError as exc:
        raise AppError(SERVER_ERROR, str(exc)) from exc

    codes = _tuya_cloud_dp_codes(meta)
    # items is a list[DPValue] (pydantic) — attribute access, not dict.
    by_code = {i.code: i.value for i in items}

    # Master (dp_codes list): OR across all channels — "on" iff any child is on.
    # Child (dp_code single): just that one DP.
    values = [by_code.get(code) for code in codes]
    if any(v is True for v in values):
        return 1
    if not all(v is False for v in values):
        raise AppError(
            SERVER_ERROR,
            "Tuya Cloud вернул неполное или некорректное состояние",
        )
    return 0


def _tuya_local_get_state_sync(meta: dict) -> int:
    device = tinytuya.OutletDevice(meta["device_id"], meta["ip"], meta["local_key"])
    device.set_version(float(meta.get("version", "3.3")))
    device.set_socketPersistent(False)
    status = device.status()
    dps = status.get("dps") if isinstance(status, dict) else None
    if not isinstance(dps, dict) or not isinstance(dps.get("1"), bool):
        raise AppError(SERVER_ERROR, "Не удалось получить статус устройства Tuya Local")
    return 1 if dps["1"] else 0


def _split_path_segments(raw: str) -> list[str]:
    return [seg for seg in raw.split("/") if seg]


def _apply_filter(value, filter_expr: str):
    key, _, expected = filter_expr.partition("=")
    if not isinstance(value, list):
        raise AppError(SERVER_ERROR, f"Ожидался список для фильтра [{filter_expr}], получено {type(value).__name__}")
    for item in value:
        if not isinstance(item, dict):
            continue
        if str(item.get(key)) == expected:
            return item
    raise AppError(SERVER_ERROR, f"Не найден элемент списка с {key}={expected}")


def _step_into(value, segment: str):
    key_part, bracket, filter_expr = segment.partition("[")
    if bracket:
        if not filter_expr.endswith("]"):
            raise AppError(SERVER_ERROR, f"Некорректный сегмент пути: '{segment}'")
        filter_expr = filter_expr[:-1]

    if key_part:
        if not isinstance(value, dict):
            raise AppError(SERVER_ERROR, f"Ожидался объект для ключа '{key_part}', получено {type(value).__name__}")
        if key_part not in value:
            raise AppError(SERVER_ERROR, f"В ответе отсутствует поле '{key_part}'")
        value = value[key_part]

    if bracket:
        value = _apply_filter(value, filter_expr)

    return value


def _extract_path_value(path: str, resp: custom_switch_http.Reply):
    if path == "status://":
        return resp.status_code

    if path.startswith("header://"):
        header_name = path[len("header://"):]
        if header_name not in resp.headers:
            raise AppError(SERVER_ERROR, f"В ответе отсутствует заголовок '{header_name}'")
        return resp.headers[header_name]

    if path.startswith("json://body"):
        try:
            value = json.loads(resp.body)
        except ValueError as exc:
            raise AppError(SERVER_ERROR, "Ответ устройства не является валидным JSON") from exc
        rest = path[len("json://body"):]
        for segment in _split_path_segments(rest):
            value = _step_into(value, segment)
        return value

    raise AppError(SERVER_ERROR, f"Неподдерживаемый path: {path}")


def _evaluate_custom_response(spec: CustomResponse | None, resp: custom_switch_http.Reply) -> None:
    if spec is None:
        if resp.status_code < 200 or resp.status_code >= 300:
            raise AppError(SERVER_ERROR, f"Устройство вернуло ошибку: {resp.status_code}")
        return

    raw = _extract_path_value(spec.path, resp)
    mapped = spec.map.get(str(raw))
    if mapped is None:
        raise AppError(SERVER_ERROR, f"Не удалось сопоставить значение '{raw}' из ответа устройства")


async def _custom_set_state(meta: dict, state: int, allow_local: bool) -> None:
    action = CustomAction(**(meta["on"] if state == 1 else meta["off"]))
    response_spec = action.response
    resp = await custom_switch_http.send(
        action.request, allow_local,
        read_body=response_spec is not None and response_spec.path.startswith("json://body"),
    )
    _evaluate_custom_response(response_spec, resp)


def _resolve_custom_response_state(spec: CustomResponse, resp: custom_switch_http.Reply) -> int:
    raw = _extract_path_value(spec.path, resp)
    mapped = spec.map.get(str(raw))
    if mapped is None:
        raise AppError(SERVER_ERROR, f"Не удалось сопоставить значение '{raw}' из ответа устройства")
    return mapped


async def _custom_get_state(meta: dict, allow_local: bool) -> int:
    sync_meta = meta.get("sync")
    if sync_meta is None:
        raise AppError(BAD_REQUEST, "Для этого устройства не настроен sync")
    action = CustomSyncAction(**sync_meta)

    resp = await custom_switch_http.send(
        action.request, allow_local, read_body=action.response.path.startswith("json://body"),
    )

    return _resolve_custom_response_state(action.response, resp)


async def set_state(switch: Switch, state: int, *, db=None) -> None:
    meta = switch.controlmeta or {}
    if switch.control_proto == "tuya_local":
        await _with_timeout(asyncio.to_thread(_tuya_local_set_state_sync, meta, state))
    elif switch.control_proto == "tuya_cloud":
        await _with_timeout(_tuya_cloud_set_state(meta, state))
    elif switch.control_proto == "custom":
        allow_local = db is not None and await custom_switch_http.owner_allows_local(db, switch.ownuser)
        await _with_timeout(_custom_set_state(meta, state, allow_local))
    else:
        raise AppError(BAD_REQUEST, f"Неизвестный control_proto: {switch.control_proto}")


def supports_sync(switch: Switch) -> bool:
    meta = switch.controlmeta or {}
    if switch.control_proto in ("tuya_local", "tuya_cloud"):
        return True
    if switch.control_proto == "custom":
        return meta.get("sync") is not None
    return False


async def get_state(switch: Switch, *, db=None) -> int:
    """Queries the device's real current state. Raises AppError if unsupported or unreachable."""
    meta = switch.controlmeta or {}
    if switch.control_proto == "tuya_local":
        return await _with_timeout(asyncio.to_thread(_tuya_local_get_state_sync, meta))
    elif switch.control_proto == "tuya_cloud":
        return await _with_timeout(_tuya_cloud_get_state(meta))
    elif switch.control_proto == "custom":
        allow_local = db is not None and await custom_switch_http.owner_allows_local(db, switch.ownuser)
        return await _with_timeout(_custom_get_state(meta, allow_local))
    else:
        raise AppError(BAD_REQUEST, f"Неизвестный control_proto: {switch.control_proto}")
