"""List query dependency (docs/query.md, objects/settings.md).

    q_dep = list_query(model=Cam, sortable=["id", "name"], searchable=["name", "addr"],
                       expand=["owner"])        # expand=None -> 351 (EXPAND_NOT_IMPLEMENTED)

    @router.get("")
    async def list_cams(q: ListQuery = Depends(q_dep), db=Depends(get_db)):
        rows, total = await q.paginate(db, select(Cam))
        return listing("cams", [serialize(r) for r in rows], query=q, total=total)

In cursedapi v1.2 simple search methods are comparisons only and all conditions
are combined with AND. Advanced logical composition belongs to `filter`, which
remains unsupported by this dependency.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from fastapi import Query
from sqlalchemy import String, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import codes
from app.core.errors import AppError
from app.core.introspection import declare
from app.core.validation import field_error

DEFAULT_LIMIT = 25
MAX_LIMIT = 255
COMPARISONS = ("equals", "exactly", "include", "starts", "ends", "exclude")
_ORDER = {"asc": "asc", "desc": "desc"}


def split_list(raw: str, param: str) -> list[str]:
    """Split on commas honouring '...'/"..." quoting ('' / "" escape a quote inside).
    Empty positions -> 297 (SEARCH_EMPTY_POSITION); malformed quoting -> 066 (INVALID_QUERY)."""
    out: list[str] = []
    i, n = 0, len(raw)
    while True:
        if i < n and raw[i] in "'\"":
            q = raw[i]
            i += 1
            buf = []
            while True:
                if i >= n:
                    raise _bad(param, "unterminated quote", raw)
                c = raw[i]
                if c == q:
                    if i + 1 < n and raw[i + 1] == q:
                        buf.append(q)
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(c)
                i += 1
            if i < n and raw[i] != ",":
                raise _bad(param, "unexpected characters after closing quote", raw)
            tok = "".join(buf)
        else:
            j = raw.find(",", i)
            j = n if j == -1 else j
            tok = raw[i:j]
            if "'" in tok or '"' in tok:
                raise _bad(param, "quotes are only allowed around a whole value", raw)
            i = j
        if tok == "":
            raise AppError(codes.SEARCH_EMPTY_POSITION, "A multi search list contains an empty position",
                           query={param: field_error("empty position in list", value=raw)})
        out.append(tok)
        if i >= n:
            return out
        i += 1  # skip comma
        if i >= n:  # trailing comma
            raise AppError(codes.SEARCH_EMPTY_POSITION, "A multi search list contains an empty position",
                           query={param: field_error("empty position in list", value=raw)})


def _bad(param: str, msg: str, value: Any) -> AppError:
    return AppError(codes.INVALID_QUERY, "Invalid query parameters",
                    query={param: field_error(msg, value=value)})


def _int(param: str, raw: str | None, default: int, *, code: str = codes.INVALID_LIMIT_OFFSET) -> int:
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise AppError(code, "Invalid limit or offset value", query={
            param: field_error("must be an integer", type="int", actual_type="string", value=raw)})


@dataclass
class Condition:
    column: str
    method: str
    value: str
    connector: str | None  # joins this to the previous one; None for the first


@dataclass
class ListQuery:
    limit: int = DEFAULT_LIMIT
    offset: int = 0
    order: list[str] = field(default_factory=lambda: ["asc"])
    sort_by: list[str] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    expand: list[str] = field(default_factory=list)
    raw: dict[str, str | None] = field(default_factory=dict)
    columns: Mapping[str, Any] = field(default_factory=dict)  # name -> SQLAlchemy column

    def settings(self, total: int) -> dict[str, Any]:
        r = self.raw
        return {
            "total": total, "limit": self.limit, "offset": self.offset,
            "order": ",".join(self.order),
            "sort_by": r.get("sort_by"), "search": r.get("search"),
            "search_method": r.get("search_method"), "search_column": r.get("search_column"),
            "filter": r.get("filter"),
        }

    def _col(self, name: str):
        return self.columns[name]

    def where_clause(self):
        expr = None
        for c in self.conditions:
            col = self._col(c.column)
            cond = _compare(col, c.method, c.value, c.column)
            if expr is None:
                expr = cond
            elif c.connector == "and":
                expr = and_(expr, cond)
            else:
                expr = and_(expr, cond)
        return expr

    def apply(self, stmt):
        """Add WHERE (search) and ORDER BY; no paging."""
        w = self.where_clause()
        if w is not None:
            stmt = stmt.where(w)
        for i, name in enumerate(self.sort_by):
            col = self._col(name)
            direction = self.order[i] if len(self.order) > 1 else self.order[0]
            stmt = stmt.order_by(col.desc() if direction == "desc" else col.asc())
        return stmt

    async def paginate(self, db: AsyncSession, stmt):
        """Apply filters/sort, count, apply limit/offset -> (rows, total). rows = scalars."""
        stmt = self.apply(stmt)
        total = (await db.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))).scalar_one()
        rows = (await db.execute(stmt.offset(self.offset).limit(self.limit))).scalars().all()
        return rows, total


def _like_escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _compare(col, method: str, value: str, name: str):
    if method in ("equals", "exactly"):
        try:
            py = col.type.python_type
        except NotImplementedError:
            py = str
        if py is int:
            try:
                return col == int(value)
            except ValueError:
                raise AppError(codes.INVALID_QUERY, "Invalid query parameters", query={
                    "search": field_error("must be an integer for this column", type="int", value=value)})
        return col == value
    sc = cast(col, String)
    esc = _like_escape(value)
    if method == "include":
        return sc.like(f"%{esc}%", escape="\\")
    if method == "starts":
        return sc.like(f"{esc}%", escape="\\")
    if method == "ends":
        return sc.like(f"%{esc}", escape="\\")
    if method == "exclude":
        return sc.not_like(f"%{esc}%", escape="\\")
    raise AppError(codes.INVALID_QUERY, "Unknown search method")  # pragma: no cover


def _canonical_list(values: list[str]) -> str:
    def quote(value: str) -> str:
        if any(char in value for char in ",'\""):
            return "'" + value.replace("'", "''") + "'"
        return value
    return ",".join(quote(value) for value in values)


def _parse_search(search, column, method, searchable: set[str]) -> tuple[list[Condition], dict[str, str | None]]:
    if search is None and column is None and method is None:
        return [], {"search": None, "search_column": None, "search_method": None}
    if search is None or column is None:
        raise AppError(codes.INVALID_QUERY, "search and search_column must be passed together", query={
            ("search" if search is None else "search_column"): field_error("is required with the other", required=True)})
    values = split_list(search, "search")
    cols = split_list(column, "search_column")
    tokens = split_list(method, "search_method") if method is not None else []
    for t in tokens:
        if t not in COMPARISONS:
            raise AppError(codes.INVALID_QUERY, "Invalid search method", query={
                "search_method": field_error("unknown method", enum=list(COMPARISONS), value=t)})
    for c in cols:
        if c not in searchable:
            raise AppError(codes.INVALID_SEARCH_COLUMN, "Invalid search column", query={
                "search_column": field_error("column is not searchable", enum=sorted(searchable), value=c)})
    n = len(values)
    if len(cols) == 1:
        cols = cols * n
    comps = tokens or ["include"]
    if len(comps) == 1:
        comps = comps * n
    if len(cols) != n or len(comps) != n:
        raise AppError(codes.SEARCH_LENGTH_MISMATCH, "Search values, columns and methods have different lengths", query={
            "search": field_error(f"{n} values", actual_length=n),
            "search_column": field_error(f"{len(cols)} columns", actual_length=len(cols)),
            "search_method": field_error(f"{len(comps)} comparisons", actual_length=len(comps))})
    normalized = {
        "search": _canonical_list(values),
        "search_column": _canonical_list(cols),
        "search_method": _canonical_list(comps),
    }
    conditions = [Condition(cols[i], comps[i], values[i], None if i == 0 else "and") for i in range(n)]
    return conditions, normalized


def list_query(
    *,
    model: Any = None,
    sortable: Iterable[str] | Mapping[str, Any] = (),
    searchable: Iterable[str] | Mapping[str, Any] = (),
    expand: Iterable[str] | None = None,
    default_limit: int = DEFAULT_LIMIT,
    max_limit: int = MAX_LIMIT,
    disabled: Iterable[str] = (),
):
    """Build the dependency. `sortable`/`searchable`: column names (resolved with
    getattr(model, name)) or a {name: column_expression} mapping. `expand=None` means
    the endpoint has no expansion (expand param -> 351, EXPAND_NOT_IMPLEMENTED); a list = allowed relations
    (unknown -> 160, EXPAND_NOT_SUPPORTED). `disabled` = functions turned off on this server
    (-> 155, FUNCTION_DISABLED)."""
    def _cols(spec):
        if isinstance(spec, Mapping):
            return dict(spec)
        return {n: getattr(model, n) for n in spec}

    sort_cols = _cols(sortable)
    search_cols = _cols(searchable)
    all_cols = {**sort_cols, **search_cols}
    allowed_expand = set(expand) if expand is not None else None
    off = set(disabled)

    def _need(fn: str, present: bool):
        if present and fn in off:
            raise AppError(codes.FUNCTION_DISABLED, f"Function '{fn}' is disabled", query={
                fn: field_error("function is disabled")})

    def _dep(
        limit: str | None = Query(None),
        offset: str | None = Query(None),
        order: str | None = Query(None),
        sort_by: str | None = Query(None),
        search: str | None = Query(None),
        search_column: str | None = Query(None),
        search_method: str | None = Query(None),
        filter_q: str | None = Query(None, alias="filter"),
        expand_q: str | None = Query(None, alias="expand"),
    ) -> ListQuery:
        if filter_q is not None:
            raise AppError(codes.FUNCTION_NOT_SUPPORTED, "Advanced filter is not supported by this endpoint", query={
                "filter": field_error("function is not supported")})
        if (search is not None or search_column is not None or search_method is not None) and not search_cols:
            raise AppError(codes.FUNCTION_NOT_SUPPORTED, "Search is not supported by this endpoint", query={
                "search": field_error("function is not supported")})
        if sort_by is not None and not sort_cols:
            raise AppError(codes.FUNCTION_NOT_SUPPORTED, "Sorting is not supported by this endpoint", query={
                "sort_by": field_error("function is not supported")})
        _need("search", search is not None)
        _need("sort_by", sort_by is not None)
        _need("expand", expand_q is not None)

        lim = _int("limit", limit, default_limit)
        off_ = _int("offset", offset, 0)
        if lim > max_limit:
            raise AppError(codes.LIMIT_TOO_LARGE, "Limit is above the maximum", query={
                "limit": field_error("above maximum", type="int", max=max_limit, value=lim)})
        if lim < 1:
            raise AppError(codes.INVALID_LIMIT_OFFSET, "Invalid limit or offset value", query={
                "limit": field_error("must be at least 1", type="int", min=1, max=max_limit, value=lim)})
        if off_ < 0:
            raise AppError(codes.INVALID_LIMIT_OFFSET, "Invalid limit or offset value", query={
                "offset": field_error("must be 0 or more", type="int", min=0, value=off_)})

        sort_list = split_list(sort_by, "sort_by") if sort_by is not None else []
        for c in sort_list:
            if c not in sort_cols:
                raise AppError(codes.INVALID_SORT_FIELD, "Invalid sort field", query={
                    "sort_by": field_error("field is not sortable", enum=sorted(sort_cols), value=c)})
        order_list = ["asc"]
        if order is not None:
            toks = split_list(order, "order")
            for t in toks:
                if t.lower() not in _ORDER:
                    raise AppError(codes.INVALID_QUERY, "Invalid query parameters", query={
                        "order": field_error("invalid direction", enum=["asc", "desc"], value=t)})
            order_list = [_ORDER[t.lower()] for t in toks]
            if len(order_list) == 1 and sort_list:
                order_list = order_list * len(sort_list)
            if len(order_list) > 1 and len(order_list) != len(sort_list):
                raise AppError(codes.INVALID_QUERY, "Invalid query parameters", query={
                    "order": field_error("must have one value or one per sort_by column",
                                         actual_length=len(order_list), value=order)})

        conditions, normalized_search = _parse_search(search, search_column, search_method, set(search_cols))

        exp: list[str] = []
        if expand_q is not None:
            if allowed_expand is None:
                raise AppError(codes.EXPAND_NOT_IMPLEMENTED, "Expansion is not implemented on this endpoint", query={
                    "expand": field_error("function is not implemented")})
            exp = split_list(expand_q, "expand")
            bad = [e for e in exp if e not in allowed_expand]
            if bad:
                raise AppError(codes.EXPAND_NOT_SUPPORTED, "Expansion is not supported for requested relations", query={
                    "expand": field_error("unknown relation", enum=sorted(allowed_expand), value=",".join(bad))})

        return ListQuery(
            limit=lim, offset=off_, order=order_list, sort_by=sort_list,
            conditions=conditions, expand=exp, columns=all_cols,
            raw={
                "sort_by": _canonical_list(sort_list) if sort_by is not None else None,
                **normalized_search,
                "filter": None,
            },
        )

    fns = ["limit", "offset", "order", "pagination"]
    if sort_cols:
        fns.append("sort_by")
    if search_cols:
        fns += ["search", "search_column", "search_method", "multi_search"]
    if allowed_expand is not None:
        fns.append("expand")
    declare(_dep, *[f for f in fns if f not in off], max_limit=max_limit)
    return _dep
