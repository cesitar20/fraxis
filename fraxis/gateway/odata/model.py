# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Entity model built from DocType meta — shared by the OData runtime, ``$metadata`` (CSDL)
and the OpenAPI document, so the three can never disagree.

* EntitySet name: DocType name with non-identifier characters replaced by ``_``
  (``Sales Invoice`` -> ``Sales_Invoice``); key property is always ``name``.
* Child tables (``Table`` / ``Table MultiSelect``) become collections of ComplexTypes,
  returned on single-entity reads only (``get_list`` cannot select them).
* ``Password`` fields and layout-only fields are never part of the model.
"""

import re
from dataclasses import dataclass, field

import frappe
from frappe.model import default_fields, no_value_fields

from fraxis.gateway import config

NAMESPACE = "Fraxis"

EDM_TYPES = {
    "Int": "Edm.Int64",
    "Long Int": "Edm.Int64",
    "Check": "Edm.Boolean",
    "Float": "Edm.Double",
    "Percent": "Edm.Double",
    "Currency": "Edm.Decimal",
    "Rating": "Edm.Double",
    "Duration": "Edm.Int64",
    "Date": "Edm.Date",
    "Datetime": "Edm.DateTimeOffset",
    "Time": "Edm.String",
}
# OData V2 (dialect at <base_path>/odata/v2): no Edm.Date / DateTimeOffset in practice,
# JSON dates as /Date(ms)/, Int64 as strings — so V2 uses these narrower types.
EDM_V2_TYPES = {
    "Check": "Edm.Boolean",
    "Int": "Edm.Int32",
    "Long Int": "Edm.Int64",
    "Duration": "Edm.Int64",
    "Float": "Edm.Double",
    "Percent": "Edm.Double",
    "Rating": "Edm.Double",
    "Currency": "Edm.Double",
    "Date": "Edm.DateTime",
    "Datetime": "Edm.DateTime",
}
STANDARD_PROPS = {
    "name": ("Edm.String", "Data"),
    "owner": ("Edm.String", "Link"),
    "creation": ("Edm.DateTimeOffset", "Datetime"),
    "modified": ("Edm.DateTimeOffset", "Datetime"),
    "modified_by": ("Edm.String", "Link"),
    "docstatus": ("Edm.Int64", "Int"),
    "idx": ("Edm.Int64", "Int"),
}
CHILD_STANDARD = ("name", "idx")
TABLE_TYPES = ("Table", "Table MultiSelect")
SKIP_TYPES = set(no_value_fields) | {"Password"}
# The gateway's own credential store is never an OData entity, whatever the config says.
NEVER_EXPOSED = {"Fraxis Refresh Token"}


@dataclass
class Prop:
    name: str
    edm: str
    fieldtype: str
    label: str = ""
    nullable: bool = True
    options: str | None = None  # Link target / Select choices


@dataclass
class EntityType:
    doctype: str
    set_name: str
    props: dict[str, Prop] = field(default_factory=dict)
    collections: dict[str, str] = field(default_factory=dict)  # fieldname -> child doctype
    submittable: bool = False
    description: str = ""


def edm_v2(prop: "Prop") -> str:
    return EDM_V2_TYPES.get(prop.fieldtype, "Edm.String")


def set_name_for(doctype: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", doctype)
    return f"_{name}" if name[0].isdigit() else name


def _props_for(meta, standard: tuple[str, ...]) -> tuple[dict[str, Prop], dict[str, str]]:
    props, collections = {}, {}
    for key in standard:
        edm, fieldtype = STANDARD_PROPS[key]
        props[key] = Prop(key, edm, fieldtype, key, nullable=key != "name")
    for df in meta.fields:
        if df.fieldtype in TABLE_TYPES:
            collections[df.fieldname] = df.options
            continue
        if df.fieldtype in SKIP_TYPES or df.fieldname in default_fields:
            continue
        props[df.fieldname] = Prop(
            df.fieldname,
            EDM_TYPES.get(df.fieldtype, "Edm.String"),
            df.fieldtype,
            df.label or df.fieldname,
            nullable=not df.reqd,
            options=df.options if df.fieldtype in ("Link", "Select") else None,
        )
    return props, collections


def entity_type(doctype: str) -> EntityType:
    meta = frappe.get_meta(doctype)
    standard = tuple(k for k in STANDARD_PROPS if k != "docstatus" or meta.is_submittable)
    props, collections = _props_for(meta, standard)
    return EntityType(
        doctype=doctype,
        set_name=set_name_for(doctype),
        props=props,
        collections=collections,
        submittable=bool(meta.is_submittable),
        description=meta.description or "",
    )


def complex_type(child_doctype: str) -> dict[str, Prop]:
    props, _ = _props_for(frappe.get_meta(child_doctype), CHILD_STANDARD)
    return props


# --- exposure --------------------------------------------------------------------------

def _exposed_uncached() -> dict[str, str]:
    apps = config.get("odata_apps")
    if apps is None:
        apps = [app for app in frappe.get_installed_apps() if app != "frappe"]
    modules = frappe.get_all("Module Def", filters={"app_name": ["in", apps or [""]]}, pluck="name")
    doctypes = set(
        frappe.get_all(
            "DocType",
            filters={"module": ["in", modules or [""]], "istable": 0, "issingle": 0, "is_virtual": 0},
            pluck="name",
        )
    )
    doctypes |= set(config.get("odata_include_doctypes") or [])
    doctypes -= set(config.get("odata_exclude_doctypes") or [])
    doctypes -= NEVER_EXPOSED
    return {set_name_for(dt): dt for dt in sorted(doctypes)}


def exposed_sets() -> dict[str, str]:
    """EntitySet name -> DocType, cached per site for 5 minutes."""
    key = "fraxis_gateway:odata_exposed"
    sets = frappe.cache.get_value(key)
    if sets is None:
        sets = _exposed_uncached()
        frappe.cache.set_value(key, sets, expires_in_sec=300)
    return sets


def readable_sets(filter_app: str | None = None) -> dict[str, str]:
    """Exposed sets the current user can read (optionally restricted to one app)."""
    sets = exposed_sets()
    if filter_app:
        modules = set(frappe.get_all("Module Def", filters={"app_name": filter_app}, pluck="name"))
        in_app = set(frappe.get_all("DocType", filters={"module": ["in", list(modules) or [""]]}, pluck="name"))
        sets = {k: v for k, v in sets.items() if v in in_app}
    return {k: v for k, v in sets.items() if frappe.has_permission(v, "read")}


def resolve_set(set_name: str) -> str | None:
    return exposed_sets().get(set_name)
