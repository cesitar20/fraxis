# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

import secrets

import frappe
from frappe import _
from frappe.model.document import Document

from fraxis.gateway import config
from fraxis.gateway.odata import model


class FraxisSettings(Document):
    """Single DocType holding every setting of the Fraxis REST gateway (read via ``fraxis.gateway.config``)."""

    def validate(self):
        path, error = config.normalise_base_path(self.base_path)
        if error:
            frappe.throw(error, title=_("Invalid Base Path"))
        self.base_path = path

        for key in config.DEFAULTS:
            if (self.get(key) or 0) <= 0:
                self.set(key, config.DEFAULTS[key])
        if self.page_size > self.max_page_size:
            frappe.throw(_("Default Page Size cannot be greater than Max Page Size"))

        if not self.get_password("jwt_secret", raise_exception=False):
            self.jwt_secret = secrets.token_urlsafe(48)

        if self.only_expose:
            # The other exposure fields are read-only in the form while Only Expose has rows.
            self.exposed_apps = None
            self.also_expose = []
            self.never_expose = []

        self.validate_excluded_fields()
        self.validate_routes()

    def validate_excluded_fields(self):
        seen = set()
        for row in self.excluded_fields:
            row.fieldname = (row.fieldname or "").strip()
            if not frappe.db.exists("DocType", row.ref_doctype):
                continue  # the Link validation reports it
            if row.fieldname not in {f["fieldname"] for f in model.publishable_fields(row.ref_doctype)}:
                frappe.throw(
                    _("Excluded Fields row {0}: {1} never returns a field {2}").format(row.idx, row.ref_doctype, row.fieldname)
                )
            key = (row.ref_doctype, row.fieldname)
            if key in seen:
                frappe.throw(_("Excluded Fields row {0}: {1}.{2} is listed twice").format(row.idx, *key))
            seen.add(key)

    def validate_routes(self):
        exposed = config.compute_exposed(self)
        seen, doctype_of_path = set(), {}
        for row in self.routes:
            if row.ref_doctype in config.NEVER_EXPOSED:
                frappe.throw(_("Route row {0}: {1} can never be served by the gateway").format(row.idx, row.ref_doctype))
            meta = frappe.get_meta(row.ref_doctype) if frappe.db.exists("DocType", row.ref_doctype) else None
            if meta and (meta.istable or meta.issingle or meta.is_virtual):
                frappe.throw(_("Route row {0}: {1} is a child table, a Single or virtual").format(row.idx, row.ref_doctype))

            row.path, error = config.normalise_sub_path(row.get("path"))
            if error:
                frappe.throw(_("Route row {0}: {1}").format(row.idx, error))
            verb = row.http_method or "GET"
            path = f"/{row.sub_route}/{row.sub_category}" + (f"/{row.path}" if row.path else "")
            if (path, verb) in seen:
                frappe.throw(_("Route row {0}: {1} {2} is defined twice").format(row.idx, verb, path))
            seen.add((path, verb))
            if doctype_of_path.setdefault(path, row.ref_doctype) != row.ref_doctype:
                frappe.throw(
                    _("Route row {0}: {1} already serves {2}; one path serves one DocType").format(
                        row.idx, path, doctype_of_path[path]
                    )
                )

            row.route = config.route_label(self.base_path, row.sub_route, row.sub_category, row.path, verb)
            row.exposed = int(row.ref_doctype in exposed)

        hidden = sorted({row.ref_doctype for row in self.routes if not row.exposed})
        if hidden:
            frappe.msgprint(
                _("Routes over {0} are not served nor documented until the DocType is exposed.").format(
                    ", ".join(hidden)
                ),
                title=_("Routes not exposed"),
                indicator="orange",
            )

    def on_update(self):
        # Exposure set and the generated OpenAPI document depend on these settings.
        frappe.cache.delete_keys(config.CACHE_PREFIX)


@frappe.whitelist()
def get_doctype_fields(doctype: str) -> list[dict]:
    """Options of Excluded Fields > Field: what ``doctype`` can return through the gateway."""
    frappe.only_for("System Manager")
    if not frappe.db.exists("DocType", doctype):
        return []
    return model.publishable_fields(doctype)
