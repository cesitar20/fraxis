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
    options: str | None = None  # Select choices / Data kind (Email, Phone, URL)
    read_only: bool = False  # standard columns and fields Read Only in the DocType: returned, never written
    public: str = ""  # name clients see and send (Fraxis Settings > Field Mappings); the fieldname by default
    description: str = ""  # the field's description, when the docs show it
    example: str = ""  # Field Mappings > Example, else the field's Placeholder; the docs fall back to the fieldtype
    lookup: tuple[str, str, str] | None = None  # Field Mappings > Lookup (see fraxis.gateway.lookup)
    lookup_restrict: bool = True  # Field Mappings > Restrict Rows
    in_lists: bool = True  # Field Mappings > Returned In: returned by lists
    write_only: bool = False  # Returned In = Never: accepted in bodies, never returned, filtered nor sorted


@dataclass
class Entity:
    doctype: str
    props: dict[str, Prop] = field(default_factory=dict)
    collections: dict[str, str] = field(default_factory=dict)  # fieldname -> child DocType
    description: str = ""
    collection_public: dict[str, str] = field(default_factory=dict)  # child table fieldname -> public name

    def child_props(self, fieldname: str) -> dict[str, Prop]:
        return _props(frappe.get_meta(self.collections[fieldname]), CHILD_STANDARD)[0]

    def public(self, fieldname: str) -> str:
        """Public name of a property or child table."""
        prop = self.props.get(fieldname)
        return prop.public if prop else self.collection_public.get(fieldname, fieldname)

    def fieldname(self, public: str) -> str | None:
        """Property or child table behind a public name, or None when clients cannot use it."""
        for name, prop in self.props.items():
            if prop.public == public:
                return name
        return next((name for name, alias in self.collection_public.items() if alias == public), None)


STANDARD_LABELS = {"name": "ID", "creation": "Created On", "modified": "Last Updated On",
                   "docstatus": "Document Status", "idx": "Row Number"}


def _props(meta, standard: dict[str, str], apply_exclusions: bool = True) -> tuple[dict[str, Prop], dict[str, str]]:
    excluded = config.excluded_fields().get(meta.name, set()) if apply_exclusions else set()
    mappings = config.field_mappings().get(meta.name, {}) if apply_exclusions else {}

    def public(fieldname: str) -> str:
        return mappings[fieldname].public_name if fieldname in mappings else fieldname

    def example(fieldname: str) -> str:
        return mappings[fieldname].example if fieldname in mappings else ""

    def lookup(fieldname: str):
        return mappings[fieldname].lookup if fieldname in mappings else None

    def lookup_restrict(fieldname: str) -> bool:
        return mappings[fieldname].lookup_restrict if fieldname in mappings else True

    def returned_in(fieldname: str) -> str:
        return mappings[fieldname].returned_in if fieldname in mappings else "Lists and Records"

    def description(fieldname: str, text: str | None) -> str:
        return (text or "") if fieldname not in mappings or mappings[fieldname].show_description else ""

    props, collections = {}, {}
    for key, fieldtype in standard.items():
        if key == "docstatus" and not meta.is_submittable:
            continue
        if key not in excluded:
            props[key] = Prop(
                key, fieldtype, STANDARD_LABELS[key], required=key == "name", read_only=True, public=public(key),
                example=example(key) or (_name_example(meta) if key == "name" else ""),
            )
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
                # Read Only in the DocType (or Customize Form): computed or set by the system, so
                # clients read it but never send it, and the docs leave it out of request bodies.
                read_only=bool(df.read_only),
                options=df.options if df.fieldtype in ("Select", "Data") else None,  # choices / Email, Phone, URL
                public=public(df.fieldname),
                description=description(df.fieldname, df.description),
                # The field's Placeholder (DocType or Customize Form) doubles as its example; a
                # Lookup shows a value clients send instead (e.g. an assistant ID, not its flow).
                example=example(df.fieldname)
                or (_lookup_example(lookup(df.fieldname)) if lookup(df.fieldname) else (df.get("placeholder") or "").strip()),
                lookup=lookup(df.fieldname),
                lookup_restrict=lookup_restrict(df.fieldname),
                in_lists=returned_in(df.fieldname) == "Lists and Records",
                write_only=returned_in(df.fieldname) == "Never",
            )
    return props, collections


def _lookup_example(lookup: tuple[str, str, str]) -> str:
    """Example of a Lookup field: its value field's, in the Lookup DocType (an ID for ``name``)."""
    doctype, _match, value_field = lookup
    if not frappe.db.exists("DocType", doctype):
        return ""
    meta = frappe.get_meta(doctype)
    if value_field == "name":
        return _name_example(meta)
    df = meta.get_field(value_field)
    return (df.get("placeholder") or "").strip() if df else ""


def _name_example(meta) -> str:
    """An ID in the DocType's own numbering: ``AI-ASSISTANT-.#########`` -> ``AI-ASSISTANT-000000001``,
    ``field:user`` -> that field's placeholder, a random name -> a short hash."""
    autoname = (meta.autoname or "").strip()
    if autoname == "hash":  # even when an unused naming_series field exists
        return "a1b2c3d4e5"
    if autoname.startswith("field:"):
        df = meta.get_field(autoname[len("field:"):].strip())
        return (df.get("placeholder") or "") if df else ""
    series = autoname[len("naming_series:"):] if autoname.startswith("naming_series:") else ""
    if not series and meta.get_field("naming_series"):
        series = next((o for o in (meta.get_field("naming_series").options or "").split("\n") if o), "")
    elif not series and ".#" in autoname:
        series = autoname
    if ".#" in series:
        prefix, _dot, hashes = series.rpartition(".")
        return f"{prefix}{'1'.rjust(len(hashes), '0')}" if set(hashes) == {"#"} else ""
    return "a1b2c3d4e5" if not autoname else ""


def entity(doctype: str) -> Entity:
    meta = frappe.get_meta(doctype)
    props, collections = _props(meta, STANDARD)
    mappings = config.field_mappings().get(doctype, {})
    collection_public = {f: mappings[f].public_name if f in mappings else f for f in collections}
    return Entity(doctype, props, collections, meta.description or "", collection_public)


def publishable_fields(doctype: str, include_name: bool = False) -> list[dict]:
    """Every field the DocType can return, before exclusions and mappings: what Excluded Fields may
    exclude (``name`` identifies the document and is left out) and Field Mappings may rename."""
    meta = frappe.get_meta(doctype)
    props, collections = _props(meta, CHILD_STANDARD if meta.istable else STANDARD, apply_exclusions=False)
    fields = [
        {"fieldname": n, "label": p.label, "fieldtype": p.fieldtype}
        for n, p in props.items()
        if include_name or n != "name"
    ]
    labels = {df.fieldname: df.label for df in meta.fields}
    fields += [{"fieldname": n, "label": labels.get(n) or n, "fieldtype": "Table"} for n in collections]
    return fields


def schema_name(doctype: str) -> str:
    """``AI Assistant`` -> ``AIAssistant`` (OpenAPI component name)."""
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", doctype) if part)
