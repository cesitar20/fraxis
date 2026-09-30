# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Entity model built from DocType meta — what a route returns, filters, sorts and accepts,
and what its OpenAPI schema shows, so the three can never disagree.

* Standard properties: ``name`` (the key), ``creation``, ``modified`` and ``docstatus`` on
  submittable DocTypes; ``owner`` / ``modified_by`` are internal and never published.
* Child tables become collections, returned on single-document reads only (``get_list``
  cannot select them).
* ``Password`` / layout-only fields and the ``Fraxis Settings > Excluded Fields`` of the
  DocType (or of the child DocType) are never part of the model.
"""

import re
from dataclasses import dataclass, field

import frappe
from frappe.model import no_value_fields

from fraxis.gateway import config

STANDARD = {"name": "Data", "creation": "Datetime", "modified": "Datetime", "docstatus": "Int"}
CHILD_STANDARD = {"name": "Data", "idx": "Int"}
TABLE_TYPES = ("Table", "Table MultiSelect")
SKIP_TYPES = frozenset(no_value_fields) | {"Password"}


@dataclass
class Prop:
    name: str
    fieldtype: str
    label: str = ""
    required: bool = False
    options: str | None = None  # Link target / Select choices
    read_only: bool = False  # standard columns; the DocType's own fields stay writable as in Frappe's API


@dataclass
class Entity:
    doctype: str
    props: dict[str, Prop] = field(default_factory=dict)
    collections: dict[str, str] = field(default_factory=dict)  # fieldname -> child DocType
    description: str = ""

    def child_props(self, fieldname: str) -> dict[str, Prop]:
        return _props(frappe.get_meta(self.collections[fieldname]), CHILD_STANDARD)[0]


def _props(meta, standard: dict[str, str]) -> tuple[dict[str, Prop], dict[str, str]]:
    excluded = config.excluded_fields().get(meta.name, set())
    props, collections = {}, {}
    for key, fieldtype in standard.items():
        if key == "docstatus" and not meta.is_submittable:
            continue
        if key not in excluded:
            props[key] = Prop(key, fieldtype, key, required=key == "name", read_only=True)
    for df in meta.fields:
        if df.fieldname in excluded:
            continue
        if df.fieldtype in TABLE_TYPES:
            collections[df.fieldname] = df.options
        elif df.fieldtype not in SKIP_TYPES:
            props[df.fieldname] = Prop(
                df.fieldname,
                df.fieldtype,
                df.label or df.fieldname,
                required=bool(df.reqd),
                options=df.options if df.fieldtype in ("Link", "Select") else None,
            )
    return props, collections


def entity(doctype: str) -> Entity:
    meta = frappe.get_meta(doctype)
    props, collections = _props(meta, STANDARD)
    return Entity(doctype, props, collections, meta.description or "")


def schema_name(doctype: str) -> str:
    """``AI Assistant`` -> ``AIAssistant`` (OpenAPI component name)."""
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", doctype) if part)
