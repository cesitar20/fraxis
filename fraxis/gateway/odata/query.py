# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
OData query options -> ``frappe.get_list`` arguments. Only properties of the entity model can
be selected, filtered or sorted, so excluded fields cannot be probed through ``$filter`` either.

    $select=a,b   $filter=<expr>   $orderby=a desc,b   $top=50   $skip=100   $count=true
"""

from dataclasses import dataclass, field

from fraxis.gateway.odata import ODataError, filter_parser
from fraxis.gateway.odata.model import Entity
from fraxis.gateway.odata.serialize import to_system_datetime

LIST_OPTIONS = frozenset({"$select", "$filter", "$orderby", "$top", "$skip", "$count", "$format"})
ITEM_OPTIONS = frozenset({"$select", "$format"})


@dataclass
class ListQuery:
    fields: list[str]
    filters: list = field(default_factory=list)
    or_filters: list = field(default_factory=list)
    order_by: str = ""
    top: int = 0
    skip: int = 0
    count: bool = False


def check_options(args, allowed: frozenset) -> None:
    for key in args:
        if key not in allowed:
            if key.startswith("$"):
                raise ODataError(f"Query option {key} is not supported here", 501, "NotImplemented")
            raise ODataError(f"Unknown query parameter {key!r}; use $filter to filter")
    if args.get("$format") not in (None, "json", "application/json"):
        raise ODataError("Only $format=json is supported", 406, "NotAcceptable")


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
    for fieldname, op, lit in conds:
        prop = entity.props.get(fieldname)
        if not prop:
            raise ODataError(f"Unknown property in $filter: {fieldname!r}")
        value = [_literal(v, prop) for v in lit] if isinstance(lit, list) else _literal(lit, prop)
        out.append([entity.doctype, fieldname, op, value])
    return out


def select(entity: Entity, raw: str | None, allow_collections: bool = False) -> list[str] | None:
    if not raw:
        return None
    fields = [f.strip() for f in raw.split(",") if f.strip()]
    for f in fields:
        if f not in entity.props and not (allow_collections and f in entity.collections):
            raise ODataError(f"Unknown property in $select: {f!r}")
    return fields


def _order_by(entity: Entity, raw: str | None) -> str:
    if not raw:
        return f"`tab{entity.doctype}`.`modified` desc"
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
    check_options(args, LIST_OPTIONS)
    fields = select(entity, args.get("$select")) or list(entity.props)
    if "name" not in fields:
        fields.insert(0, "name")

    filters, or_filters = [], []
    if args.get("$filter"):
        and_c, or_c = filter_parser.to_frappe(args["$filter"])
        filters, or_filters = _conditions(and_c, entity), _conditions(or_c, entity)

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
