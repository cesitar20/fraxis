# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Excluded Fields went from one row per field (``fieldname``) to one row per DocType with a
comma-separated ``fieldnames``: merge the old rows so nothing that was excluded is exposed.
The old ``fieldname`` column is left in the table by the model sync and read here.
"""

import frappe

from fraxis.gateway import config

DOCTYPE = "Fraxis Excluded Field"


def merge(rows: list[dict]) -> tuple[dict[str, str], list[str]]:
    """``(row name -> merged fieldnames, row names to delete)``: the first row of each DocType
    per parent keeps every field of that DocType, in order and without repeats."""
    keep: dict[tuple, dict] = {}
    fields: dict[str, list[str]] = {}
    delete = []
    for row in sorted(rows, key=lambda r: (r["parent"], r["parentfield"], r["idx"] or 0)):
        key = (row["parent"], row["parentfield"], row["ref_doctype"])
        first = keep.setdefault(key, row)
        if first is not row:
            delete.append(row["name"])
        names = fields.setdefault(first["name"], [])
        for name in config.split_fieldnames(row.get("fieldnames")) + config.split_fieldnames(row.get("fieldname")):
            if name not in names:
                names.append(name)
    return {name: ", ".join(names) for name, names in fields.items()}, delete


def execute():
    if not frappe.db.has_column(DOCTYPE, "fieldname"):
        return  # installed after the change: nothing to merge
    rows = frappe.db.sql(
        f"select name, parent, parenttype, parentfield, idx, ref_doctype, fieldname, fieldnames from `tab{DOCTYPE}`",
        as_dict=True,
    )
    merged, delete = merge(rows)
    for name, fieldnames in merged.items():
        frappe.db.set_value(DOCTYPE, name, "fieldnames", fieldnames, update_modified=False)
    for name in delete:
        frappe.db.delete(DOCTYPE, {"name": name})

    # Renumber what is left so the grid shows 1..n.
    for parent, parentfield in {(r.parent, r.parentfield) for r in rows}:
        names = frappe.get_all(
            DOCTYPE, filters={"parent": parent, "parentfield": parentfield}, order_by="idx asc", pluck="name"
        )
        for idx, name in enumerate(names, start=1):
            frappe.db.set_value(DOCTYPE, name, "idx", idx, update_modified=False)

    frappe.clear_document_cache("Fraxis Settings", "Fraxis Settings")
    frappe.cache.delete_keys(config.CACHE_PREFIX)
