# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
OData V2 serialisation (JSON "verbose" format) for ``<base_path>/odata/v2/``.

Same Frappe responses as the V4 dialect, different envelope — what V2 clients such as SAP
pyodata parse:

    collection  {"d": {"results": [...], "__count": "12", "__next": "<url>"}}
    entity      {"d": {..., "__metadata": {"uri", "type", "etag"}}}
    create      201 + {"d": {...}}          update / delete   204, no body
    function    {"d": {"<FunctionName>": value}}   (dotted operations: {"d": value})
    error       {"error": {"code": "...", "message": {"lang": "en", "value": "..."}}}
    Edm.DateTime  /Date(<ms since epoch, UTC>)/      Edm.Int64  string
"""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fraxis.gateway.odata import model
from fraxis.gateway.odata.request import _system_tz, etag_for
from fraxis.gateway.odata.response import _count, _entity_url, _error_payload, _next_link, _odata_status

HEADERS = {"DataServiceVersion": "2.0"}
JSON_MIME = "application/json;charset=utf-8"
UTC = ZoneInfo("UTC")


def _v2_date(value, fieldtype: str):
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value) if fieldtype == "Date" else datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=_system_tz())
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day, tzinfo=UTC)  # Edm.DateTime at midnight UTC
    else:
        return value
    return f"/Date({int(dt.timestamp() * 1000)})/"


def _value(prop: model.Prop, value):
    if value is None:
        return None
    edm = model.edm_v2(prop)
    if edm == "Edm.DateTime":
        return _v2_date(value, prop.fieldtype)
    if edm == "Edm.Boolean":
        return bool(value)
    if edm == "Edm.Int64":
        return str(int(value))
    if edm == "Edm.Int32":
        return int(value)
    return value


def entity(row: dict, props: dict[str, model.Prop], ctx: dict) -> dict:
    out = {key: _value(props[key], value) for key, value in row.items() if key in props}
    metadata = {"type": f"{model.NAMESPACE}.{ctx['set']}"}
    if row.get("name"):
        metadata["uri"] = _entity_url(ctx, row["name"])
    if row.get("modified"):
        metadata["etag"] = etag_for(row["modified"])
    return {"__metadata": metadata, **out}


def error_payload(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": {"lang": "en", "value": message}}}


def _set_json(response, payload: dict, status: int | None = None) -> None:
    response.set_data(json.dumps(payload, default=str, separators=(",", ":")))
    response.headers["Content-Type"] = JSON_MIME
    if status:
        response.status_code = status


def _no_content(response) -> None:
    response.set_data(b"")
    response.status_code = 204
    response.headers.pop("Content-Type", None)


def reshape(response, ctx: dict) -> None:
    for header, value in HEADERS.items():
        response.headers[header] = value
    response.headers.pop("OData-Version", None)

    status = response.status_code
    try:
        body = json.loads(response.get_data() or b"{}")
    except ValueError:
        return

    if status >= 400:
        if "error" in body and isinstance(body["error"].get("message"), dict):
            return  # already V2-shaped (fraxis.gateway.api.odata_error)
        v4 = _error_payload(body, status)["error"] if "error" not in body else body["error"]
        _set_json(response, error_payload(v4["code"], v4["message"]), _odata_status(body, status))
        return
    if "error" in ctx:
        return

    data = body.get("data")
    shape = ctx.get("shape")
    if shape in ("action", "operation"):
        _set_json(response, {"d": {ctx["function"]: data} if ctx.get("function") else data})
        return

    et = model.entity_type(ctx["doctype"])

    if shape == "collection":
        rows = data or []
        payload = {"results": [entity(r, et.props, ctx) for r in rows]}
        if ctx.get("count") and (count := _count(ctx)) is not None:
            payload["__count"] = str(count)
        if next_link := _next_link(ctx, len(rows)):
            payload["__next"] = next_link
        _set_json(response, {"d": payload})

    elif shape == "created":
        row = entity(data or {}, et.props, ctx)
        if uri := row["__metadata"].get("uri"):
            response.headers["Location"] = uri
        if etag := row["__metadata"].get("etag"):
            response.headers["ETag"] = etag
        _set_json(response, {"d": row}, 201)

    elif shape == "entity":
        row = entity(data or {}, et.props, ctx)
        if etag := row["__metadata"].get("etag"):
            response.headers["ETag"] = etag
        if ctx.get("write"):
            _no_content(response)  # V2 MERGE / PATCH answer 204
            return
        if select := ctx.get("select"):
            row = {k: v for k, v in row.items() if k in select or k in ("name", "__metadata")}
        _set_json(response, {"d": row})

    elif shape == "deleted":
        _no_content(response)

    elif shape == "count":
        response.set_data(str(int(data or 0)).encode())
        response.headers["Content-Type"] = "text/plain"
