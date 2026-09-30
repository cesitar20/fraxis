# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Lookup fields (Fraxis Settings > Field Mappings > Lookup): a field shown as the matching record of
another DocType. ``Jambonz Call Stats.agent`` holds the agent id; with the lookup
``(AI Assistant, agent_id, name)`` clients read ``assistant_id: AI-ASSISTANT-…``, filter and write
by it, and never see the agent id.

Every lookup goes through ``frappe.get_list`` as the caller plus its Extra Params on the lookup
DocType, so a record of another organization is never matched, and when the caller is scoped on
that DocType the rows themselves are limited to values of the records it can see (:func:`restrictions`),
unless the mapping unticks Restrict Rows (a field that only decorates the row, such as a phone
number shown by its title).
"""

import frappe

from fraxis.gateway import scope
from fraxis.gateway.odata import ODataError
from fraxis.gateway.odata.model import Entity, Prop

# Matches nothing: an "in" filter needs at least one value.
NOTHING = "\x00"


def _lookup_rows(prop: Prop, filters: list) -> list:
    doctype, match_field, value_field = prop.lookup
    return frappe.get_list(
        doctype,
        filters=filters + scope.list_filters(doctype),
        fields=[match_field, value_field],
        order_by=f"`tab{doctype}`.`{value_field}` asc",
        limit_page_length=0,
    )


def to_public(prop: Prop, values) -> dict:
    """Internal values -> the value clients see (the first visible match of each)."""
    values = sorted({v for v in values if v not in (None, "")})
    if not values:
        return {}
    doctype, match_field, value_field = prop.lookup
    out = {}
    for row in _lookup_rows(prop, [[doctype, match_field, "in", values]]):
        out.setdefault(row[match_field], row[value_field])
    return out


def to_internal(prop: Prop, values) -> list:
    """Values clients send -> internal values of the visible matching records."""
    doctype, match_field, value_field = prop.lookup
    rows = _lookup_rows(prop, [[doctype, value_field, "in", list(values)]])
    return list(dict.fromkeys(row[match_field] for row in rows if row[match_field] not in (None, "")))


def to_internal_one(prop: Prop, value):
    """The internal value behind one client value; 400 when no visible record matches."""
    if value in (None, ""):
        return value
    matches = to_internal(prop, [value])
    if not matches:
        raise ODataError(f"Unknown {prop.public}: {value!r}")
    return matches[0]


def in_filter(entity: Entity, fieldname: str, values: list, negate: bool = False) -> list:
    return [entity.doctype, fieldname, "not in" if negate else "in", values or [NOTHING]]


def restrictions(entity: Entity) -> list:
    """Limit rows to values of lookup records the caller sees, when it is scoped on that DocType and
    the mapping restricts rows (Restrict Rows)."""
    out = []
    for fieldname, prop in entity.props.items():
        if prop.lookup and prop.lookup_restrict and scope.for_doctype(prop.lookup[0]):
            doctype, match_field, _value_field = prop.lookup
            values = [r[match_field] for r in _lookup_rows(prop, []) if r[match_field] not in (None, "")]
            out.append(in_filter(entity, fieldname, list(dict.fromkeys(values))))
    return out


def allowed(entity: Entity, doc) -> bool:
    """Single-document counterpart of :func:`restrictions`."""
    for fieldname, prop in entity.props.items():
        if prop.lookup and prop.lookup_restrict and scope.for_doctype(prop.lookup[0]):
            if not to_public(prop, [doc.get(fieldname)]):
                return False
    return True


def publish(entity: Entity, rows: list[dict]) -> list[dict]:
    """Serialized rows (public keys): replace each lookup field's internal value by the public one."""
    for prop in entity.props.values():
        if prop.lookup and any(prop.public in r for r in rows):
            mapping = to_public(prop, [r.get(prop.public) for r in rows])
            for r in rows:
                if prop.public in r:
                    r[prop.public] = mapping.get(r[prop.public])
    return rows
