# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
``after_request`` hook: reshapes Frappe v2 JSON into OData JSON on gateway routes — V4 here,
V2 (``dialect == "v2"``) in :mod:`fraxis.gateway.odata.response_v2`.

Frappe runs ``after_request`` hooks in the ``finally`` block of ``frappe.app.application``
with the built response and swallows any exception they raise, so this module never
lets an error escape: on any failure the Frappe response is returned untouched.
"""

import json
from datetime import date, datetime
from urllib.parse import urlencode

import frappe
from frappe.utils import strip_html

from fraxis.gateway import paths
from fraxis.gateway.odata import model
from fraxis.gateway.odata.request import _system_tz, etag_for

ODATA_HEADERS = {"OData-Version": "4.0"}
JSON_MIME = "application/json;odata.metadata=minimal;odata.streaming=true"


def after_request(response=None, request=None) -> None:
    route = paths.current_route()
    if not route or response is None or not hasattr(response, "get_data"):
        return
    try:
        if response.status_code == 401:
            error = getattr(frappe.local, "fraxis_auth_error", None) or "invalid_token"
            response.headers["WWW-Authenticate"] = f'Bearer realm="fraxis", error="invalid_token", error_description="{error}"'
        if route.kind == "odata" and route.odata:
            if route.odata.get("dialect") == "v2":
                from fraxis.gateway.odata import response_v2

                response_v2.reshape(response, route.odata)
            else:
                _reshape(response, route.odata)
    except Exception:
        frappe.logger("fraxis.gateway").error(frappe.get_traceback())


# --- value shaping -------------------------------------------------------------------------

def _iso(value):
    if not value or not isinstance(value, str | datetime):
        return value
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.replace(tzinfo=_system_tz()).isoformat() if dt.tzinfo is None else dt.isoformat()


def _shape_value(prop: model.Prop, value):
    if value is None:
        return None
    if prop.edm == "Edm.DateTimeOffset":
        return _iso(value)
    if prop.edm == "Edm.Boolean":
        return bool(value)
    if prop.edm == "Edm.Date" and isinstance(value, date):
        return value.isoformat()
    return value


def shape_row(row: dict, props: dict[str, model.Prop], collections: dict[str, str] | None = None) -> dict:
    out = {}
    for key, value in row.items():
        if key in props:
            out[key] = _shape_value(props[key], value)
        elif collections and key in collections and isinstance(value, list):
            child = model.complex_type(collections[key])
            out[key] = [shape_row(r, child) for r in value if isinstance(r, dict)]
    return out


# --- responses -----------------------------------------------------------------------------

def _set_json(response, payload: dict, status: int | None = None) -> None:
    response.set_data(json.dumps(payload, default=str, separators=(",", ":")))
    response.headers["Content-Type"] = JSON_MIME
    if status:
        response.status_code = status


# Frappe answers validation failures with 417 (Expectation Failed), which is about the
# ``Expect`` header in HTTP. OData / REST clients expect 400, and 412 for stale writes.
STATUS_BY_TYPE = {"TimestampMismatchError": 412}
STATUS_REMAP = {417: 400}


def _odata_status(body: dict, status: int) -> int:
    errors = body.get("errors") or []
    error_type = (errors[0] if errors else {}).get("type")
    return STATUS_BY_TYPE.get(error_type) or STATUS_REMAP.get(status, status)


def _error_payload(body: dict, status: int) -> dict:
    errors = body.get("errors") or []
    first = errors[0] if errors else {}
    code = first.get("type") or ("NotFound" if status == 404 else "Error")
    message = strip_html(first.get("message") or first.get("title") or "")
    if status == 401:
        message = message or getattr(frappe.local, "fraxis_auth_error", "")
    message = message or code
    details = [
        {"code": e.get("type") or code, "message": strip_html(e.get("message") or "")} for e in errors[1:]
    ]
    return {"error": {"code": code, "message": message, **({"details": details} if details else {})}}


def _entity_url(ctx: dict, name: str) -> str:
    return f"{ctx['root']}/{ctx['set']}('{str(name).replace(chr(39), chr(39) * 2)}')"


def _count(ctx: dict) -> int | None:
    doctype = ctx["doctype"]
    try:
        rows = frappe.get_list(
            doctype,
            fields=[f"count(`tab{doctype}`.`name`) as total_count"],
            filters=ctx.get("filters") or None,
            or_filters=ctx.get("or_filters") or None,
            order_by=None,
        )
        return rows[0].total_count if rows else 0
    except Exception:
        frappe.logger("fraxis.gateway").error(frappe.get_traceback())
        return None


def _next_link(ctx: dict, returned: int) -> str | None:
    if returned < ctx["top"]:
        return None
    args = {k: v for k, v in frappe.local.request.args.items() if k != "$skip"}
    args["$skip"] = ctx["skip"] + ctx["top"]
    args.setdefault("$top", ctx["top"])
    return f"{ctx['root']}/{ctx['set']}?{urlencode(args)}"


def _reshape(response, ctx: dict) -> None:
    for header, value in ODATA_HEADERS.items():
        response.headers[header] = value

    status = response.status_code
    try:
        body = json.loads(response.get_data() or b"{}")
    except ValueError:
        return

    if status >= 400:
        # Already OData-shaped when rendered by fraxis.gateway.api.odata_error; an auth
        # failure on an error route (401 raised before the endpoint) still needs mapping.
        if "error" not in body:
            _set_json(response, _error_payload(body, status), _odata_status(body, status))
        return
    if "error" in ctx:
        return

    data = body.get("data")
    shape = ctx.get("shape")
    if shape in ("action", "operation"):
        _set_json(response, {"@odata.context": f"{ctx['root']}/$metadata#Edm.Untyped", "value": data})
        return

    entity = model.entity_type(ctx["doctype"])
    context_url = f"{ctx['root']}/$metadata#{ctx['set']}"

    if shape == "collection":
        payload = {"@odata.context": context_url}
        if ctx.get("count"):
            count = _count(ctx)
            if count is not None:
                payload["@odata.count"] = count
        rows = data or []
        payload["value"] = [
            ({"@odata.etag": etag_for(r["modified"])} if r.get("modified") else {}) | shape_row(r, entity.props)
            for r in rows
        ]
        if next_link := _next_link(ctx, len(rows)):
            payload["@odata.nextLink"] = next_link
        _set_json(response, payload)

    elif shape in ("entity", "created"):
        raw = data or {}
        row = shape_row(raw, entity.props, entity.collections)
        if select := ctx.get("select"):
            row = {k: v for k, v in row.items() if k in select or k == "name"}
        etag = etag_for(raw["modified"]) if raw.get("modified") else None
        payload = {"@odata.context": f"{context_url}/$entity", **({"@odata.etag": etag} if etag else {}), **row}
        if etag:
            response.headers["ETag"] = etag
        if shape == "created" and row.get("name"):
            response.headers["Location"] = _entity_url(ctx, row["name"])
        writing = frappe.local.request.method in ("POST", "PATCH")
        if writing and "return=minimal" in (frappe.local.request.headers.get("Prefer") or ""):
            response.set_data(b"")
            response.status_code = 204
            response.headers.pop("Content-Type", None)
            response.headers["Preference-Applied"] = "return=minimal"
            return
        _set_json(response, payload, 201 if shape == "created" else 200)

    elif shape == "deleted":
        response.set_data(b"")
        response.status_code = 204
        response.headers.pop("Content-Type", None)

    elif shape == "count":
        response.set_data(str(int(data or 0)).encode())
        response.headers["Content-Type"] = "text/plain"


