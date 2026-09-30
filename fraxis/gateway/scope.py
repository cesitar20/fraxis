# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Extra Params of the caller's ``Fraxis User Profile``, forced on every request against their
DocType (e.g. ``organization_id = 67edad81…`` on ``AI Assistant``):

* list        -> added to the filters
* read / update / delete of a document outside them -> 404, as if it did not exist
* create      -> the value is set (or checked when the body sends one)
* update      -> cannot move the document outside them

Several rows with the same DocType and field allow any of their values.
"""

import frappe
from frappe import _

from fraxis.gateway.odata import ODataError

PARAM = "Fraxis Profile Param"
# Standard columns an extra param may target besides the DocType's own fields.
STANDARD_FIELDS = ("name", "owner")
_TRUE, _FALSE = ("1", "true", "yes"), ("0", "false", "no", "")


def cast(value, fieldtype: str):
    """Value of a param (or of a document / request field) as the param's type. Raises ValueError."""
    if value is None:
        return None
    if fieldtype == "Int":
        return int(value)
    if fieldtype == "Float":
        return float(value)
    if fieldtype == "Check":
        text = str(value).strip().lower()
        if text in _TRUE:
            return 1
        if text in _FALSE:
            return 0
        raise ValueError(value)
    return str(value)


def _params() -> dict[str, dict[str, tuple[str, list]]]:
    """DocType -> fieldname -> (fieldtype, allowed values) for the authenticated gateway user."""
    params = getattr(frappe.local, "fraxis_params", None)
    if params is None or params[0] != frappe.local.fraxis_user:
        rows = frappe.get_all(
            PARAM,
            filters={"parenttype": "Fraxis User Profile", "parent": frappe.local.fraxis_user},
            fields=["ref_doctype", "fieldname", "fieldtype", "value"],
            order_by="idx asc",
        )
        out: dict = {}
        for row in rows:
            fieldtype, values = out.setdefault(row.ref_doctype, {}).setdefault(
                row.fieldname, (row.fieldtype or "Data", [])
            )
            values.append(cast(row.value, fieldtype))
        params = (frappe.local.fraxis_user, out)
        frappe.local.fraxis_params = params
    return params[1]


def for_doctype(doctype: str) -> dict[str, tuple[str, list]]:
    return _params().get(doctype, {})


def list_filters(doctype: str) -> list:
    return [
        [doctype, fieldname, "in" if len(values) > 1 else "=", values if len(values) > 1 else values[0]]
        for fieldname, (_fieldtype, values) in for_doctype(doctype).items()
    ]


def _allowed(value, fieldtype: str, values: list) -> bool:
    try:
        return cast(value, fieldtype) in values
    except (TypeError, ValueError):
        return False


def assert_in_scope(doc) -> None:
    for fieldname, (fieldtype, values) in for_doctype(doc.doctype).items():
        if not _allowed(doc.get(fieldname), fieldtype, values):
            raise frappe.DoesNotExistError(_("{0} {1} not found").format(_(doc.doctype), doc.name))


def apply_to_new(doctype: str, data: dict) -> None:
    for fieldname, (fieldtype, values) in for_doctype(doctype).items():
        if fieldname in data:
            if not _allowed(data[fieldname], fieldtype, values):
                raise frappe.PermissionError(_("{0} must be one of: {1}").format(fieldname, ", ".join(map(str, values))))
        elif len(values) == 1:
            data[fieldname] = values[0]
        else:
            raise ODataError(_("{0} is required, one of: {1}").format(fieldname, ", ".join(map(str, values))))


def check_update(doctype: str, data: dict) -> None:
    for fieldname, (fieldtype, values) in for_doctype(doctype).items():
        if fieldname in data and not _allowed(data[fieldname], fieldtype, values):
            raise frappe.PermissionError(_("{0} must be one of: {1}").format(fieldname, ", ".join(map(str, values))))
