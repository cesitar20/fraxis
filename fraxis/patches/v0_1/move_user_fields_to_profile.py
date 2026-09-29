# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Move the gateway's per-user data off the core ``User`` DocType.

An earlier gateway build stored it as Custom Fields on User (``fraxis_api_enabled``,
``fraxis_token_version`` + layout fields). Users with access enabled get a
``Fraxis User Profile`` carrying the same state, then the Custom Fields and their
``tabUser`` columns are removed so User is back to its stock schema.
"""

import frappe

LEGACY_FIELDS = ("fraxis_api_section", "fraxis_api_enabled", "fraxis_api_column", "fraxis_token_version")
DATA_COLUMNS = ("fraxis_api_enabled", "fraxis_token_version")


def execute():
    frappe.reload_doc("fraxis_socket_io", "doctype", "fraxis_user_profile")

    if frappe.db.has_column("User", "fraxis_api_enabled"):
        users = frappe.db.sql(
            "select name, fraxis_token_version from `tabUser` where fraxis_api_enabled = 1", as_dict=True
        )
        for row in users:
            if not frappe.db.exists("Fraxis User Profile", row.name):
                frappe.get_doc(
                    {
                        "doctype": "Fraxis User Profile",
                        "user": row.name,
                        "api_enabled": 1,
                        "token_version": row.fraxis_token_version or 0,
                    }
                ).insert(ignore_permissions=True)

    for fieldname in LEGACY_FIELDS:
        frappe.delete_doc("Custom Field", f"User-{fieldname}", ignore_missing=True, force=True, ignore_permissions=True)

    # Custom Field.on_trash keeps the column; drop it so tabUser matches the stock schema.
    for column in DATA_COLUMNS:
        if frappe.db.has_column("User", column):
            frappe.db.sql_ddl(f"alter table `tabUser` drop column `{column}`")

    frappe.clear_cache(doctype="User")
