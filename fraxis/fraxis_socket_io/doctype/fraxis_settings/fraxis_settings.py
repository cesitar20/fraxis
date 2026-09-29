# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

import re
import secrets

import frappe
from frappe import _
from frappe.model.document import Document

from fraxis.gateway import config


class FraxisSettings(Document):
    """Single DocType holding every setting of the Fraxis REST gateway (read via ``fraxis.gateway.config``)."""

    def validate(self):
        path, error = config.normalise_base_path(self.base_path)
        if error:
            frappe.throw(error, title=_("Invalid Base Path"))
        self.base_path = path

        for field in ("access_token_ttl", "refresh_token_ttl", "odata_page_size", "odata_max_page_size"):
            if (self.get(field) or 0) <= 0:
                self.set(field, config.DEFAULTS[field])
        if self.odata_page_size > self.odata_max_page_size:
            frappe.throw(_("Default Page Size cannot be greater than Max Page Size"))

        if not self.get_password("jwt_secret", raise_exception=False):
            self.jwt_secret = secrets.token_urlsafe(48)

        self.validate_odata_functions()

    def validate_odata_functions(self):
        seen = set()
        for row in self.odata_functions:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", row.function_name or ""):
                frappe.throw(_("Row {0}: Function Name must be an identifier (letters, digits, _)").format(row.idx))
            if row.function_name in seen:
                frappe.throw(_("Row {0}: duplicate Function Name {1}").format(row.idx, row.function_name))
            seen.add(row.function_name)
            try:
                fn = frappe.get_attr(row.method)
            except Exception:
                frappe.throw(_("Row {0}: method {1} does not exist").format(row.idx, row.method))
            if fn not in frappe.whitelisted:
                frappe.throw(_("Row {0}: {1} is not a @frappe.whitelist() method").format(row.idx, row.method))

    def on_update(self):
        # Exposure set and generated OpenAPI documents depend on these settings.
        frappe.cache.delete_keys("fraxis_gateway:")
