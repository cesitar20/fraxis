# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Query string of a collection -> ``frappe.get_list`` arguments. Clients use public names
(Fraxis Settings > Field Mappings) and only properties of the entity model can be selected,
filtered or sorted, so excluded fields cannot be probed either.

Field filters, one parameter per property (see :func:`field_parameters`)::

    ?status=Active                 equals
    ?status=Active,Paused          any of
    ?creation=2026-09-23           a datetime's whole day (23-09-2026 works too)
    ?creation_from=2026-09-01&creation_to=2026-09-30T23:59:59Z    date / datetime range

OData options, for what field filters cannot express::

    $select=a,b   $filter=<expr>   $orderby=a desc,b   $top=50   $skip=100   $count=true

Lists come newest first (``creation desc``) unless ``$orderby`` says otherwise.
"""

import re
from dataclasses import dataclass, field

from fraxis.gateway.odata import ODataError, filter_parser
from fraxis.gateway.odata.model import Entity, Prop
from fraxis.gateway.odata.serialize import to_system_datetime

LIST_OPTIONS = frozenset({"$select", "$filter", "$orderby", "$top", "$skip", "$count", "$format"})
ITEM_OPTIONS = frozenset({"$select", "$format"})
# Long or binary content: no field filter parameter (still usable in $filter).
UNFILTERABLE = frozenset(
    {"JSON", "Long Text", "Text", "Small Text", "Text Editor", "Code", "HTML Editor", "Markdown Editor",
     "Attach", "Attach Image", "Signature", "Geolocation"}
)
INT_TYPES = ("Int", "Long Int", "Duration")
FLOAT_TYPES = ("Float", "Currency", "Percent", "Rating")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
DMY_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})")  # 23-09-2026, as dates are written in the desk


def _iso_date(raw: str) -> str | None:
    """``2026-09-23`` or ``23-09-2026`` -> ``2026-09-23``; anything else -> None."""
    if DATE_RE.fullmatch(raw):
        return raw
    if m := DMY_RE.fullmatch(raw):
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


@dataclass
class FieldParam:
    fieldname: str
    prop: Prop
    operator: str  # "=" (comma-separated: any of), "day" (a datetime's whole day), ">=" (_from) or "<=" (_to)


@dataclass
class ListQuery:
    fields: list[str]
    filters: list = field(default_factory=list)
    or_filters: list = field(default_factory=list)
    order_by: str = ""
    top: int = 0
    skip: int = 0
    count: bool = False


def field_parameters(entity: Entity) -> dict[str, FieldParam]:
    """Query parameter -> the filter it applies: equality for every property, a ``_from`` /
    ``_to`` range for dates and datetimes, and for a datetime a whole day (``?creation=2026-09-23``)
    instead of an equality that would never be exact."""
    params: dict[str, FieldParam] = {}
    for fieldname, prop in entity.props.items():
        if prop.fieldtype in UNFILTERABLE or prop.write_only:
            continue
        params[prop.public] = FieldParam(fieldname, prop, "day" if prop.fieldtype == "Datetime" else "=")
        if prop.fieldtype in ("Date", "Datetime") and not prop.lookup:
            params[f"{prop.public}_from"] = FieldParam(fieldname, prop, ">=")
            params[f"{prop.public}_to"] = FieldParam(fieldname, prop, "<=")
    return params


def check_options(args, allowed) -> None:
    for key in args:
        if key not in allowed:
            if key.startswith("$"):
                raise ODataError(f"Query option {key} is not supported here", 501, "NotImplemented")
            raise ODataError(f"Unknown query parameter {key!r}")
    if args.get("$format") not in (None, "json", "application/json"):
        raise ODataError("Only $format=json is supported", 406, "NotAcceptable")


def _fieldname(entity: Entity, public: str, where: str, collections: bool = False) -> str:
    fieldname = entity.fieldname(public)
    prop = entity.props.get(fieldname)
    # A write-only property (Returned In = Never) is unknown to reads: it cannot be probed either.
    if not fieldname or (fieldname in entity.collections and not collections) or (prop and prop.write_only):
        raise ODataError(f"Unknown property in {where}: {public!r}")
    return fieldname


def _literal(lit, prop):
    if lit.kind == "bool":
        return 1 if lit.value else 0
    if lit.kind == "datetime":
        return to_system_datetime(lit.value)
    if lit.kind == "date" and prop.fieldtype == "Datetime":
        return f"{lit.value} 00:00:00"
    return lit.value


def _conditions(conds, entity: Entity) -> list:
    out = []
    for public, op, lit in conds:
        fieldname = _fieldname(entity, public, "$filter")
        prop = entity.props[fieldname]
        value = [_literal(v, prop) for v in lit] if isinstance(lit, list) else _literal(lit, prop)
        if prop.lookup and op != "is":
            out.append(_lookup_condition(entity, fieldname, prop, op, value))
        else:
            out.append([entity.doctype, fieldname, op, value])
    return out


def _lookup_condition(entity: Entity, fieldname: str, prop: Prop, op: str, value) -> list:
    """A lookup field compares the other record's value: only equality makes sense across the two."""
    from fraxis.gateway import lookup

    if op not in ("=", "!=", "in", "not in"):
        raise ODataError(f"{prop.public} only supports eq, ne and in")
    values = value if isinstance(value, list) else [value]
    return lookup.in_filter(entity, fieldname, lookup.to_internal(prop, values), negate=op in ("!=", "not in"))


def _param_value(name: str, param: FieldParam, raw: str):
    fieldtype = param.prop.fieldtype
    raw = raw.strip()
    try:
        if fieldtype == "Check":
            if raw.lower() in ("true", "1"):
                return 1
            if raw.lower() in ("false", "0"):
                return 0
            raise ValueError
        if fieldtype in INT_TYPES:
            return int(raw)
        if fieldtype in FLOAT_TYPES:
            return float(raw)
        if fieldtype == "Date":
            if not (day := _iso_date(raw)):
                raise ValueError
            return day
        if fieldtype == "Datetime":
            day = _iso_date(raw)
            if param.operator == "day":  # the whole day of a date, or of a datetime's own date
                day = day or to_system_datetime(raw)[:10]
                return (f"{day} 00:00:00", f"{day} 23:59:59.999999")
            if day:  # a date alone: from its start / to its end
                return f"{day} 00:00:00" if param.operator == ">=" else f"{day} 23:59:59.999999"
            return to_system_datetime(raw)
    except (ValueError, ODataError):
        raise ODataError(f"Invalid value for {name}: {raw!r}")
    return raw


def _field_filters(entity: Entity, args, params: dict[str, FieldParam]) -> list:
    out = []
    for name in args:
        if param := params.get(name):
            raw = args.get(name)
            values = [v for v in raw.split(",") if v.strip()] if param.operator == "=" else [raw]
            if not values:
                raise ODataError(f"Invalid value for {name}: {raw!r}")
            converted = [_param_value(name, param, v) for v in values]
            if param.operator == "day":
                start, end = converted[0]
                out += [[entity.doctype, param.fieldname, ">=", start], [entity.doctype, param.fieldname, "<=", end]]
            elif param.prop.lookup:
                from fraxis.gateway import lookup

                out.append(lookup.in_filter(entity, param.fieldname, lookup.to_internal(param.prop, converted)))
            elif len(converted) > 1:
                out.append([entity.doctype, param.fieldname, "in", converted])
            else:
                out.append([entity.doctype, param.fieldname, param.operator, converted[0]])
    return out


def select(entity: Entity, raw: str | None, allow_collections: bool = False) -> list[str] | None:
    """``$select`` (public names) -> fieldnames."""
    if not raw:
        return None
    return [_fieldname(entity, f.strip(), "$select", allow_collections) for f in raw.split(",") if f.strip()]


def _order_by(entity: Entity, raw: str | None) -> str:
    if not raw:
        # creation, not modified: routes are usually scoped by an indexed field such as the bot
        # (agent, creation), so the newest rows come straight from that index, without a filesort.
        return f"`tab{entity.doctype}`.`creation` desc"
    parts = []
    for item in raw.split(","):
        bits = item.split()
        if not bits or len(bits) > 2:
            raise ODataError(f"Invalid $orderby item: {item.strip()!r}")
        fieldname = _fieldname(entity, bits[0], "$orderby")
        direction = bits[1].lower() if len(bits) == 2 else "asc"
        if direction not in ("asc", "desc"):
            raise ODataError(f"Invalid $orderby direction: {bits[1]!r}")
        parts.append(f"`tab{entity.doctype}`.`{fieldname}` {direction}")
    return ", ".join(parts)


def _int_option(args, key: str, default: int) -> int:
    raw = args.get(key)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = -1
    if value < 0:
        raise ODataError(f"{key} must be a non-negative integer")
    return value


def parse_list(entity: Entity, args, page_size: int, max_page_size: int) -> ListQuery:
    params = field_parameters(entity)
    check_options(args, LIST_OPTIONS | params.keys())
    fields = select(entity, args.get("$select")) or [k for k, p in entity.props.items() if p.in_lists]
    if hidden := [entity.props[f].public for f in fields if f in entity.props and not entity.props[f].in_lists]:
        raise ODataError(f"Not returned in lists, read one record instead: {', '.join(hidden)}")
    if "name" not in fields:
        fields.insert(0, "name")

    filters, or_filters = _field_filters(entity, args, params), []
    if args.get("$filter"):
        and_c, or_c = filter_parser.to_frappe(args["$filter"])
        filters += _conditions(and_c, entity)
        or_filters = _conditions(or_c, entity)

    top = min(_int_option(args, "$top", page_size), max_page_size)
    if top == 0:
        raise ODataError("$top must be greater than 0")
    count = str(args.get("$count", "")).lower()
    if count not in ("", "true", "false"):
        raise ODataError("$count must be true or false")

    return ListQuery(
        fields=fields,
        filters=filters,
        or_filters=or_filters,
        order_by=_order_by(entity, args.get("$orderby")),
        top=top,
        skip=_int_option(args, "$skip", 0),
        count=count == "true",
    )
