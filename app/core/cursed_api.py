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
from pydantic import Field, create_model
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.routing import compile_path

from app.core import codes
from app.core.errors import AppError, error_response, http_exception_handler, validation_error_handler
from app.core.introspection import CANONICAL_FUNCTIONS, _flatten, declare, options_headers, options_info, route_info
from app.core.response import CURSED_ENVELOPE_MARKER, build_metadata, success
from app.core.websocket_docs import WEBSOCKET_CONTRACT, websocket_markdown
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
        config["responses"] = _common_response_specs(
            self, config.get("responses", {}), public=True,
            validation=any(config.get(key) for key in ("path_schema", "query_schema", "header_schema", "input_schema")),
            body=config.get("input_schema") is not None,
        )
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


def _common_response_specs(api: ZeroHomeAPI, responses: Mapping[str, ResponseSpec], *, public=False,
                           validation=False, body=False):
    """Override the library's generic descriptions through named ResponseSpecs."""
    result = _response_specs(api, responses)
    common = [(codes.SERVER_ERROR, "Сервер не смог выполнить запрос; повторите позже или передайте request_id администратору.")]
    if not public:
        common.append((codes.FORBIDDEN, "Доступ к действию запрещён; проверьте права пользователя."))
    if validation:
        common.append((codes.VALIDATION_ERROR, "Поля запроса неверны; проверьте error.fields, error.query и error.path."))
    if body:
        common.append((codes.EMPTY_BODY, "Не передано обязательное тело запроса либо массив объектов пуст."))
    for code, description in common:
        if any(spec.resolve(api.codes).code == code and spec.content_type == "application/json" for spec in result.values()):
            continue
        name = f"zerohome_common_{code}"
        while name in result:
            name += "_"
        result[name] = ResponseSpec(codes.cursed_code(api.codes, code), description)
    return result


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
    entry.responses = _common_response_specs(api, responses or {}, validation=True)


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


def _hls_raw_header(path, status, variant):
    """FileResponse on the streaming HLS route keeps the streaming header.

    ResponseSpec maps each code to one status. The actual middleware uses 017
    for every raw status of this streaming route, including 206, 400 and 416.
    """
    if (path == "/v1/cams/{id}/hls/{segment}" and int(status) in (206, 400, 416)
            and variant.get("content_type") != "application/json"):
        variant["code"] = codes.STREAMING
        variant["code_name"] = "STREAMING"


def _example_metadata(example):
    """Examples need the metadata always added by ZeroHome's envelope builder."""
    if isinstance(example, dict) and {"code", "data", "metadata"} <= example.keys():
        example["metadata"] = {
            "request_id": "00000000-0000-4000-8000-000000000000",
            "generated_at": 1760000000,
            "duration": 0.001,
            "cursed_version": "1.2",
        }


def _drop_public_forbidden(variants):
    """The library adds a generic forbidden response even to public actions."""
    return [variant for variant in variants if variant.get("code") != codes.FORBIDDEN]


def _drop_openapi_code(responses, status, code):
    response = responses.get(status)
    if not response or "x-cursedapi-variants" not in response:
        return
    remaining = [variant for variant in response["x-cursedapi-variants"] if variant.get("code") != code]
    if not remaining:
        del responses[status]
        return
    response["x-cursedapi-variants"] = remaining
    response["description"] = "\n\n".join(dict.fromkeys(v["description"] for v in remaining))
    media = response.get("content", {}).get("application/json")
    shapes = [variant["schema"] for variant in remaining
              if variant.get("content_type") == "application/json" and "schema" in variant]
    if media and shapes:
        media["schema"] = shapes[0] if len(shapes) == 1 else {"oneOf": shapes}


def integrate_contracts(app: FastAPI, api: ZeroHomeAPI) -> None:
    """Add generated contracts to the existing OpenAPI and per-path discovery."""
    legacy = create_cursed_api()
    contracts: dict[tuple[str, str], tuple[LegacyContract, str]] = {}
    existing_options: set[tuple[str, str]] = set()
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
        auth = route_info(route)["auth"]
        # The live HLS dependency performs authorization inside a wrapper,
        # which route_info cannot see. Discovery must not treat it as public.
        if route.endpoint.__module__ == "app.routers.cams.hls":
            auth = "bearer,token"
        # FastAPI already validates these parameters. Reuse its declarations in
        # discovery instead of replacing typed ids by generic strings.
        dependent = getattr(route, "dependant", None)
        if dependent is not None and not args.get("path_schema") and dependent.path_params:
            args["path_schema"] = create_model(
                f"LegacyPath{len(contracts)}",
                **{field.name: (field.field_info.annotation, field.field_info)
                   for field in dependent.path_params},
            )

        def query_fields(dep):
            yield from dep.query_params
            for child in dep.dependencies:
                yield from query_fields(child)

        if dependent is not None:
            base = args.get("query_schema")
            existing = set(base.model_fields) if base else set()
            extra = {field.name: (field.field_info.annotation, field.field_info)
                     for field in query_fields(dependent) if field.name not in existing}
            # The legacy envelope parser accepts this query on every body route;
            # options.partial_allowed in the JSON body takes precedence.
            if args.get("input_schema") is not None and "partial_allowed" not in existing and "partial_allowed" not in extra:
                extra["partial_allowed"] = (str | None, Field(
                    default=None, description="true/false (также 1/0, yes/no, on/off); options.partial_allowed в теле имеет приоритет.",
                ))
            if extra:
                args["query_schema"] = create_model(
                    f"LegacyQuery{len(contracts)}", __base__=base, **extra,
                ) if base else create_model(f"LegacyQuery{len(contracts)}", **extra)
        args["responses"] = _common_response_specs(
            legacy, args.get("responses", {}), public=auth == "off",
            validation=any(args.get(key) for key in ("path_schema", "query_schema", "header_schema", "input_schema")),
            body=args.get("input_schema") is not None and contract.body_required,
        )
        for method in methods:
            key = (path, method)
            if key in contracts:
                raise ValueError(f"Duplicate cursedapi contract: {method} {path}")
            contracts[key] = (contract, auth)
            if getattr(route.endpoint, "zerohome_existing_options", False):
                existing_options.add(key)
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
                if type(entry.policy) is Policy and "403" in methods[method]["responses"]:
                    remaining = _drop_public_forbidden(methods[method]["responses"]["403"])
                    if remaining:
                        methods[method]["responses"]["403"] = remaining
                    else:
                        del methods[method]["responses"]["403"]
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
            if not contract.body_required:
                status = str(codes.http_status(codes.EMPTY_BODY))
                variants = [variant for variant in method_schema["responses"].get(status, ())
                            if variant.get("code") != codes.EMPTY_BODY]
                if variants:
                    method_schema["responses"][status] = variants
                else:
                    method_schema["responses"].pop(status, None)
            if auth == "off" and "403" in method_schema["responses"]:
                remaining = _drop_public_forbidden(method_schema["responses"]["403"])
                if remaining:
                    method_schema["responses"]["403"] = remaining
                else:
                    del method_schema["responses"]["403"]
            if method_schema.get("request"):
                method_schema["request"]["required"] = contract.body_required
                _legacy_request_shape(method_schema["request"]["body_schema"])
            methods[method] = method_schema
        if not detailed:
            return methods
        for schema in methods.values():
            for status, variants in schema["responses"].items():
                for variant in variants:
                    _hls_raw_header(template, status, variant)
                    _example_metadata(variant.get("example"))
                    if variant.get("content_type") == "application/json" and "body_schema" in variant:
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

    async def options_principal(
        request: Request, db: AsyncSession = Depends(get_db),
        bearer=Depends(_bearer_scheme),
    ):
        template, _ = match(request.scope["path"])
        methods = set(legacy._paths.get(template, {})) | set(api._paths.get(template, {}))
        keys = {(template, method) for method in methods}
        only_old_options = bool(keys) and keys <= existing_options
        if only_old_options and all(contracts[key][1] == "off" for key in keys):
            # Previously public OPTIONS ignored an unrelated Authorization header.
            return None
        try:
            return await _optional_principal(request, db, bearer)
        except AppError:
            if only_old_options:
                # Preserve the old advertised OPTIONS for bad credentials, but
                # withhold protected schema URLs unless the token is valid.
                return None
            raise

    async def endpoint_options(
        request: Request, principal=Depends(options_principal),
        db: AsyncSession = Depends(get_db),
    ):
        target_path = request.scope["path"]
        template, path_params = match(target_path)
        methods = await describe_methods(request, target_path, template, path_params, principal, db, detailed=False)
        info = options_info(request.scope, template=template)
        declared = set(api._paths.get(template, {})) | set(legacy._paths.get(template, {}))
        if (info["auth"] == "off" and any(
                contracts.get((template, method), (None, "off"))[1] == "bearer,token"
                for method in declared)):
            # HLS authenticates inside a nested resolver that the legacy
            # introspector cannot follow; report its actual requirement.
            info["auth"] = "bearer,token"
        # Hand-written methods without a declaration retain their existing
        # discovery behaviour. Declared methods are restricted by their policy.
        unavailable = {method for method in declared - methods.keys()
                       if (template, method) not in existing_options}
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
                    contract = contracts.get((path, method.upper()))
                    if (contract and contract[1] == "off") or (not contract and not operation.get("security")):
                        _drop_openapi_code(operation.get("responses", {}), "403", codes.FORBIDDEN)
                    if contract and not contract[0].body_required:
                        _drop_openapi_code(operation.get("responses", {}), str(codes.http_status(codes.EMPTY_BODY)), codes.EMPTY_BODY)
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
                    if contract:
                        auth = contract[1]
                        if auth == "off":
                            current.pop("security", None)
                        elif auth == "bearer,token" and path != "/v1/webrtc/ice":
                            document.setdefault("components", {}).setdefault("securitySchemes", {})[
                                "ZeroHomeQueryToken"
                            ] = {"type": "apiKey", "in": "query", "name": "token",
                                 "description": "Токен сессии или ссылка с правом на запрошенный ресурс."}
                            current["security"] = [{"ZeroHomeBearer": []}, {"ZeroHomeQueryToken": []}]
                    if contract and "requestBody" in current:
                        current["requestBody"]["required"] = contract[0].body_required
                        _legacy_request_shape(current["requestBody"]["content"]["application/json"]["schema"])
                    for status, response in operation.get("responses", {}).items():
                        for variant in response.get("x-cursedapi-variants", []):
                            _hls_raw_header(path, status, variant)
                            _example_metadata(variant.get("example"))
                            if variant.get("content_type") == "application/json" and "schema" in variant:
                                _envelope_details(variant["schema"], int(status))
                        if path == "/v1/cams/{id}/hls/{segment}" and int(status) in (206, 400, 416):
                            header = response.get("headers", {}).get("cursed-code")
                            if header:
                                header["schema"]["enum"] = [codes.STREAMING]
                        media = response.get("content", {}).get("application/json", {})
                        for example in media.get("examples", {}).values():
                            _example_metadata(example.get("value"))
                        _example_metadata(media.get("example"))
                        shape = media.get("schema", {})
                        for schema in shape.get("oneOf", [shape]):
                            _envelope_details(schema, int(status))
        document["x-websocket"] = {WEBSOCKET_CONTRACT["path"]: WEBSOCKET_CONTRACT}
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
        return Response(markdown + websocket_markdown(), media_type="text/markdown")
