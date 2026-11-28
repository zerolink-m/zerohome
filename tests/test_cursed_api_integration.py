import pytest

from tests.conftest import bearer, mint_test_token
from app.version import APP_VERSION

pytestmark = pytest.mark.asyncio


async def test_public_generated_handlers_preserve_envelope_and_ignore_bearer(client):
    response = await client.get("/v1/health", headers=bearer("obsolete-token"))
    assert response.status_code == 200
    assert "cursed-code" not in response.headers
    assert response.headers["cursed-version"] == "1.2"
    envelope = response.json()
    assert envelope["data"] == {"status": "ok"}
    assert envelope["metadata"]["request_id"] == response.headers["cursed-request-id"]


async def test_authorized_discovery_and_legacy_request_contract(client, mk_user):
    uid = await mk_user("session-owner@example.org")
    other_uid = await mk_user("different-owner@example.org")
    token = await mint_test_token(uid)
    own_path = f"/v1/users/{uid}/sessions/bulk/1"
    other_path = f"/v1/users/{other_uid}/sessions/bulk/1"

    denied = await client.options(other_path, headers=bearer(token))
    assert denied.headers["allow"] == "OPTIONS"
    assert "cursed-schema-url" not in denied.headers

    allowed = await client.options(own_path, headers=bearer(token))
    assert set(allowed.headers["allow"].split(", ")) == {"GET", "DELETE", "OPTIONS"}
    url = allowed.headers["cursed-schema-url"]
    assert (await client.get(url)).status_code == 401
    response = await client.get(url, headers=bearer(token))
    assert response.headers["cache-control"] == "private, no-store"
    contract = response.json()["data"]["schema"]["methods"]["DELETE"]
    assert contract["request"]["required"] is False
    schema = contract["request"]["body_schema"]
    assert schema["required"] == ["data"]
    assert schema["properties"]["options"]["properties"]["partial_allowed"]["type"] == "boolean"


async def test_generated_docs_keep_openapi_legacy_routes(client):
    document = (await client.get("/v1/openapi.json")).json()
    assert document["info"]["version"] == APP_VERSION
    assert "/v1/cams" in document["paths"]
    bearer_scheme = document["components"]["securitySchemes"]["ZeroHomeBearer"]
    assert (bearer_scheme["type"], bearer_scheme["scheme"]) == ("http", "bearer")
    for path in ("/v1/connections", "/v1/users/{user_id}/sessions"):
        assert document["paths"][path]["get"]["security"] == [{"ZeroHomeBearer": []}]
    assert not document["paths"]["/v1/health"]["get"].get("security")
    response = document["paths"]["/v1/health"]["get"]["responses"]["200"]
    envelope = response["content"]["application/json"]["schema"]
    assert envelope["properties"]["code"]["const"] == "000"
    assert "status" in envelope["properties"]["data"]["properties"]
    assert "request_id" in envelope["properties"]["metadata"]["properties"]

    markdown = await client.get("/v1/cursedapi.md")
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "`GET /v1/health`" in markdown.text
    assert "Варианты ответа" in markdown.text
