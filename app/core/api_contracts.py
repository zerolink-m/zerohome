"""Documentation-only contracts for the existing HTTP handlers.

The handlers remain the source of truth for authorization and side effects.  This
catalog describes their envelopes without replacing them with generated handlers.
"""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, RootModel, WithJsonSchema, create_model

from app.core import codes
from app.core.cursed_api import ResponseSpec, describe_contract
from app.deps.cursed_crud import AUTH_ERRORS, BODY_ERRORS, CrudContract, DeletedId, IgnoredData, ListQueryContract, error_specs
from app.routers import auth, cluster, config, invitations, motion, push_settings, shares, stats, switches, users, webrtc
from app.routers.cams import crud as cams, hls, recordings_routes, stream_ptz
from app.schemas.auth import LoginRequest, RegisterRequest, TokenOut
from app.schemas.cams import CamCreate, CamOnvifPatch, CamOutFull, CamOutRestricted, CamPatch, CamRecordingOut, CamRecordingPatch
from app.schemas.config import AssignmentOut, ConfigOut, NodeConfigurationOut, NodeConfigurationPatch, ServerTimeOut, SettingOut, SettingPatch
from app.schemas.invitation import InvitationCreate, InvitationOut
from app.schemas.motion import MotionEventResponse
from app.schemas.ptz import PTZCommandIn, PTZPositionOut
from app.schemas.push_settings import PushSettingsResponse, PushSettingsUpdate
from app.schemas.recording import RecordingOut
from app.schemas.sharing import SharingCreate, SharingCreateOut, SharingOut
from app.schemas.stats import AllStats, CameraStats, ErrorStats, MotionStats, PerformanceStats, RecordingStats, StreamStats, SystemStats, UserStats
from app.schemas.switch import CustomControlMeta, SwitchCreate, SwitchOutFull, SwitchOutRestricted, SwitchPatch, SwitchStateIn, SwitchStateOut, TuyaCloudMeta, TuyaLocalMeta
from app.schemas.user import UserCreate, UserOut, UserPatch


class CamRead(RootModel[CamOutFull | CamOutRestricted]):
    """Владелец и admin получают полную запись: id, owner, name, enabled, notify,
    protocol, address, port, path, username, password, control_protocol,
    control_address, control_port, control_username, control_password,
    onvif_enabled, onvif_snapshot_enabled, onvif_ptz_enabled, record, record_type,
    record_metadata, updated_at и added_at. Приглашённый получает те же общие поля
    без notify, username, password, control_address, control_port, control_username,
    control_password и record_metadata. Учётные данные не включаются в ограниченный ответ.
    """


class SwitchRead(RootModel[SwitchOutFull | SwitchOutRestricted]):
    """Владелец и admin получают полную запись: id, owner, name, enabled, notify,
    control_protocol, control_metadata, state, state_updated_at, updated_at,
    added_at и children. Приглашённый получает ограниченную запись без notify,
    control_metadata и state_updated_at. children содержит видимые дочерние
    выключатели мастер-устройства или null; control_metadata может содержать секреты.
    """


def _inline_model_schema(model):
    """Inline nested definitions so metadata variants remain self-contained."""
    root = model.model_json_schema()
    definitions = root.pop("$defs", {})

    def inline(value):
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        if value.get("$ref", "").startswith("#/$defs/"):
            name = value["$ref"].removeprefix("#/$defs/")
            return {**inline(definitions[name]), **{k: inline(v) for k, v in value.items() if k != "$ref"}}
        return {key: inline(item) for key, item in value.items()}

    return inline(root)


_SWITCH_METADATA = {
    "tuya_local": _inline_model_schema(TuyaLocalMeta),
    "tuya_cloud": _inline_model_schema(TuyaCloudMeta),
    "custom": _inline_model_schema(CustomControlMeta),
}
_SWITCH_VARIANT = {"oneOf": list(_SWITCH_METADATA.values())}
_SWITCH_METADATA_DESCRIPTION = (
    "Структура зависит от control_protocol: tuya_local — обязательны device_id, ip, local_key, version; "
    "tuya_cloud — обязательны access_id, access_secret, endpoint, device_id, необязательны "
    "dp_codes или dp_code (не вместе), region; custom — обязательны on и off, необязателен sync. "
    "Каждое действие custom содержит request с обязательными url и method и необязательными headers и body; "
    "response содержит обязательные path и map, у on/off он необязателен, у sync обязателен. "
    "Пароли и ключи являются секретами."
)
_SWITCH_CONDITIONS = [
    {"if": {"required": ["control_protocol", "control_metadata"],
            "properties": {"control_protocol": {"const": protocol}}},
     "then": {"properties": {"control_metadata": schema}}}
    for protocol, schema in _SWITCH_METADATA.items()
]


class SwitchCreateContract(SwitchCreate):
    """Request-only view of the protocol-specific metadata validator."""

    model_config = ConfigDict(json_schema_extra={"allOf": _SWITCH_CONDITIONS})
    control_metadata: Annotated[dict, WithJsonSchema(_SWITCH_VARIANT)] = Field(
        description=_SWITCH_METADATA_DESCRIPTION,
    )


class SwitchPatchContract(SwitchPatch):
    model_config = ConfigDict(json_schema_extra={"allOf": [*_SWITCH_CONDITIONS, {
        "if": {"required": ["control_metadata"]}, "then": {"required": ["control_protocol"]},
    }]})
    control_metadata: Annotated[dict | None, WithJsonSchema(_SWITCH_VARIANT)] = Field(
        default=None, description="При изменении метаданных передайте также control_protocol. " + _SWITCH_METADATA_DESCRIPTION,
    )


class RegisterResult(BaseModel):
    user: DeletedId
    token: str = Field(description="Новый токен сессии; сохраните его в безопасном хранилище.")


class PtzMoved(BaseModel):
    id: int
    pan: float | None
    tilt: float | None
    zoom: float | None


class OnvifResult(BaseModel):
    id: int
    onvif_enabled: int
    onvif_snapshot_enabled: int
    onvif_ptz_enabled: int


class OpenResult(BaseModel):
    success: bool


class MotionStatus(BaseModel):
    cam_id: int
    is_active: bool


class MotionStart(MotionStatus):
    started: bool


class MotionStop(MotionStatus):
    stopped: bool


class MotionEventsQuery(ListQueryContract):
    limit: int = Field(default=50, ge=1, le=200)
    order: str = Field(default="desc", description="По умолчанию события выводятся от новых к старым; заданная сортировка использует order.")


class RecordingsQuery(ListQueryContract):
    order: str = Field(default="desc", description="Без sort_by записи упорядочиваются по started_at от новых к старым.")


class StreamFormat(BaseModel):
    format: str
    enabled: bool
    readonly: bool
    name: str
    description: str
    latency: str
    endpoint: str
    mime_type: str | None = None
    protocol: str | None = None
    features: list[str]


class StreamsData(BaseModel):
    streams: list[StreamFormat]


class IceServer(BaseModel):
    urls: list[str]
    username: str | None = None
    credential: str | None = None


class IceResult(BaseModel):
    ice_servers: list[IceServer]
    ttl: int = Field(description="Время жизни временных TURN-учётных данных в секундах.")


class RangeHeaders(BaseModel):
    range: str | None = Field(default=None, alias="Range", description="Запрошенные диапазоны байтов, например bytes=0-1023.")
    if_range: str | None = Field(default=None, alias="If-Range", description="ETag или дата, ограничивающая применение Range.")


class ClusterNode(BaseModel):
    node_id: str
    address: str | None
    heartbeat_age_seconds: int
    alive: bool
    capabilities: dict[str, Any]
    version: str


class ClusterAssignment(BaseModel):
    function: str
    resource_id: int
    owner_node_id: str | None
    generation: int
    expires_in_seconds: int
    expired: bool


class Unassigned(BaseModel):
    function: str
    resource_id: int
    reason: str


class ClusterResult(BaseModel):
    node_id: str
    cluster_enabled: bool
    nodes: list[ClusterNode]
    assignments: list[ClusterAssignment]
    unassigned: list[Unassigned]


def _body(key: str, model: type[BaseModel]):
    return create_model(f"{model.__name__}{key.title().replace('_', '')}Data", __base__=IgnoredData,
                        **{key: (model, ...)})


def _action(endpoint, summary, *, description="", request=None, key=None, response=None,
            result_key=None, code=codes.SUCCESS, alternatives=(), errors=(), public=False,
            body_required=True, examples=None, request_example=None):
    # Every HTTP request, including a public one, crosses the global limiter.
    responses = error_specs(codes.RATE_LIMITED) if public else error_specs(*AUTH_ERRORS)
    responses.update(error_specs(*errors))
    if request is not None:
        responses.update(error_specs(*BODY_ERRORS, codes.EMPTY_BODY, codes.INVALID_QUERY))
    responses["result"] = ResponseSpec(code, summary, schema=response, response_key=result_key,
                                        example=examples)
    for alt, detail in alternatives:
        responses["result_" + alt] = ResponseSpec(alt, detail, schema=response, response_key=result_key)
    describe_contract(summary=summary, description=description, input_schema=_body(key, request) if request else None,
                      input_example=request_example,
                      body_required=body_required, responses=responses)(endpoint)


def _raw(endpoint, summary, media, *, description="", streaming=False, errors=(), file_range=False, range_media=None):
    details = description + " Успешное тело передаётся без JSON-обёртки; ошибки приложения имеют CursedJSON."
    response_code = codes.STREAMING if streaming else codes.SUCCESS
    responses = error_specs(*AUTH_ERRORS, codes.OBJECT_NOT_FOUND, *errors)
    for index, kind in enumerate(media):
        responses[f"body_{index}"] = ResponseSpec(response_code, summary, content_type=kind)
    if file_range:
        for index, kind in enumerate(range_media if range_media is not None else media):
            responses[f"range_{index}"] = ResponseSpec(codes.PARTIAL_CONTENT, "Часть файла по заголовку Range; Content-Range указывает границы.", content_type=kind)
        responses["range_multiple"] = ResponseSpec(codes.PARTIAL_CONTENT, "Несколько диапазонов в multipart/byteranges.", content_type="multipart/byteranges")
        responses["bad_range"] = ResponseSpec(codes.BAD_REQUEST, "Некорректный заголовок Range; текстовая ошибка FileResponse.", content_type="text/plain")
        responses["unsatisfiable_range"] = ResponseSpec(codes.RANGE_NOT_SATISFIABLE, "Диапазон за пределами файла; Content-Range содержит его размер.", content_type="text/plain")
    describe_contract(summary=summary, description=details, responses=responses,
                      header_schema=RangeHeaders if file_range else None)(endpoint)


def _crud(module, prefix, singular, plural, read, create=None, patch=None, *, id_name="id", errors=(), note="", create_response=None, names=None, optional_delete_body=False):
    contract = CrudContract(singular, plural, read, id_name=id_name)
    names = names or {}
    def apply(name, operation, summary, **kwargs):
        endpoint = getattr(module, names.get(name, name + "_" + prefix), None)
        if endpoint is not None:
            contract.route(operation, summary=summary, description=note, errors=errors, **kwargs)(endpoint)
    apply("list", "list", f"Список: {plural}")
    apply("get", "get", f"Получить: {singular}")
    apply("get_bulk", "get", f"Получить несколько: {plural}", bulk=True)
    if create:
        apply("create", "create", f"Создать: {singular}", request=create, response=create_response)
        apply("create_bulk", "create", f"Создать несколько: {plural}", request=create, response=create_response, bulk=True)
    if patch:
        apply("patch", "patch", f"Изменить: {singular}", request=patch)
        apply("patch_bulk", "patch", f"Изменить несколько: {plural}", request=patch, bulk=True)
    apply("delete", "delete", f"Удалить: {singular}", optional_delete_body=optional_delete_body)
    apply("delete_bulk", "delete", f"Удалить несколько: {plural}", bulk=True)


def register_contracts():
    """Attach contracts to concrete functions before integrating the router."""
    _crud(users, "user", "user", "users", UserOut, UserCreate, UserPatch,
          errors=(codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE, codes.FORBIDDEN_ROLE, codes.EMAIL_TAKEN),
          optional_delete_body=True,
          note="Пользователь видит себя; администратор может управлять пользователями. Административные поля менять может только admin.",
          names={"list": "list_users", "create_bulk": "create_users_bulk", "get_bulk": "get_users_bulk", "patch_bulk": "patch_users_bulk", "delete_bulk": "delete_users_bulk"})
    _action(users.get_current_user, "Данные текущего пользователя", response=UserOut, result_key="user", errors=(codes.OBJECT_NOT_FOUND,))
    _crud(cams, "cam", "cam", "cams", CamRead, CamCreate, CamPatch,
          errors=(codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE, codes.CONNECTION_INVALID),
          note="Владелец и admin получают полную запись, включая учётные данные; приглашённый получает ограниченную. Импортированную камеру нельзя изменять и удалять отдельно.",
          names={"list":"list_cams", "create_bulk":"create_cams", "get_bulk":"get_cams_bulk", "patch_bulk":"patch_cams_bulk", "delete_bulk":"delete_cams_bulk"})
    _crud(switches, "switch", "switch", "switches", SwitchRead, SwitchCreateContract, SwitchPatchContract, id_name="switch_id",
          errors=(codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE, codes.TUYA_LOCAL_NOT_ALLOWED),
          note="Владелец и admin получают полную запись, включая control_metadata; приглашённый получает ограниченную. Импортированные устройства доступны для чтения и управления.",
          names={"list":"list_switches", "create_bulk":"create_switches_bulk", "get_bulk":"get_switches_bulk", "patch_bulk":"patch_switches_bulk", "delete_bulk":"delete_switches_bulk"})
    _crud(invitations, "invitation", "invitation", "invitations", InvitationOut, InvitationCreate,
          errors=(codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE, codes.INVITATION_INVALID),
          optional_delete_body=True,
          note="Создать приглашение может владелец устройства или admin; принять его может адресат или admin.",
          names={"list":"list_invitations", "create_bulk":"create_invitations_bulk", "get_bulk":"get_invitations_bulk", "delete_bulk":"delete_invitations_bulk"})
    _crud(shares, "share", "share", "shares", SharingOut, SharingCreate, create_response=SharingCreateOut,
          errors=(codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE, codes.SHARE_INVALID, codes.RECORDING_NOT_CONFIGURED),
          optional_delete_body=True,
          note="Токен ссылки выдаётся только при создании; последующее чтение его не возвращает. Храните токен как секрет.",
          names={"list":"list_shares", "create_bulk":"create_shares_bulk", "get_bulk":"get_shares_bulk", "delete_bulk":"delete_shares_bulk"})
    _action(invitations.accept_invitation, "Принять приглашение", response=InvitationOut, result_key="invitation", code=codes.EDITED,
            alternatives=((codes.NOT_CHANGED, "Приглашение уже принято."),), errors=(codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE))
    _action(invitations.decline_invitation, "Отклонить и удалить приглашение", response=DeletedId, result_key="invitation", code=codes.DELETED,
            errors=(codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE))

    _action(auth.register, "Зарегистрировать пользователя и создать сессию", request=RegisterRequest, key="user", response=RegisterResult,
            code=codes.CREATED, errors=(codes.EMAIL_TAKEN, codes.ENDPOINT_RATE_LIMITED), public=True,
            request_example={"user": {"name": "Anna", "surname": "Example", "email": "anna@example.org", "password": "<password>"}},
            examples={"user": {"id": 1}, "token": "<session-token>"})
    _action(auth.login, "Войти и получить токен сессии", request=LoginRequest, key="credentials", response=TokenOut,
            code=codes.AUTHORIZED, errors=(codes.BAD_CREDENTIALS, codes.LOGIN_RATE_LIMITED, codes.ENDPOINT_RATE_LIMITED), public=True,
            request_example={"credentials": {"email": "anna@example.org", "password": "<password>"}},
            examples={"token": "<session-token>"})
    _action(auth.new_refresh, "Однократно обменять прежний токен сессии", response=TokenOut, code=codes.TOKEN_REFRESHED,
            errors=(codes.TOKEN_BAD_SIGNATURE, codes.TOKEN_BAD_ALGORITHM, codes.TOKEN_FORMAT_INVALID),
            description="Принимается действительный прежний session JWT; после обмена он больше не действует.")
    _action(auth.logout, "Завершить текущую сессию", response=DeletedId, result_key="session", code=codes.LOGGED_OUT)

    _action(config.get_frontend_config, "Версия приложения", response=ConfigOut, result_key="config", public=True)
    _action(config.get_time, "Текущее серверное время в секундах Unix UTC", response=ServerTimeOut, public=True, errors=(codes.ENDPOINT_RATE_LIMITED,))
    _action(config.get_config, "Проверить совместимость версии фронтенда", response=ConfigOut, result_key="config", public=True,
            description="Укажите v в формате vMAJOR.MINOR.PATCH-Release|Beta|Alpha.",
            alternatives=((codes.FRONTEND_UPDATE_AVAILABLE, "Доступно необязательное обновление фронтенда."),),
            errors=(codes.BAD_REQUEST, codes.FRONTEND_UPDATE_NEEDED, codes.ENDPOINT_RATE_LIMITED))
    _action(config.get_supported_streams, "Форматы трансляций, доступные на ноде", response=StreamsData, public=True)
    for endpoint, name, model in ((config.list_system_settings, "system_setting", SettingOut), (config.list_nodes, "node", NodeConfigurationOut), (cluster.list_assignments, "assignment", AssignmentOut)):
        plural = {"system_setting":"system_settings", "node":"nodes", "assignment":"assignments"}[name]
        CrudContract(name, plural, model).route("list", summary=f"Список: {plural}", description="Доступно администратору.", errors=(codes.FORBIDDEN_ROLE,))(endpoint)
    _action(config.get_system_setting, "Получить системную настройку", response=SettingOut, result_key="system_setting", errors=(codes.FORBIDDEN_ROLE, codes.OBJECT_NOT_FOUND), description="Только admin; секретные значения маскируются. metadata.revision содержит текущую ревизию.")
    _action(config.update_system_setting, "Обновить системную настройку", request=SettingPatch, key="setting", response=SettingOut, result_key="system_setting", code=codes.EDITED,
            errors=(codes.FORBIDDEN_ROLE, codes.OBJECT_NOT_FOUND), description="Только admin. metadata содержит revision и restart_required; секретные значения маскируются.")
    _action(config.get_node, "Получить настройки и возможности ноды", response=NodeConfigurationOut, result_key="node", errors=(codes.FORBIDDEN_ROLE, codes.OBJECT_NOT_FOUND))
    _action(config.update_node, "Изменить настройки и возможности ноды", request=NodeConfigurationPatch, key="node", response=NodeConfigurationOut, result_key="node", code=codes.EDITED,
            errors=(codes.FORBIDDEN_ROLE, codes.OBJECT_NOT_FOUND, codes.UNKNOWN_FIELD), description="Только admin. metadata содержит revision и restart_required.")
    _action(cluster.get_cluster, "Состояние кластера", response=ClusterResult, result_key="cluster", errors=(codes.FORBIDDEN_ROLE,), description="Только admin; показывает ноды, назначения и ресурсы без владельца.")
    _action(cluster.get_assignment, "Получить назначение функции", response=AssignmentOut, result_key="assignment", errors=(codes.FORBIDDEN_ROLE, codes.OBJECT_NOT_FOUND))

    _action(stream_ptz.get_ptz, "Получить позицию PTZ", response=PTZPositionOut, result_key="ptz", errors=(codes.OBJECT_NOT_FOUND, codes.PTZ_NOT_CONFIGURED, codes.EXTERNAL_API_UNAVAILABLE))
    _action(stream_ptz.move_ptz, "Передать команду PTZ", request=PTZCommandIn, key="ptz", response=PtzMoved, result_key="ptz", errors=(codes.BAD_REQUEST, codes.OBJECT_NOT_FOUND, codes.PTZ_NOT_CONFIGURED, codes.EXTERNAL_API_UNAVAILABLE),
            request_example={"ptz": {"pan": 0.25, "tilt": 0.0}}, examples={"id": 1, "pan": 0.25, "tilt": 0.0, "zoom": None})
    _action(stream_ptz.update_onvif_settings, "Обновить настройки ONVIF", request=CamOnvifPatch, key="onvif", response=OnvifResult, result_key="onvif", code=codes.EDITED,
            errors=(codes.BAD_REQUEST, codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE))
    _action(recordings_routes.get_cam_recording, "Получить настройки записи камеры", response=CamRecordingOut, result_key="recording_config", errors=(codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE, codes.VALIDATION_ERROR))
    _action(recordings_routes.patch_cam_recording, "Изменить настройки записи камеры", request=CamRecordingPatch, key="recording_config", response=CamRecordingOut, result_key="recording_config", code=codes.EDITED,
            alternatives=((codes.NOT_CHANGED, "Настройки не изменились."),), errors=(codes.BAD_REQUEST, codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE, codes.RECORDING_NOT_ALLOWED))
    CrudContract("recording", "recordings", RecordingOut).route("list", summary="Список записей камеры", description="Фильтры from и to задаются в секундах Unix; записи локальные.", request_query=RecordingsQuery, errors=(codes.OBJECT_NOT_FOUND, codes.VALIDATION_ERROR, codes.RECORDING_NOT_CONFIGURED))(recordings_routes.list_cam_recordings)
    _action(recordings_routes.get_cam_recording_meta, "Получить сведения о записи", response=RecordingOut, result_key="recording", errors=(codes.OBJECT_NOT_FOUND, codes.VALIDATION_ERROR, codes.RECORDING_NOT_CONFIGURED))

    _raw(stream_ptz.stream_cam, "Непрерывный fragmented MP4 поток", ("video/mp4",), streaming=True, errors=(codes.BAD_REQUEST, codes.SERVER_ERROR), description="Поток читается до отключения клиента; облачным камерам доступен HLS.")
    _raw(stream_ptz.get_live_snapshot, "JPEG-снимок камеры", ("image/jpeg",), errors=(codes.BAD_GATEWAY, codes.SERVER_ERROR))
    _raw(recordings_routes.get_cam_recording_file, "Скачать файл записи", ("video/mp4",), file_range=True, errors=(codes.VALIDATION_ERROR, codes.RECORDING_NOT_CONFIGURED), description="Поддерживаются Range и If-Range. Доступны только локальные MP4-записи.")
    _raw(recordings_routes.get_cam_recording_snapshot, "Получить JPEG-кадр записи", ("image/jpeg",), errors=(codes.INVALID_QUERY, codes.VALIDATION_ERROR, codes.RECORDING_NOT_CONFIGURED, codes.SERVER_ERROR), description="Необязательный at задаёт позицию в секундах.")
    _raw(hls.get_hls_playlist, "Получить live HLS-плейлист", ("application/vnd.apple.mpegurl",), streaming=True, errors=(codes.BAD_REQUEST, codes.BAD_GATEWAY, codes.SERVER_ERROR), description="При передаче ?token= тот же токен добавляется в URL сегментов.")
    _raw(hls.get_hls_segment, "Получить сегмент live HLS", ("video/mp2t", "application/octet-stream", "*/*"), streaming=True, file_range=True, range_media=("video/mp2t",), errors=(codes.BAD_REQUEST, codes.FILE_NOT_FOUND, codes.SERVER_ERROR), description="Локальный сегмент — video/mp2t и поддерживает Range; облачный использует Content-Type провайдера или application/octet-stream и игнорирует Range.")
    _raw(hls.get_ufanet_archive_playlist, "Получить HLS-плейлист облачного архива", ("application/vnd.apple.mpegurl",), errors=(codes.BAD_REQUEST, codes.INVALID_QUERY, codes.QUERY_REQUIRED, codes.BAD_GATEWAY, codes.SERVER_ERROR), description="Нужен access-токен сессии, share-токен не подходит. Обязательны start и duration; duration от 1 до 86400 секунд.")
    _raw(hls.get_ufanet_archive_resource, "Получить ресурс HLS облачного архива", ("application/octet-stream", "*/*"), errors=(codes.BAD_REQUEST, codes.INVALID_QUERY, codes.QUERY_REQUIRED, codes.FILE_NOT_FOUND, codes.SERVER_ERROR), description="Нужен access-токен сессии, share-токен не подходит. Content-Type задаёт провайдер; application/octet-stream — запасной вариант. Передайте те же start и duration, что для плейлиста.")
    _raw(motion.get_motion_event_photo, "Получить JPEG события движения", ("image/jpeg",), file_range=True, errors=(codes.FORBIDDEN_RESOURCE,), description="Range поддерживается только для старых локальных фото; объектное хранилище отдаёт ответ целиком.")

    _action(switches.get_switch_state, "Получить состояние выключателя", response=SwitchStateOut, result_key="state", errors=(codes.OBJECT_NOT_FOUND, codes.VALIDATION_ERROR))
    _action(switches.set_switch_state, "Установить состояние выключателя", request=SwitchStateIn, key="state", response=SwitchStateOut, result_key="state", errors=(codes.BAD_REQUEST, codes.FORBIDDEN_RESOURCE, codes.OBJECT_NOT_FOUND, codes.VALIDATION_ERROR, codes.SERVER_ERROR))
    _action(switches.open_switch, "Открыть СКУД", response=OpenResult, result_key="open", errors=(codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE, codes.VALIDATION_ERROR, codes.BAD_GATEWAY))
    for endpoint in (push_settings.get_cam_push_settings, push_settings.get_switch_push_settings):
        _action(endpoint, "Получить настройки уведомлений устройства", response=PushSettingsResponse, result_key="push_settings", errors=(codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE), description="Настройки относятся к текущему пользователю; при первом чтении создаются значения по умолчанию.")
    for endpoint in (push_settings.update_cam_push_settings, push_settings.update_switch_push_settings):
        _action(endpoint, "Изменить настройки уведомлений устройства", request=PushSettingsUpdate, key="push_settings", response=PushSettingsResponse, result_key="push_settings", code=codes.EDITED,
                alternatives=((codes.NOT_CHANGED, "Настройки не изменились."),), errors=(codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE))
    CrudContract("motion_event", "motion_events", MotionEventResponse).route("list", summary="Список событий движения камеры", request_query=MotionEventsQuery, errors=(codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE))(motion.get_motion_events)
    _action(motion.get_motion_event, "Получить событие движения", response=MotionEventResponse, result_key="motion_event", errors=(codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE))
    _action(motion.delete_motion_event, "Удалить событие движения и фото", response=DeletedId, result_key="motion_event", code=codes.DELETED, errors=(codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE, codes.FILE_STORAGE_ERROR))
    _action(motion.get_motion_detection_status, "Получить состояние детектора движения", response=MotionStatus, result_key="motion", errors=(codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE))
    _action(motion.start_motion_detection_endpoint, "Запустить детектор движения", response=MotionStart, result_key="motion", errors=(codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE))
    _action(motion.stop_motion_detection_endpoint, "Остановить детектор движения", response=MotionStop, result_key="motion", errors=(codes.OBJECT_NOT_FOUND, codes.FORBIDDEN_RESOURCE))
    for endpoint, key, model, title in (
        (stats.get_all_stats, None, AllStats, "Вся статистика"), (stats.get_system_stats, "system", SystemStats, "Ресурсы системы"),
        (stats.get_stream_stats, "streams", StreamStats, "Статистика трансляций"), (stats.get_camera_stats, "cameras", CameraStats, "Статистика камер"),
        (stats.get_recording_stats, "recordings", RecordingStats, "Статистика записей"), (stats.get_motion_stats, "motion", MotionStats, "Статистика детектора движения"),
        (stats.get_user_stats, "users", UserStats, "Статистика пользователей"), (stats.get_error_stats, "errors", ErrorStats, "Статистика ошибок"),
        (stats.get_performance_stats, "performance", PerformanceStats, "Метрики производительности"),
    ):
        errors = (codes.FORBIDDEN_ROLE, codes.INVALID_QUERY) if endpoint in (stats.get_all_stats, stats.get_motion_stats) else (codes.FORBIDDEN_ROLE,)
        _action(endpoint, title, response=model, result_key=key, errors=errors, description="Доступно только администратору.")
    _action(webrtc.get_ice, "Получить ICE-серверы для WebRTC", response=IceResult, result_key="ice", description="Bearer сессии или share:stream; query token не принимается. TURN-учётные данные временны; секрет сервера не возвращается.")
