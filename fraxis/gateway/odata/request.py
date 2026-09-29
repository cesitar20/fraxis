# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
OData request -> Frappe REST v2 request (path + ``frappe.form_dict``).

    GET    /Set                      -> GET    /api/v2/document/<DocType>        (list)
    GET    /Set/$count               -> GET    /api/v2/doctype/<DocType>/count
    POST   /Set                      -> POST   /api/v2/document/<DocType>
    GET    /Set('<name>')            -> GET    /api/v2/document/<DocType>/<name>
    PATCH  /Set('<name>')  (or PUT)  -> PATCH  /api/v2/document/<DocType>/<name>
    DELETE /Set('<name>')            -> DELETE /api/v2/document/<DocType>/<name>
    GET|POST /Set('<name>')/<method> -> /api/v2/document/<DocType>/<name>/method/<method>
    GET|POST /<dotted.method>        -> /api/v2/method/<dotted.method>   (unbound function / action)

``If-Match`` on PATCH / DELETE / bound actions is checked against the document's
``modified`` (its ETag) before Frappe runs: a stale ETag answers 412.

``form_dict`` is rebuilt from scratch: v2's ``update_doc``/``create_doc`` apply the whole
``form_dict`` to the document, so no query option may leak into it.

Two dialects share this translation (``dialect`` in the route context):

* ``v4`` at ``<base_path>/odata/`` — OData V4 JSON.
* ``v2`` at ``<base_path>/odata/v2/`` — OData V2 for V2-only clients (SAP pyodata):
  ``$inlinecount``, ``MERGE`` / ``X-HTTP-Method`` tunnelling, ``/Date(ms)/`` bodies and
  FunctionImports from Fraxis Settings. Only the serialisation differs (``response.py``).
"""

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_system_timezone

from fraxis.gateway import config
from fraxis.gateway.odata import ODataError, filter_parser, functions, model
from fraxis.gateway.paths import API_V2, GATEWAY_METHOD, Route, gateway_url

SUPPORTED_OPTIONS = {"$select", "$filter", "$orderby", "$top", "$skip", "$count", "$format", "$inlinecount"}
V2_DATE_RE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")


def service_root(dialect: str = "v4") -> str:
    return gateway_url("/odata/v2" if dialect == "v2" else "/odata")


def translate(sub: str, dialect: str = "v4") -> Route:
    try:
        return _translate(sub, dialect)
    except ODataError as e:
        return Route(
            "odata",
            f"{GATEWAY_METHOD}.odata_error",
            odata={"dialect": dialect, "error": {"code": e.code, "message": str(e), "status": e.status_code}},
        )


# --- path parsing ------------------------------------------------------------------------

def _split(sub: str) -> tuple[str, str | None, str | None]:
    """``Set('a)b')/rest`` -> ("Set", "a)b", "rest"), quote-aware."""
    i = 0
    while i < len(sub) and sub[i] not in "(/":
        i += 1
    set_name, key, rest = sub[:i], None, None
    if i < len(sub) and sub[i] == "(":
        j, in_str = i + 1, False
        while j < len(sub):
            ch = sub[j]
            if ch == "'":
                if in_str and j + 1 < len(sub) and sub[j + 1] == "'":
                    j += 2
                    continue
                in_str = not in_str
            elif ch == ")" and not in_str:
                break
            j += 1
        if j >= len(sub):
            raise ODataError("Unbalanced parentheses in resource path")
        key = _parse_key(sub[i + 1 : j])
        i = j + 1
    if i < len(sub):
        if sub[i] != "/":
            raise ODataError("Invalid resource path")
        rest = sub[i + 1 :] or None
    return set_name, key, rest


def _parse_key(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("name="):
        raw = raw[len("name=") :].strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    if not raw:
        raise ODataError("Empty key")
    return raw


# --- value conversion ----------------------------------------------------------------------

def _system_tz() -> ZoneInfo:
    return ZoneInfo(get_system_timezone())


def to_system_datetime(value: str) -> str:
    """ISO 8601 (with or without offset) -> naive system-timezone ``YYYY-MM-DD HH:MM:SS``."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ODataError(f"Invalid datetime: {value!r}")
    if dt.tzinfo:
        dt = dt.astimezone(_system_tz()).replace(tzinfo=None)
    return dt.isoformat(sep=" ")


def _literal_value(lit, prop: model.Prop):
    if lit.kind == "bool":
        return 1 if lit.value else 0
    if lit.kind == "datetime":
        return to_system_datetime(lit.value)
    if lit.kind == "date" and prop.edm == "Edm.DateTimeOffset":
        return f"{lit.value} 00:00:00"
    return lit.value


def _frappe_conditions(conds, entity: model.EntityType) -> list:
    out = []
    for field, op, value in conds:
        prop = entity.props.get(field)
        if not prop:
            raise ODataError(f"Unknown property in $filter: {field!r}")
        if isinstance(value, list):
            value = [_literal_value(v, prop) for v in value]
        else:
            value = _literal_value(value, prop)
        out.append([entity.doctype, field, op, value])
    return out


def _from_v2_date(value: str, prop: model.Prop):
    """``/Date(ms)/`` (UTC, optional ``+hhmm``) -> Frappe date / system-timezone datetime."""
    m = V2_DATE_RE.match(value)
    dt = datetime.fromtimestamp(int(m.group(1)) / 1000, tz=ZoneInfo("UTC"))
    if prop.fieldtype == "Date":
        return dt.date().isoformat()
    return dt.astimezone(_system_tz()).replace(tzinfo=None).isoformat(sep=" ")


def convert_body(body: dict, entity_props: dict[str, model.Prop], collections: dict[str, str]) -> dict:
    """Strip OData annotations (V4 ``@…``, V2 ``__metadata``) and convert dates to Frappe's format."""
    clean = {}
    for key, value in body.items():
        if key.startswith(("@", "__")) or key == "doctype":
            continue
        prop = entity_props.get(key)
        if prop and isinstance(value, str) and V2_DATE_RE.match(value):
            value = _from_v2_date(value, prop)
        elif prop and prop.edm == "Edm.DateTimeOffset" and isinstance(value, str) and "T" in value:
            value = to_system_datetime(value)
        elif key in collections and isinstance(value, list):
            child_props = model.complex_type(collections[key])
            value = [convert_body(row, child_props, {}) if isinstance(row, dict) else row for row in value]
        clean[key] = value
    return clean


# --- query options -------------------------------------------------------------------------

def _select(entity: model.EntityType, raw: str | None) -> list[str]:
    if not raw:
        return list(entity.props)
    fields = [f.strip() for f in raw.split(",") if f.strip()]
    for f in fields:
        if f not in entity.props and f not in entity.collections:
            raise ODataError(f"Unknown property in $select: {f!r}")
    return fields


def _orderby(entity: model.EntityType, raw: str | None) -> str | None:
    if not raw:
        return None
    parts = []
    for item in raw.split(","):
        bits = item.split()
        if not bits or len(bits) > 2 or bits[0] not in entity.props:
            raise ODataError(f"Invalid $orderby item: {item.strip()!r}")
        direction = bits[1].lower() if len(bits) == 2 else "asc"
        if direction not in ("asc", "desc"):
            raise ODataError(f"Invalid $orderby direction: {bits[1]!r}")
        parts.append(f"`tab{entity.doctype}`.`{bits[0]}` {direction}")
    return ", ".join(parts)


def _int_option(args: dict, key: str, default: int) -> int:
    raw = args.get(key)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ODataError(f"{key} must be a non-negative integer")
    if value < 0:
        raise ODataError(f"{key} must be a non-negative integer")
    return value


# --- concurrency ---------------------------------------------------------------------------

def etag_for(modified) -> str:
    """Weak ETag from ``modified`` exactly as Frappe serialises it (``str(datetime)``)."""
    return f'W/"{modified}"'


def _check_if_match(doctype: str, name: str) -> None:
    header = frappe.local.request.headers.get("If-Match")
    if not header or header.strip() == "*":
        return
    modified = frappe.db.get_value(doctype, name, "modified")
    if modified is None:
        return  # let Frappe answer 404
    current = etag_for(modified)
    if current not in [t.strip() for t in header.split(",")]:
        raise ODataError("The entity was modified by someone else (ETag mismatch)", 412, "PreconditionFailed")


# --- main ----------------------------------------------------------------------------------

def _unbound_operation(sub: str, method: str, args, body, dialect: str) -> Route:
    """``/odata/<dotted.method>``: whitelisted method as an OData function (GET) / action (POST)."""
    if method not in ("GET", "POST"):
        raise ODataError("Functions use GET and actions use POST", 405, "MethodNotAllowed")
    params = dict(body) if method == "POST" else {k: v for k, v in args.items() if not k.startswith("$")}
    frappe.local.form_dict = frappe._dict({k: v for k, v in params.items() if not k.startswith("@")})
    ctx = {"shape": "operation", "root": service_root(dialect), "dialect": dialect}
    return Route("odata", f"{API_V2}/method/{sub}", odata=ctx)


def _function_import(fn: dict, method: str, args, body) -> Route:
    """V2 FunctionImport: parameters are URI literals in the query string (GET and POST)."""
    if method != fn["http_method"]:
        raise ODataError(f"{fn['name']} must be called with {fn['http_method']}", 405, "MethodNotAllowed")
    params = {k: functions.parse_literal(v) for k, v in args.items() if not k.startswith("$")}
    if method == "POST" and isinstance(body, dict):
        params.update({k: v for k, v in body.items() if not k.startswith(("@", "__"))})
    frappe.local.form_dict = frappe._dict(params)
    ctx = {"shape": "operation", "root": service_root("v2"), "dialect": "v2", "function": fn["name"]}
    return Route("odata", f"{API_V2}/method/{fn['method']}", odata=ctx)


def _v2_effective_method(request) -> str:
    """V2 clients update with MERGE, or tunnel verbs through POST + ``X-HTTP-Method``.

    Frappe's router and its commit-on-unsafe-method logic only know PATCH / PUT / DELETE, so
    the request is rewritten to the real verb before Frappe dispatches it.
    """
    method = request.method
    tunnelled = (request.headers.get("X-HTTP-Method") or "").upper()
    if method == "POST" and tunnelled in ("MERGE", "PATCH", "PUT", "DELETE"):
        method = tunnelled
    if method == "MERGE":
        method = "PATCH"
    if method != request.method:
        request.environ["REQUEST_METHOD"] = method
        request.method = method
    return method


def _translate(sub: str, dialect: str = "v4") -> Route:
    request = frappe.local.request
    method = _v2_effective_method(request) if dialect == "v2" else request.method
    args = request.args

    for key in args:
        if key.startswith("$") and key not in SUPPORTED_OPTIONS:
            raise ODataError(f"Query option {key} is not supported", 501, "NotImplemented")
    if args.get("$format") not in (None, "json", "application/json"):
        raise ODataError("Only $format=json is supported", 406, "NotAcceptable")

    body = frappe.form_dict if request.is_json else {}
    if isinstance(body, dict) and "data" in body and isinstance(body.get("data"), list):
        raise ODataError("Request body must be a JSON object")

    if dialect == "v2" and (fn := functions.v2_functions().get(sub)):
        return _function_import(fn, method, args, body)
    if "." in sub and "(" not in sub and "/" not in sub:
        return _unbound_operation(sub, method, args, body, dialect)

    set_name, key, rest = _split(sub)
    doctype = model.resolve_set(set_name)
    if not doctype:
        raise ODataError(f"Resource not found for the segment {set_name!r}", 404, "NotFound")

    entity = model.entity_type(doctype)
    ctx = {"doctype": doctype, "set": set_name, "root": service_root(dialect), "dialect": dialect}

    # Collection -----------------------------------------------------------------------
    if key is None and rest is None:
        if method == "GET":
            filters, or_filters = [], []
            if args.get("$filter"):
                and_c, or_c = filter_parser.to_frappe(args["$filter"])
                filters, or_filters = _frappe_conditions(and_c, entity), _frappe_conditions(or_c, entity)
            select = [f for f in _select(entity, args.get("$select")) if f not in entity.collections]
            if "name" not in select:
                select.insert(0, "name")
            top = min(
                _int_option(args, "$top", int(config.get("odata_page_size"))),
                int(config.get("odata_max_page_size")),
            )
            skip = _int_option(args, "$skip", 0)
            if top == 0:
                raise ODataError("$top must be greater than 0")
            frappe.local.form_dict = frappe._dict(
                fields=json.dumps(select),
                filters=filters,
                or_filters=or_filters,
                order_by=_orderby(entity, args.get("$orderby")) or f"`tab{doctype}`.`modified` desc",
                limit=top,
                limit_start=skip,
            )
            ctx.update(
                shape="collection",
                count=str(args.get("$count", "")).lower() == "true"
                or str(args.get("$inlinecount", "")).lower() == "allpages",
                filters=filters,
                or_filters=or_filters,
                top=top,
                skip=skip,
            )
            return Route("odata", f"{API_V2}/document/{doctype}", odata=ctx)
        if method == "POST":
            frappe.local.form_dict = frappe._dict(convert_body(body, entity.props, entity.collections))
            ctx.update(shape="created")
            return Route("odata", f"{API_V2}/document/{doctype}", odata=ctx)
        raise ODataError(f"{method} is not allowed on a collection", 405, "MethodNotAllowed")

    # Collection count -----------------------------------------------------------------
    if key is None and rest == "$count":
        if method != "GET":
            raise ODataError("$count only supports GET", 405, "MethodNotAllowed")
        filters, or_filters = [], []
        if args.get("$filter"):
            and_c, or_c = filter_parser.to_frappe(args["$filter"])
            filters, or_filters = _frappe_conditions(and_c, entity), _frappe_conditions(or_c, entity)
        frappe.local.form_dict = frappe._dict(filters=filters, or_filters=or_filters)
        ctx.update(shape="count")
        return Route("odata", f"{API_V2}/doctype/{doctype}/count", odata=ctx)

    if key is None:
        raise ODataError(f"Resource not found for the segment {rest!r}", 404, "NotFound")

    # Single entity --------------------------------------------------------------------
    target = f"{API_V2}/document/{doctype}/{key}"
    if rest is None:
        if method == "GET":
            select = args.get("$select")
            ctx.update(shape="entity", select=_select(entity, select) if select else None, key=key)
            frappe.local.form_dict = frappe._dict()
        elif method == "PATCH":
            _check_if_match(doctype, key)
            frappe.local.form_dict = frappe._dict(convert_body(body, entity.props, entity.collections))
            ctx.update(shape="entity", select=None, key=key, write=True)
        elif method == "PUT":
            # OData PUT replaces the whole entity; Frappe only merges. Refuse rather than
            # silently turning a replace into a partial update.
            raise ODataError("PUT (full replacement) is not supported; use PATCH", 405, "MethodNotAllowed")
        elif method == "DELETE":
            _check_if_match(doctype, key)
            frappe.local.form_dict = frappe._dict()
            ctx.update(shape="deleted", key=key)
        else:
            raise ODataError(f"{method} is not allowed on an entity", 405, "MethodNotAllowed")
        return Route("odata", target, odata=ctx)

    # Bound action / function (whitelisted controller method) --------------------------
    if "/" in rest or rest.startswith("$"):
        raise ODataError(f"Resource not found for the segment {rest!r}", 404, "NotFound")
    if method not in ("GET", "POST"):
        raise ODataError("Bound operations support GET or POST", 405, "MethodNotAllowed")
    if rest.startswith(f"{model.NAMESPACE}."):
        rest = rest[len(model.NAMESPACE) + 1 :]  # namespace-qualified OData action name
    if method == "POST":
        _check_if_match(doctype, key)
    params = dict(body) if method == "POST" else {k: v for k, v in args.items() if not k.startswith("$")}
    frappe.local.form_dict = frappe._dict({k: v for k, v in params.items() if not k.startswith("@")})
    ctx.update(shape="action", key=key)
    return Route("odata", f"{target}/method/{rest}", odata=ctx)
