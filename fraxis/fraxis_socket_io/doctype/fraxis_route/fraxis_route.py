# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

from frappe.model.document import Document


class FraxisRoute(Document):
    """Gateway route ``<base_path>/<sub_route>[/<sub_category>]`` over a DocType (row of Fraxis Settings)."""
