# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
The part of OData V4 the gateway uses, as a helper library over Frappe's document API:

* :mod:`.model`         — entity properties from DocType meta, minus excluded / Password fields
* :mod:`.filter_parser` — ``$filter`` -> Frappe ``filters`` / ``or_filters`` (pure module)
* :mod:`.query`         — ``$select $filter $orderby $top $skip $count`` -> ``frappe.get_list`` kwargs
* :mod:`.serialize`     — rows / documents / errors as OData JSON, request bodies back to Frappe values

No ``$metadata`` document is served: the contract of each route is its OpenAPI description.
"""


class ODataError(Exception):
    """Client error in a gateway request; rendered as ``{"error": {"code", "message"}}``."""

    def __init__(self, message: str, status_code: int = 400, code: str = "BadRequest"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
