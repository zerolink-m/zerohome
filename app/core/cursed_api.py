"""Connect cursedapi-server to the existing ZeroHome HTTP and database pipeline."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

from cursedapi.auth import Policy
from cursedapi.execution import Context, invoke
from cursedapi.fastapi import CursedAPI, ResponseSpec
from cursedapi.fastapi.documentation import markdown_document
from cursedapi.fastapi.schema import endpoint_schema
from cursedapi.filtering import FilterLimits, filter_schema
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.routing import compile_path

from app.core import codes
from app.core.errors import AppError, error_response, http_exception_handler, validation_error_handler
from app.core.introspection import CANONICAL_FUNCTIONS, _flatten, declare, options_headers, options_info, route_info
from app.core.response import CURSED_ENVELOPE_MARKER, build_metadata, success
from app.db.session import get_db
from app.deps.auth import get_bearer_token, require_access
from app.deps.cursed_query import ZeroHomeProfile12
from app.version import APP_VERSION

_session: ContextVar[AsyncSession] = ContextVar("cursedapi_db_session")
_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="ZeroHomeBearer")


@contextmanager
def _session_scope(db: AsyncSession):
    token = _session.set(db)
    try:
        yield
    finally:
        _session.reset(token)


async def _bind_db(db: AsyncSession = Depends(get_db)):
    with _session_scope(db):
        yield


async def _optional_principal(
    request: Request, db: AsyncSession = Depends(get_db),
    _bearer=Depends(_bearer_scheme),
):
    # HTTPBearer declares the OpenAPI scheme. The service parser owns validation
    # and preserves existing error codes and headers for invalid credentials.
    authorization = request.headers.get("authorization")
    if authorization is None:
        return None
    return await require_access(request, get_bearer_token(authorization), db)


class Authenticated(Policy):
    def authorize(self, ctx):
        if ctx.principal is None:
            if ctx.request.scope.get("cursed_schema_discovery"):
                return False
            get_bearer_token(None)
        return True


class ZeroHomeAPI(CursedAPI):
    """Use the shared middleware for headers and preserve application errors."""

    def add_resource(self, path, **config):
        config.setdefault("policy", Authenticated())
        return super().add_resource(path, **config)

    def public_action(self, method, path, **config):
        """Register a public action that does not attempt bearer authentication."""
        register = self.action(method, path, principal=lambda: None, **config)

        def decorate(handler):
            register(handler)
            self._paths[path][method.upper()].discoverable = True
            return handler
        return decorate

    def json(self, code, *, metadata=None, headers=None, **kwargs):
        response = super().json(code, metadata=build_metadata(metadata), headers=headers, **kwargs)
        response.headers[CURSED_ENVELOPE_MARKER] = "1"
        return response

    async def handle_exception(self, request, exc):
        if isinstance(exc, AppError):
            return error_response(exc)
        if isinstance(exc, RequestValidationError):
            return await validation_error_handler(request, exc)
        if isinstance(exc, HTTPException):
            return await http_exception_handler(request, exc)
        return await super().handle_exception(request, exc)


def create_cursed_api() -> ZeroHomeAPI:
    api = ZeroHomeAPI(
        title="ZeroHome",
        version=APP_VERSION,
        spec_version="1.2",
        profile=ZeroHomeProfile12(),
        principal=_optional_principal,
        session_factory=_session.get,
        openapi_url=None,
        markdown_url=None,
    )
    codes.register_cursed_codes(api.codes)
    api.router.dependencies.append(Depends(_bind_db))
    return api


def declare_generated_route(api, path, method, *features, **meta):
    """Expose the capabilities of a generated route to ZeroHome's OPTIONS."""
    for route in api.router.routes:
        if route.path == path and method in route.methods:
            declare(route.endpoint, *features, **meta)
            return
    raise ValueError(f"Unknown generated route: {method} {path}")


@dataclass(frozen=True)
class LegacyContract:
    arguments: Mapping[str, Any]
    body_required: bool
    schema_access: Callable | None


def describe_contract(*, body_required=True, schema_access=None, **kwargs):
    """Describe a FastAPI handler while retaining its existing implementation.

    input_schema models the object inside data. ResponseSpec models either the
    object inside data or the value of the named response_key.
    """
    def decorate(endpoint):
        endpoint.zerohome_contract = LegacyContract(kwargs, body_required, schema_access)
        return endpoint
    return decorate


def _response_specs(api: ZeroHomeAPI, responses: Mapping[str, ResponseSpec]):
    def resolve(spec: ResponseSpec):
        if isinstance(spec.code, str) and spec.code.isdecimal():
            return replace(spec, code=codes.cursed_code(api.codes, spec.code))
        return spec

    return {name: resolve(spec) for name, spec in responses.items()}


def describe_generated_route(
    api: ZeroHomeAPI, path: str, method: str, *, summary="", description="",
    query_schema=None, header_schema=None, path_schema=None, responses=None,
):
    """Supply the domain-specific contract for an autogenerated operation."""
    entry = api._paths[path][method]
    entry.summary = summary
    entry.description = description
    entry.parameters = {location: model for location, model in (
        ("path", path_schema), ("query", query_schema), ("headers", header_schema),
    ) if model is not None}
    entry.responses = _response_specs(api, responses or {})


def _documented_handler():
    raise RuntimeError("A legacy contract is documentation only")


def _legacy_request_shape(schema):
    """Match app.deps.request_body.read_envelope, including nullable options."""
    schema.pop("additionalProperties", None)
    schema["properties"]["options"] = {
        "type": ["object", "null"],
        "properties": {"partial_allowed": {"type": "boolean"}},
    }


def _envelope_details(schema, status):
    properties = schema.get("properties")
    if properties is None:
        return
    if status >= 400:
        field_error = {
            "type": "object",
            "required": ["error"],
            "properties": {
                "error": {"type": "string"},
                "required": {"type": "boolean"},
                "type": {"type": "string"},
                "actual_type": {"type": "string"},
                "value": {},
            },
        }
        properties["error"] = {
            "type": "object",
            "required": ["error"],
            "properties": {
                "error": {"type": "string"},
                **{key: {"type": "object", "additionalProperties": field_error}
                   for key in ("fields", "query", "path", "headers")},
                "hint": {"type": "string"},
                "docs": {"type": "string"},
                "request_schema": {"type": "object"},
                "request_example": {"type": "object"},
            },
        }
    else:
        properties["error"] = {"type": "object", "maxProperties": 0}
    properties["metadata"] = {
        "type": "object",
        "properties": {
            "request_id": {"type": "string", "format": "uuid"},
            "generated_at": {"type": "integer"},
            "duration": {"type": "number"},
            "cursed_version": {"const": "1.2"},
            "deprecated": {"type": "boolean"},
            "expand": {"type": "array", "items": {"type": "string"}},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }


def integrate_contracts(app: FastAPI, api: ZeroHomeAPI) -> None:
    """Add generated contracts to the existing OpenAPI and per-path discovery."""
    legacy = create_cursed_api()
    contracts: dict[tuple[str, str], tuple[LegacyContract, str]] = {}
    known_paths = set()
    for route in _flatten(app.router.routes):
        methods = getattr(route, "methods", None)
        if not methods:
            continue
        path = route.path
        known_paths.add(path)
        if methods & api._paths.get(path, {}).keys():
            raise ValueError(f"Generated cursedapi methods conflict with an existing route: {path}")
        contract = getattr(getattr(route, "endpoint", None), "zerohome_contract", None)
        if contract is None:
            continue
        args = dict(contract.arguments)
        args["responses"] = _response_specs(legacy, args.get("responses", {}))
        for method in methods:
            key = (path, method)
            if key in contracts:
                raise ValueError(f"Duplicate cursedapi contract: {method} {path}")
            contracts[key] = (contract, route_info(route)["auth"])
            legacy.action(method, path, **args)(_documented_handler)

    # Publish schemas solely for routes with declared contracts.
    paths = set(api._paths) | set(legacy._paths)
    ordered_paths = sorted(paths, key=lambda path: (tuple(
        part.startswith("{") for part in path.split("/")
    ), path))
    all_paths = sorted(known_paths | paths, key=lambda path: (
        tuple(part.startswith("{") for part in path.split("/")),
        path not in paths,
        path,
    ))
    patterns = [(path, compile_path(path)[0]) for path in all_paths]

    def match(path):
        for template, pattern in patterns:
            match_ = pattern.fullmatch(path)
            if match_:
                if template in paths:
                    return template, match_.groupdict()
                break
        return None, {}

    app.state.cursed_has_contract = lambda path: match(path)[0] is not None

    async def describe_methods(request, target_path, template, path_params, principal, db, *, detailed=True):
        target_request = Request({
            **request.scope,
            "path": target_path,
            "path_params": path_params,
            "cursed_schema_discovery": True,
        })
        if template in api._paths:
            with _session_scope(db):
                capabilities = await api._capabilities(target_request, principal, template)
            methods = {}
            for method, capability in capabilities.items():
                if not detailed:
                    methods[method] = capability[1]
                    continue
                methods[method] = api._describe(*capability)
                entry, _, filters, _, _ = capability
                if filters:
                    limits = entry.resource.filter_limits or FilterLimits()
                    methods[method].setdefault("query", {})["filter"] = filter_schema(filters, limits)
        else:
            methods = {}
        for method, entry in legacy._paths.get(template, {}).items():
            contract, auth = contracts[(template, method)]
            if principal is None and auth != "off":
                continue
            if contract.schema_access is not None:
                context = Context(target_request, legacy.codes, principal, "schema", session=db)
                if not await invoke(contract.schema_access, context):
                    continue
            if not detailed:
                methods[method] = None
                continue
            method_schema = endpoint_schema(entry, {}, legacy.codes)
            if method_schema.get("request"):
                method_schema["request"]["required"] = contract.body_required
                _legacy_request_shape(method_schema["request"]["body_schema"])
            methods[method] = method_schema
        if not detailed:
            return methods
        for schema in methods.values():
            for status, variants in schema["responses"].items():
                for variant in variants:
                    if "body_schema" in variant:
                        _envelope_details(variant["body_schema"], int(status))
        return methods

    @app.get("/v1/_cursed/schema/{operation_path:path}", include_in_schema=False)
    async def endpoint_contract(
        request: Request, operation_path: str,
        principal=Depends(_optional_principal),
        db: AsyncSession = Depends(get_db),
    ):
        target_path = "/" + operation_path
        template, path_params = match(target_path)
        if template is None:
            raise AppError(codes.ROUTE_NOT_FOUND, "Route not found")
        methods = await describe_methods(request, target_path, template, path_params, principal, db)
        if not methods:
            if principal is None:
                get_bearer_token(None)
            raise AppError(codes.OBJECT_NOT_FOUND, "Endpoint schema not found")
        _, _, converters = compile_path(template)
        route_parameters = {
            name: {"type": "string", "required": True} for name in converters
        }
        for method in methods.values():
            for name, field in method.get("path", {}).items():
                schema = field.get("schema", {})
                if isinstance(schema.get("type"), str):
                    route_parameters[name]["type"] = schema["type"]
                if schema.get("description"):
                    route_parameters[name]["description"] = schema["description"]
        return success({"schema": {
            "path_template": template,
            "path_parameters": route_parameters,
            "methods": methods,
        }}, headers={"Cache-Control": "private, no-store"})

    async def endpoint_options(
        request: Request, principal=Depends(_optional_principal),
        db: AsyncSession = Depends(get_db),
    ):
        target_path = request.scope["path"]
        template, path_params = match(target_path)
        methods = await describe_methods(request, target_path, template, path_params, principal, db, detailed=False)
        info = options_info(request.scope, template=template)
        declared = set(api._paths.get(template, {})) | set(legacy._paths.get(template, {}))
        # Hand-written methods without a declaration retain their existing
        # discovery behaviour. Declared methods are restricted by their policy.
        unavailable = declared - methods.keys()
        info["methods"] = [method for method in info["methods"] if method not in unavailable]
        info["method_functions"] = {
            method: (methods[method] if methods.get(method) is not None else features)
            for method, features in info["method_functions"].items()
            if method not in unavailable
        }
        order = {name: index for index, name in enumerate(CANONICAL_FUNCTIONS)}
        info["functions"] = [
            f"{method}.{feature}"
            for method, features in sorted(info["method_functions"].items())
            for feature in sorted(features, key=lambda name: (order.get(name, len(order)), name))
        ]
        headers = options_headers(info)
        headers["Cache-Control"] = "private, no-store"
        if methods:
            headers["cursed-schema-url"] = str(request.url_for(
                "endpoint_contract", operation_path=target_path.lstrip("/"),
            ))
        return success({"endpoint": {
            "path": target_path,
            "methods": info["methods"],
            "supported_functions": info["functions"],
        }}, headers=headers)

    for path in ordered_paths:
        app.add_api_route(path, endpoint_options, methods=["OPTIONS"], include_in_schema=False)
    # Route discovery through the application's policy-aware contract endpoint.
    api.router.routes[:] = [
        route for route in api.router.routes
        if "OPTIONS" not in route.methods and not route.path.startswith("/_cursed/")
    ]
    app.include_router(api.router)

    previous_openapi = app.openapi
    last_document = None

    def openapi():
        nonlocal last_document
        document = previous_openapi()
        if last_document is document:
            return document
        for source in (api.openapi(), legacy.openapi()):
            document["x-cursedapi-version"] = source["x-cursedapi-version"]
            document.setdefault("components", {}).setdefault("schemas", {}).update(
                source.get("components", {}).get("schemas", {})
            )
            for path, methods in source["paths"].items():
                for method, operation in methods.items():
                    # OPTIONS and schema helper paths are excluded by the library.
                    current = document.setdefault("paths", {}).setdefault(path, {}).setdefault(method, {})
                    current_parameters = current.get("parameters", [])
                    generated_parameters = {
                        (parameter["name"].casefold(), parameter["in"]): parameter
                        for parameter in operation.get("parameters", [])
                    }
                    for parameter in current_parameters:
                        generated_parameters.setdefault((parameter["name"].casefold(), parameter["in"]), parameter)
                    current.update(operation)
                    if generated_parameters:
                        current["parameters"] = list(generated_parameters.values())
                    contract = contracts.get((path, method.upper()))
                    if contract and "requestBody" in current:
                        current["requestBody"]["required"] = contract[0].body_required
                        _legacy_request_shape(current["requestBody"]["content"]["application/json"]["schema"])
                    for status, response in operation.get("responses", {}).items():
                        for variant in response.get("x-cursedapi-variants", []):
                            if "schema" in variant:
                                _envelope_details(variant["schema"], int(status))
                        media = response.get("content", {}).get("application/json", {})
                        shape = media.get("schema", {})
                        for schema in shape.get("oneOf", [shape]):
                            _envelope_details(schema, int(status))
        last_document = document
        return document

    app.openapi = openapi

    @app.get("/v1/cursedapi.md", include_in_schema=False)
    async def markdown_contracts():
        document = openapi()
        known_paths = {}
        for catalog in (api, legacy):
            for path, methods in catalog._paths.items():
                known_paths.setdefault(compile_path(path)[1], set()).update(methods)
        declared_paths = {
            path: {method: operation for method, operation in document["paths"][path].items()
                   if method.upper() in methods}
            for path, methods in known_paths.items() if path in document["paths"]
        }
        markdown = markdown_document(api, {
            **document,
            "info": {
                **document["info"],
                "description": "Автоматическая документация эндпоинтов с зарегистрированными контрактами.",
            },
            "paths": declared_paths,
        })
        return Response(markdown, media_type="text/markdown")
