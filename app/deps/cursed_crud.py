"""Contracts for existing CRUD handlers whose behavior must remain explicit."""
from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field, RootModel, create_model

from app.core import codes
from app.core.cursed_api import ResponseSpec, describe_contract
from app.schemas.base import StrictModel


class DeletedId(StrictModel):
    id: int


class FailedItem(StrictModel):
    id: int
    error: dict[str, Any]


class Empty(StrictModel):
    pass


class IgnoredData(StrictModel):
    """A write action can ignore data while validating the request envelope."""

    model_config = ConfigDict(extra="allow")


class PartialQuery(StrictModel):
    partial_allowed: str | None = Field(default=None, description="options.partial_allowed в теле имеет приоритет.")


class ListQueryContract(StrictModel):
    limit: int = Field(default=25, ge=1, le=255)
    offset: int = Field(default=0, ge=0)
    sort_by: str | None = None
    order: str = "asc"
    search: str | None = None
    search_column: str | None = None
    search_method: str | None = Field(default=None, description="По умолчанию include")


class ListSettings(StrictModel):
    total: int
    limit: int
    offset: int
    sort_by: str | None
    order: str
    search: str | None
    search_column: str | None
    search_method: str | None
    filter: str | None


AUTH_ERRORS = (
    codes.TOKEN_MISSING, codes.TOKEN_ERROR, codes.TOKEN_EXPIRED,
    codes.RATE_LIMITED, codes.ENDPOINT_RATE_LIMITED,
)
LIST_ERRORS = (
    codes.INVALID_QUERY, codes.INVALID_SORT_FIELD, codes.INVALID_SEARCH_COLUMN,
    codes.INVALID_LIMIT_OFFSET, codes.LIMIT_TOO_LARGE, codes.SEARCH_EMPTY_POSITION,
    codes.SEARCH_LENGTH_MISMATCH, codes.EXPAND_NOT_IMPLEMENTED,
)
BODY_ERRORS = (codes.INVALID_JSON, codes.UNSUPPORTED_BODY_FORMAT, codes.VALIDATION_ERROR)
_WRITES = {
    "create": (codes.CREATED, codes.PARTIALLY_CREATED),
    "patch": (codes.EDITED, codes.PARTIALLY_EDITED),
    "delete": (codes.DELETED, codes.PARTIALLY_DELETED),
}


def error_specs(*items, schema=Empty):
    return {code: ResponseSpec(code, codes.code_detail(code), schema=schema) for code in items}


class CrudContract:
    def __init__(self, singular, plural, read_schema, *, id_name="id", max_bulk=100):
        self.singular = singular
        self.plural = plural
        self.read_schema = read_schema
        self.max_bulk = max_bulk
        self.name = "".join(part.title() for part in singular.split("_"))
        self.id_path = self._model("IdPath", **{id_name: int})
        self.ids_path = self._model("BulkPath", ids=str)

    def _model(self, suffix, **fields):
        return create_model(self.name + suffix, __base__=StrictModel, **{
            name: (type_, ...) for name, type_ in fields.items()
        })

    def list_responses(self, *errors):
        output = self._model("ListData", **{self.plural: list[self.read_schema], "settings": ListSettings})
        return {
            "found": ResponseSpec(codes.SUCCESS, "Ресурсы найдены", schema=output),
            "empty": ResponseSpec(codes.EMPTY_RESULT, "Ресурсы не найдены", schema=output),
            **error_specs(*AUTH_ERRORS, *LIST_ERRORS, *errors),
        }

    def route(self, operation, *, summary, description="", bulk=False,
              request=None, response=None, optional_delete_body=False, errors=(),
              schema_access=None, request_query=None):
        keyed = self.plural if bulk else self.singular
        item_read = response or (DeletedId if operation == "delete" else self.read_schema)
        path = self.ids_path if bulk and operation != "create" else None
        if not bulk and operation in {"get", "patch", "delete", "action"}:
            path = self.id_path
        query = request_query or (ListQueryContract if operation == "list" else None)
        input_schema = None
        body_required = True
        if request is not None:
            if bulk:
                input_schema = create_model(self.name + request.__name__ + "BulkData", __base__=IgnoredData,
                    **{keyed: (list[request], Field(min_length=1, max_length=self.max_bulk))})
            else:
                input_schema = create_model(self.name + request.__name__ + "Data", __base__=IgnoredData,
                                            **{keyed: (request, ...)})
        if operation == "delete" and (bulk or optional_delete_body):
            input_schema = IgnoredData
            body_required = False
            query = PartialQuery

        error_codes = [*AUTH_ERRORS, *errors]
        if path is not None:
            error_codes.append(codes.VALIDATION_ERROR)
        if bulk:
            error_codes.append(codes.BULK_LIMIT_EXCEEDED)
        if input_schema is not None:
            error_codes.extend(BODY_ERRORS)
            error_codes.append(codes.INVALID_QUERY)
            if body_required:
                error_codes.append(codes.EMPTY_BODY)

        responses = error_specs(*error_codes)
        if operation in _WRITES:
            full_code, partial_code = _WRITES[operation]
        else:
            full_code, partial_code = codes.SUCCESS, None
        responses["result"] = ResponseSpec(full_code, codes.code_detail(full_code),
                                          schema=self._model(operation.title() + "ManyData", **{keyed: list[item_read]})
                                          if bulk else item_read, response_key=None if bulk else keyed)
        if not bulk and operation == "patch":
            responses["unchanged"] = ResponseSpec(codes.NOT_CHANGED, "Данные не изменились", schema=item_read, response_key=keyed)
        if bulk and partial_code is not None:
            partial = self._model(operation.title() + "Result", succeeded=list[item_read], failed=list[FailedItem])
            partial_data = self._model(operation.title() + "PartialData", **{keyed: partial})
            responses["partial"] = ResponseSpec(partial_code, "Часть записей обработана", schema=partial_data)
            # Failed write codes can describe either a plain error or an
            # all-failed bulk result. Errors still carry their current codes.
            all_failed = type(self.name + operation.title() + "ErrorData", (RootModel[partial_data | Empty],), {})
            for code in {codes.VALIDATION_ERROR, *errors}:
                responses[code] = ResponseSpec(code, codes.code_detail(code), schema=all_failed)
        if operation == "list":
            responses = self.list_responses(*errors)
        return describe_contract(
            summary=summary, description=description, input_schema=input_schema,
            body_required=body_required, path_schema=path, query_schema=query,
            responses=responses, schema_access=schema_access,
        )
