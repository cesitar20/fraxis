# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Fraxis REST gateway — a Bearer-protected envelope around Frappe's standard REST API.

Clients authenticate with ``Authorization: Bearer <api_key>:<api_secret>`` (the User's
Frappe API key pair) or with a short-lived JWT from ``/fraxis/auth/token`` (+ refresh).

No per-operation endpoint is written here. Requests under ``/fraxis/`` are rewritten in a
``before_request`` hook onto Frappe's own ``/api/v2/...`` routes (the same path-resolver
technique Dendriva uses for its OpenAI-compatible ``chat/completions`` path), so every
call still goes through Frappe's permissions, controllers, hooks and transaction handling:

    /fraxis/auth/token | refresh | revoke   -> JWT issue / rotation / revocation
    /fraxis/api/<rest>                      -> /api/v2/<rest>          (Bearer, raw Frappe shape)
    /fraxis/odata/<EntitySet>...            -> /api/v2/document/...    (Bearer, OData v4 shape)
    /fraxis/odata/$metadata                 -> CSDL XML of the exposed entity model
    /fraxis/openapi.json, /fraxis/docs     -> OpenAPI 3 + Scalar

Pipeline per request (see ``frappe.app.application``):

    init_request -> before_request  (paths.route_request: rewrite + OData translation)
                 -> validate_auth   (auth.validate_bearer as an ``auth_hooks`` entry)
                 -> frappe.api.handle(/api/v2/...)
                 -> after_request   (odata.response.reshape: Frappe JSON -> OData JSON)
"""
