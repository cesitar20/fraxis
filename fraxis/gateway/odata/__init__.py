# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""OData v4 (JSON format, minimal metadata) remapping over Frappe's REST v2."""


class ODataError(Exception):
    """Client error in an OData request; rendered as an OData error payload."""

    def __init__(self, message: str, status_code: int = 400, code: str = "BadRequest"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
