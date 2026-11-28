# Коды ответов cursedapi v1.2

Числовые коды изменились в cursedapi v1.2 по сравнению с v1.1. Справочник стандартных кодов: [`docs/cursedapi/docs/codes.md`](cursedapi/docs/codes.md); сервисные коды ZeroHome приведены ниже, а ситуации их применения — в [`API.md`](../API.md). В коде приложения используйте именованные константы из `app/core/codes.py`, а не числовые литералы: это предотвращает ошибки при переносе номеров между версиями спецификации.

Стандартные коды, статусы HTTP и значения `code_detail` загружаются из профиля 1.2 библиотеки `cursedapi-server`. Прежние сервисные константы и их номера сохранены; отдельные сценарии теперь возвращают новые коды `396–403`. Если библиотека использует другое имя для того же кода, `cursed_code(api.codes, BAD_REQUEST)` возвращает её объект `Code` для `INVALID_REQUEST`.

При создании `CursedAPI` вызовите `register_cursed_codes(api.codes)` до объявления маршрутов и схем ответов. Все сервисные коды назначаются в одном реестре `app/core/codes.py`. Перед добавлением кода проверьте, что его имя и номер свободны. Служебные номера не переназначаются после публикации.

| Код | HTTP | Константа |
| --- | --- | --- |
| 384 | 200 | `FRONTEND_UPDATE_AVAILABLE` |
| 385 | 403 | `TUYA_LOCAL_NOT_ALLOWED` |
| 386 | 403 | `RECORDING_NOT_ALLOWED` |
| 387 | 403 | `FRONTEND_UPDATE_NEEDED` |
| 388 | 409 | `PTZ_NOT_CONFIGURED` |
| 389 | 409 | `RECORDING_NOT_CONFIGURED` |
| 390 | 400 | `INVITATION_INVALID` |
| 391 | 400 | `PUSH_INVALID` |
| 392 | 400 | `SHARE_INVALID` |
| 393 | 400 | `CONNECTION_INVALID` |
| 394 | 206 | `PARTIAL_CONTENT` |
| 395 | 204 | `NO_CONTENT` |
| 396 | 400 | `INVITATION_ALREADY_EXISTS` |
| 397 | 400 | `INVITATION_OWNER_FORBIDDEN` |
| 398 | 400 | `CONNECTION_DISABLED` |
| 399 | 400 | `IMPORTED_CAMERA_READ_ONLY` |
| 400 | 400 | `IMPORTED_CAMERA_DELETE_VIA_CONNECTION` |
| 401 | 409 | `ONVIF_CAMERA_AUTH_REJECTED` |
| 402 | 409 | `ONVIF_CAMERA_TIMEOUT` |
| 403 | 400 | `CLOUD_CAMERA_REQUIRES_HLS` |

Диапазон `404–511` свободен для будущих расширений ZeroHome. Код — строка из трёх цифр и не является HTTP-статусом: например, `"401"` соответствует HTTP 409, а `"403"` — HTTP 400.
