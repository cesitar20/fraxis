# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
OData V4 JSON (minimal metadata) for gateway responses, and request bodies back to Frappe values.

    collection  {"@odata.count": 12, "value": [...], "@odata.nextLink": "<url>"}
    document    {"@odata.etag": "W/\\"<modified>\\"", ...properties, <child table>: [...]}
    error       {"error": {"code": "NotFound", "message": "..."}}

Datetimes are ISO 8601 with the system timezone offset, Check fields booleans, JSON fields
objects. Only properties of the entity model leave the gateway, so excluded fields never do.
"""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_system_timezone, strip_html
from werkzeug.wrappers import Response

from fraxis.gateway.odata import ODataError
from fraxis.gateway.odata.model import Entity, Prop

HEADERS = {"OData-Version": "4.0"}
MIMETYPE = "application/json"
INT_TYPES = ("Int", "Long Int", "Duration")
FLOAT_TYPES = ("Float", "Currency", "Percent", "Rating")


def system_tz() -> ZoneInfo:
    return ZoneInfo(get_system_timezone())


def to_system_datetime(value: str) -> str:
    """ISO 8601 (with or without offset) -> naive system-timezone ``YYYY-MM-DD HH:MM:SS``."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ODataError(f"Invalid datetime: {value!r}")
    if dt.tzinfo:
        dt = dt.astimezone(system_tz()).replace(tzinfo=None)
    return dt.isoformat(sep=" ")


def etag(modified) -> str:
    """Weak ETag from ``modified`` exactly as Frappe stringifies it."""
    return f'W/"{modified}"'


# --- values out ----------------------------------------------------------------------------

def value(prop: Prop, raw):
    if raw is None:
        return None
    fieldtype = prop.fieldtype
    if fieldtype == "Datetime":
        if isinstance(raw, str):
            try:
                raw = datetime.fromisoformat(raw)
            except ValueError:
                return raw
        return (raw if raw.tzinfo else raw.replace(tzinfo=system_tz())).isoformat()
    if fieldtype == "Date" and isinstance(raw, date):
        return raw.isoformat()
    if fieldtype == "Time" and isinstance(raw, timedelta):
        seconds = int(raw.total_seconds())
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    if fieldtype == "Check":
        return bool(raw)
    if fieldtype in INT_TYPES:
        return int(raw)
    if fieldtype in FLOAT_TYPES:
        return float(raw)
    if fieldtype == "JSON" and isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def row(props: dict[str, Prop], data: dict) -> dict:
    """Frappe row -> client row, keyed by each property's public name."""
    return {prop.public: value(prop, data.get(key)) for key, prop in props.items() if key in data}


def document(entity: Entity, doc, select: list[str] | None = None, data: dict | None = None) -> dict:
    """``select``: fieldnames (already translated from public names) to keep besides ``name``;
    ``data``: ``doc.as_dict()`` when the caller already has it (adjusted by apps)."""
    data = data if data is not None else doc.as_dict()
    props = {
        k: p for k, p in entity.props.items() if not p.write_only and (not select or k in select or k == "name")
    }
    out = row(props, data)
    for fieldname in entity.collections:
        if not select or fieldname in select:
            child_props = entity.child_props(fieldname)
            out[entity.public(fieldname)] = [row(child_props, child) for child in data.get(fieldname) or []]
    return {"@odata.etag": etag(data["modified"]), **out} if data.get("modified") else out


# --- values in -----------------------------------------------------------------------------

def _by_public(props: dict[str, Prop]) -> dict[str, tuple[str, Prop]]:
    return {prop.public: (key, prop) for key, prop in props.items()}


def _writable(props: dict[str, Prop], body: dict, where: str, keep: tuple[str, ...] = ()) -> dict:
    """Client row (public names) -> Frappe values (fieldnames)."""
    by_public = _by_public(props)
    clean = {}
    for public, raw in body.items():
        if public.startswith("@"):
            continue  # OData annotations (@odata.etag, ...)
        key, prop = by_public.get(public, (None, None))
        if not prop or (prop.read_only and key not in keep):
            raise ODataError(f"Unknown or read-only property {where}{public!r}")
        if prop.fieldtype == "Datetime" and isinstance(raw, str) and "T" in raw:
            raw = to_system_datetime(raw)
        elif prop.fieldtype == "Check" and isinstance(raw, bool):
            raw = int(raw)
        elif prop.fieldtype == "JSON" and isinstance(raw, dict | list):
            raw = json.dumps(raw)
        clean[key] = raw
    return clean


def parse_body(entity: Entity, body) -> dict:
    """JSON body of a create / update -> Frappe values; rejects anything outside the model."""
    if not isinstance(body, dict):
        raise ODataError("Request body must be a JSON object")
    tables = {entity.public(f): f for f in entity.collections}
    clean = _writable(entity.props, {k: v for k, v in body.items() if k not in tables}, "")
    for public in tables.keys() & body.keys():
        fieldname, rows = tables[public], body[public]
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ODataError(f"{public!r} must be an array of objects")
        child_props = entity.child_props(fieldname)
        # A child row may name an existing row to update it in place.
        clean[fieldname] = [_writable(child_props, r, f"{public}.", keep=("name",)) for r in rows]
    return clean


# --- responses -----------------------------------------------------------------------------

def json_response(payload, status: int = 200, headers: dict | None = None) -> Response:
    return Response(
        json.dumps(payload, default=str, ensure_ascii=False),
        status=status,
        mimetype=MIMETYPE,
        headers={**HEADERS, **(headers or {})},
    )


def empty_response(status: int = 204) -> Response:
    return Response(status=status, headers=HEADERS)


def error_response(status: int, code: str, message: str, headers: dict | None = None) -> Response:
    return json_response({"error": {"code": code, "message": message}}, status, headers)


# Most specific first: several of these subclass ValidationError.
_EXCEPTIONS = (
    (frappe.DoesNotExistError, 404, "NotFound"),
    (frappe.PermissionError, 403, "Forbidden"),
    (frappe.DuplicateEntryError, 409, "Conflict"),
    (frappe.TimestampMismatchError, 412, "PreconditionFailed"),
    (frappe.ValidationError, 400, "BadRequest"),
)


def exception_response(e: Exception) -> Response:
    if isinstance(e, ODataError):
        return error_response(e.status_code, e.code, str(e))
    for exc_type, status, code in _EXCEPTIONS:
        if isinstance(e, exc_type):
            message = strip_html(str(e) or "") or code
            return error_response(status, code, message)
    # Deferred: a GET is never committed, and a failed write has been rolled back already.
    frappe.log_error(title="Fraxis gateway request failed", defer_insert=True)
    return error_response(500, "InternalServerError", "An unexpected error occurred")
