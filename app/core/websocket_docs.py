"""WebSocket protocol supplement: OpenAPI has no operation for WebSocket frames."""
from __future__ import annotations

import json


WEBSOCKET_CONTRACT = {
    "path": "/v1/cams/{id}/webrtc",
    "summary": "Сигнализация WebRTC для просмотра камеры",
    "description": (
        "Соединитесь с ?token=, дождитесь серверного offer, установите его как remote description, "
        "передайте answer и затем локальные ICE-кандидаты. Видео и возможное аудио идут по WebRTC; "
        "WebSocket переносит только сигнализацию. Облачным камерам доступен HLS."
    ),
    "parameters": [
        {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}, "description": "ID камеры."},
        {"name": "token", "in": "query", "required": True, "schema": {"type": "string"},
         "description": "Токен сессии или действующей share:stream ссылки на эту камеру; передавать в заголовке недостаточно."},
    ],
    "messages": {
        "offer": {"direction": "server-to-client", "description": "Сервер отправляет после открытия соединения.",
                  "schema": {"type": "object", "required": ["type", "sdp"], "properties": {"type": {"const": "offer"}, "sdp": {"type": "string"}}},
                  "example": {"type": "offer", "sdp": "<server SDP>"}},
        "answer": {"direction": "client-to-server", "description": "Ответ клиента на полученный SDP offer.",
                   "schema": {"type": "object", "required": ["type", "sdp"], "properties": {"type": {"const": "answer"}, "sdp": {"type": "string", "minLength": 1}}},
                   "example": {"type": "answer", "sdp": "<client SDP>"}},
        "ice_candidate": {"direction": "client-to-server", "description": "ICE-кандидат браузера или null после завершения сбора; отсутствие candidate также считается null. У непустого кандидата нужен sdpMid или sdpMLineIndex.",
                          "schema": {"type": "object", "required": ["type"], "properties": {"type": {"const": "ice_candidate"}, "candidate": {"anyOf": [{"type": "null"}, {"type": "object", "properties": {"candidate": {"type": "string", "maxLength": 2048}, "sdpMid": {"type": ["string", "null"]}, "sdpMLineIndex": {"type": ["integer", "null"], "minimum": 0}, "usernameFragment": {"type": ["string", "null"]}}}]}}},
                          "example": {"type": "ice_candidate", "candidate": None}},
        "error": {"direction": "server-to-client", "description": "Ошибка сигнализации или завершение источника. После ошибки обработки answer сервер завершает сессию; некорректный ICE-кандидат сам по себе её не закрывает.",
                  "schema": {"type": "object", "required": ["type", "message"], "properties": {"type": {"const": "error"}, "message": {"type": "string"}}},
                  "example": {"type": "error", "message": "SDP отсутствует"}},
    },
    "close": [
        {"code": 1008, "description": "До accept: отключено, нет доступа, лимит или облачная камера; клиент обычно видит отказ HTTP handshake (403), а не WebSocket close frame."},
        {"code": 1012, "description": "Источник перезапущен: сервер отправляет error и предлагает переподключиться."},
        {"code": 1011, "description": "Источник недоступен: сервер отправляет error и предлагает переподключиться позже."},
    ],
}


def websocket_markdown() -> str:
    data = WEBSOCKET_CONTRACT
    lines = ["\n## WebSocket\n", f"### `WS {data['path']}`\n", data["description"] + "\n",
             "Токен сессии или `share:stream` передаётся в обязательном query-параметре `token`. "
             "Адрес `GET /v1/webrtc/ice` возвращает ICE-серверы; ему нужен Bearer в заголовке.\n",
             "Сообщения — JSON, без CursedJSON-обёртки:\n"]
    for name, message in data["messages"].items():
        lines.append(f"- **{name}** ({message['direction']}): {message['description']} Пример: `{json.dumps(message['example'], ensure_ascii=False)}`")
    lines.append("\nЗавершение:\n")
    lines.extend(f"- **{item['code']}**: {item['description']}" for item in data["close"])
    return "\n".join(lines) + "\n"
