"""Preserve existing ZeroHome list parameters on generated CRUD routes."""
from __future__ import annotations

from cursedapi.filtering import FilterLimits, Query
from cursedapi.filtering.advanced import parse_filter
from cursedapi.filtering.query import SearchTerm
from cursedapi.protocols.v1_2 import Profile12

from app.core import codes
from app.core.errors import AppError
from app.core.validation import field_error
from app.deps.list_query import list_query


class ZeroHomeProfile12(Profile12):
    def parse_query(self, params, **options):
        if not options.get("collection", True) or not options.get("pagination", True):
            return super().parse_query(params, **options)

        # The existing parser validates the documented optional search method
        # and supplies the response settings in the form used by API clients.
        expand_fields = options.get("expand_fields")
        parse_legacy = list_query(
            sortable=dict.fromkeys(options.get("sort_fields", ())),
            searchable=dict.fromkeys(options.get("search_fields", ())),
            expand=expand_fields or None,
            default_limit=options.get("default_limit", 25),
            max_limit=options.get("max_limit", 255),
        )
        legacy = parse_legacy(
            limit=params.get("limit"), offset=params.get("offset"), order=params.get("order"),
            sort_by=params.get("sort_by"), search=params.get("search"),
            search_column=params.get("search_column"), search_method=params.get("search_method"),
            filter_q=None, expand_q=params.get("expand"),
        )
        filter_value = filter_echo = None
        raw = params.get("filter")
        if raw is not None:
            getlist = getattr(params, "getlist", None)
            if getlist is not None and len(getlist("filter")) != 1:
                raise AppError(codes.INVALID_QUERY, "Invalid query parameters", query={
                    "filter": field_error("supply this parameter only once"),
                })
            allowed = options.get("filter_fields")
            if not allowed:
                raise AppError(codes.FUNCTION_NOT_SUPPORTED, "Advanced filter is not supported", query={
                    "filter": field_error("function is not supported"),
                })
            filter_value, filter_echo = parse_filter(raw, allowed, options.get("filter_limits") or FilterLimits())

        fields = None
        if options.get("response_fields") is not None and params.get("fields") is not None:
            fields = super().parse_query({"fields": params["fields"]}, **options).fields

        return Query(
            version=self.version, limit=legacy.limit, offset=legacy.offset,
            sort=tuple(zip(legacy.sort_by, legacy.order, strict=False)),
            search=tuple(SearchTerm(c.column, c.value, c.method, c.connector or "and")
                         for c in legacy.conditions),
            expand=tuple(legacy.expand), fields=fields, filter=filter_value,
            sort_echo=legacy.raw.get("sort_by"), order_echo=",".join(legacy.order),
            search_echo=tuple(legacy.raw.get(key) for key in ("search", "search_column", "search_method")),
            filter_echo=filter_echo, default_limit=options.get("default_limit", 25),
        )
