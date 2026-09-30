# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
OpenAPI 3.0 document rendered by Scalar at ``<base_path>/docs``.

* ``Authentication`` — token, refresh and revoke with request / response examples, and an
  OAuth2 *client credentials* scheme so Scalar itself can fetch the token (Client ID = API
  Key, Client Secret = API Secret).
* One folder per Sub Route (Stats, Assistants, Numbers) with a sub-folder per Sub Category
  (``x-tagGroups``: group = Sub Route, tag = Sub Category) holding the routes of exposed
  DocTypes; schemas come from the same entity model the routes serve, so excluded fields
  never appear.
"""

import re

import frappe

from fraxis import __version__
from fraxis.gateway import config, router
from fraxis.gateway.odata import model

# Public title of the docs: nothing on the page may name the platform behind the API.
DOCS_TITLE = "API Documentation"
INT_TYPES = ("Int", "Long Int", "Duration")
FLOAT_TYPES = ("Float", "Currency", "Percent", "Rating")
ERROR_EXAMPLES = {
    "400": ("BadRequest", "Unknown property in $filter: 'foo'"),
    "401": ("Unauthorized", "Access token expired"),
    "403": ("Forbidden", "Insufficient Permission for AI Assistant"),
    "404": ("NotFound", "AI Assistant AI-ASSISTANT-000001 not found"),
    "405": ("MethodNotAllowed", "DELETE is not allowed on this route"),
    "409": ("Conflict", "AI Assistant AI-ASSISTANT-000001 already exists"),
    "412": ("PreconditionFailed", "The document was modified by someone else (ETag mismatch)"),
}
ERROR_TEXT = {
    "400": "Invalid query option, property or value",
    "401": "Missing, invalid, expired or revoked access token",
    "403": "The user's permissions or Extra Params do not allow it",
    "404": "No such document (or outside the caller's Extra Params)",
    "405": "HTTP method not mapped on this route",
    "409": "Duplicate document",
    "412": "If-Match does not match the current ETag",
}


def _ref(name: str, kind: str = "schemas") -> dict:
    return {"$ref": f"#/components/{kind}/{name}"}


def _errors(*codes: str) -> dict:
    return {code: _ref(f"Error{code}", "responses") for code in codes}


# --- schemas ------------------------------------------------------------------------------

def _prop_schema(p: model.Prop) -> dict:
    if p.fieldtype in INT_TYPES:
        schema = {"type": "integer"}
    elif p.fieldtype in FLOAT_TYPES:
        schema = {"type": "number"}
    elif p.fieldtype == "Check":
        schema = {"type": "boolean"}
    elif p.fieldtype == "Date":
        schema = {"type": "string", "format": "date"}
    elif p.fieldtype == "Datetime":
        schema = {"type": "string", "format": "date-time"}
    elif p.fieldtype == "JSON":
        schema = {"description": "JSON value"}
    else:
        schema = {"type": "string"}
    if p.label and p.label != p.name:
        schema["title"] = p.label
    if p.fieldtype == "Select" and p.options:
        if choices := [c for c in p.options.split("\n") if c]:
            schema["enum"] = choices
    elif p.fieldtype == "Link" and p.options:
        schema["description"] = f"Link to {p.options}"
    if p.read_only:
        schema["readOnly"] = True
    if not p.required and "type" in schema:
        schema["nullable"] = True
    return schema


def _object(props: dict[str, model.Prop], collections: dict[str, str] | None = None, write: bool = False) -> dict:
    """``collections``: child table fieldname -> schema name of its rows."""
    properties = {n: _prop_schema(p) for n, p in props.items() if not (write and p.read_only)}
    for fieldname, child_schema in (collections or {}).items():
        properties[fieldname] = {"type": "array", "items": _ref(child_schema)}
    required = [n for n, p in props.items() if p.required and n in properties]
    return {"type": "object", "properties": properties, **({"required": required} if required else {})}


def _entity_schemas(entity: model.Entity, public_name: str, schemas: dict) -> str:
    """Schemas named after the route's Public Name, never after the DocType (child rows too)."""
    name = model.schema_name(public_name)
    children = {fieldname: name + model.schema_name(fieldname) for fieldname in entity.collections}
    schemas[name] = {**_object(entity.props, children), "description": public_name}
    schemas[f"{name}Input"] = _object(entity.props, children, write=True)
    for fieldname, child_schema in children.items():
        schemas.setdefault(child_schema, _object(entity.child_props(fieldname)))
    return name


# --- operations ---------------------------------------------------------------------------

def _list_parameters(entity: model.Entity) -> list[dict]:
    fields = [n for n in entity.props if n != "name"][:3]
    date_field = "modified" if "modified" in entity.props else None
    return [
        {"name": "$select", "in": "query", "description": "Comma-separated properties to return", "schema": {"type": "string"},
         **({"example": ",".join(["name", *fields])} if fields else {})},
        {"name": "$filter", "in": "query", "schema": {"type": "string"},
         "description": "OData filter: `eq ne gt ge lt le`, `in (...)`, `contains/startswith/endswith(field,'text')`, "
         "`and`, `or`, `not`, parentheses. Shape: `a and b and (c or d)`. Strings in single quotes, "
         "datetimes as ISO 8601 (`2026-09-01T00:00:00Z`).",
         **({"example": f"{date_field} ge 2026-09-01T00:00:00Z"} if date_field else {})},
        {"name": "$orderby", "in": "query", "description": "Default `modified desc`", "schema": {"type": "string"},
         "example": "modified desc"},
        {"name": "$top", "in": "query", "description": f"Page size (default {config.get_int('page_size')})",
         "schema": {"type": "integer", "minimum": 1, "maximum": config.get_int("max_page_size")}},
        {"name": "$skip", "in": "query", "description": "Documents to skip", "schema": {"type": "integer", "minimum": 0}},
        {"name": "$count", "in": "query", "description": "Add `@odata.count` (total matching documents)",
         "schema": {"type": "boolean"}},
    ]


def _tag(spec: config.RouteSpec) -> str:
    """Tag of a route: unique per path (``stats/records``), shown as its Sub Category."""
    return f"{spec.sub_route}/{spec.sub_category}"


def _operations(spec: config.RouteSpec, entity: model.Entity, schema: str) -> tuple[dict, dict]:
    tag = [_tag(spec)]
    public_name = spec.public_name
    op_id = re.sub(r"[^A-Za-z0-9_]", "_", "_".join(spec.segments))
    one = {"description": "Document", "headers": {"ETag": {"schema": {"type": "string"}}},
           "content": {"application/json": {"schema": _ref(schema)}}}
    body = {"required": True, "content": {"application/json": {"schema": _ref(f"{schema}Input")}}}
    if_match = {"name": "If-Match", "in": "header", "schema": {"type": "string"},
                "description": "ETag of a previous read (`@odata.etag`); 412 if the document changed since"}
    name_param = {"name": "name", "in": "path", "required": True, "description": f"{public_name} ID",
                  "schema": {"type": "string"}}

    def summary(verb: str, fieldname: str) -> dict:
        """Operation name from the route row (Fraxis Route > API Names); its description below it."""
        text = spec.verbs[verb]["description"]
        return {"summary": spec.verbs[verb]["names"][fieldname], **({"description": text} if text else {})}

    collection, item = {}, {}
    if "GET" in spec.verbs:
        collection["get"] = {
            "tags": tag, "operationId": f"{op_id}_list", **summary("GET", "list_name"),
            "parameters": _list_parameters(entity),
            "responses": {
                "200": {"description": "Page of documents", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "@odata.count": {"type": "integer", "description": "Only with $count=true"},
                        "value": {"type": "array", "items": _ref(schema)},
                        "@odata.nextLink": {"type": "string", "format": "uri",
                                            "description": "Next page; absent on the last one"},
                    },
                }}}},
                **_errors("400", "401", "403"),
            },
        }
        item["get"] = {
            "tags": tag, "operationId": f"{op_id}_get", **summary("GET", "get_name"),
            "parameters": [{"name": "$select", "in": "query", "schema": {"type": "string"},
                            "description": "Comma-separated properties (child tables allowed)"}],
            "responses": {"200": one, **_errors("400", "401", "403", "404")},
        }
    if "POST" in spec.verbs:
        collection["post"] = {
            "tags": tag, "operationId": f"{op_id}_create", **summary("POST", "create_name"),
            "requestBody": body,
            "responses": {"201": {**one, "description": "Created; `Location` has its URL"},
                          **_errors("400", "401", "403", "409")},
        }
    if "PATCH" in spec.verbs:
        item["patch"] = {
            "tags": tag, "operationId": f"{op_id}_update", **summary("PATCH", "update_name"),
            "parameters": [if_match],
            "requestBody": {**body, "content": {"application/json": {"schema": {
                **_ref(f"{schema}Input"), "description": "Only the properties to change"}}}},
            "responses": {"200": one, **_errors("400", "401", "403", "404", "412")},
        }
    if "DELETE" in spec.verbs:
        item["delete"] = {
            "tags": tag, "operationId": f"{op_id}_delete", **summary("DELETE", "delete_name"),
            "parameters": [if_match],
            "responses": {"204": {"description": "Deleted"}, **_errors("401", "403", "404", "412")},
        }
    if item:
        item = {"parameters": [name_param], **item}
    return collection, item


# --- authentication -------------------------------------------------------------------------

AUTH_TAG = "auth"
KEY_EXAMPLE = {"api_key": "a1b2c3d4e5f6g7h", "api_secret": "9z8y7x6w5v4u3t2"}
REFRESH_EXAMPLE = "rt1.Q2hhbmdlIG1lIC0gZXhhbXBsZSByZWZyZXNoIHRva2Vu..."


def _token_example() -> dict:
    return {"access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...", "token_type": "Bearer",
            "expires_in": config.get_int("access_token_ttl"), "refresh_token": REFRESH_EXAMPLE,
            "refresh_expires_in": config.get_int("refresh_token_ttl")}


def _oauth_error(code: str, text: str) -> dict:
    return {"description": text, "content": {"application/json": {
        "schema": _ref("TokenError"), "example": {"error": code, "error_description": text}}}}


def _tokens_ok(description: str) -> dict:
    return {"description": description, "content": {"application/json": {
        "schema": _ref("TokenResponse"), "example": _token_example()}}}


def _auth_paths() -> dict:
    token = {"post": {
        "tags": [AUTH_TAG],
        "operationId": "auth_token",
        "summary": "Get an access token",
        "description": "Send the API Key and API Secret provided to you. Every other route only accepts the "
        "returned `access_token` as `Authorization: Bearer <access_token>`. Keep the `refresh_token` to renew "
        "it with **Refresh the access token**. Also accepted as OAuth2 client credentials "
        "(`grant_type=client_credentials` with `client_id` / `client_secret`, in the form body or as HTTP Basic) "
        "and `grant_type=refresh_token`.",
        "security": [],
        "requestBody": {"required": True, "content": {
            "application/json": {"schema": _ref("TokenRequest"), "example": KEY_EXAMPLE},
            "application/x-www-form-urlencoded": {
                "schema": _ref("ClientCredentials"),
                "example": {"grant_type": "client_credentials", "client_id": KEY_EXAMPLE["api_key"],
                            "client_secret": KEY_EXAMPLE["api_secret"]},
            },
        }},
        "responses": {
            "200": _tokens_ok("Access and refresh token"),
            "400": _oauth_error("invalid_request", "api_key and api_secret are required"),
            "401": _oauth_error("invalid_client", "Invalid api_key or api_secret"),
            "403": _oauth_error("unauthorized_client",
                                "API access is not enabled for this account"),
            "429": {"description": "Too many token requests (30 per minute)"},
        },
    }}
    refresh = {"post": {
        "tags": [AUTH_TAG],
        "operationId": "auth_refresh",
        "summary": "Refresh the access token",
        "description": "Exchange the `refresh_token` for a new access token **and a new refresh token**; the one sent "
        "stops working (rotation). Sending an already used refresh token again ends the whole session. When the "
        "refresh token expires, request new tokens with the API keys.",
        "security": [],
        "requestBody": {"required": True, "content": {"application/json": {
            "schema": _ref("RefreshRequest"), "example": {"refresh_token": REFRESH_EXAMPLE}}}},
        "responses": {
            "200": _tokens_ok("New access and refresh token"),
            "400": _oauth_error("invalid_grant", "Refresh token is no longer valid"),
            "429": {"description": "Too many requests (60 per minute)"},
        },
    }}
    revoke = {"post": {
        "tags": [AUTH_TAG],
        "operationId": "auth_revoke",
        "summary": "Revoke (log out)",
        "description": "Ends the session of a refresh token or an access token: the refresh token and every access "
        "token issued from the same login stop working at once. Answers 200 even for unknown tokens (RFC 7009).",
        "security": [],
        "requestBody": {"required": True, "content": {"application/json": {
            "schema": _ref("RevokeRequest"), "example": {"token": REFRESH_EXAMPLE}}}},
        "responses": {
            "200": {"description": "Session ended", "content": {"application/json": {
                "schema": {"type": "object", "properties": {"revoked": {"type": "boolean"}}},
                "example": {"revoked": True}}}},
        },
    }}
    return {"/auth/token": token, "/auth/refresh": refresh, "/auth/revoke": revoke}


def _description(example_path: str | None) -> str:
    token_url = router.gateway_url("/auth/token")
    example_url = router.gateway_url(example_path or "/stats/records")
    refresh_url = router.gateway_url("/auth/refresh")
    revoke_url = router.gateway_url("/auth/revoke")
    ttl = config.get_int("access_token_ttl")
    refresh_ttl = config.get_int("refresh_token_ttl")
    return f"""\
## How to authenticate

Your **API Key** and **API Secret** are provided to you together with your access to this API.

**1. Get the tokens** with your API Key and API Secret:

```bash
curl -X POST "{token_url}" \\
  -H "Content-Type: application/json" \\
  -d '{{"api_key": "<api_key>", "api_secret": "<api_secret>"}}'
```

```json
{{"access_token": "eyJhbGciOiJIUzI1NiIs...", "token_type": "Bearer", "expires_in": {ttl},
 "refresh_token": "rt1.Q2hhbmdl...", "refresh_expires_in": {refresh_ttl}}}
```

**2. Send it on every request** — no other credential is accepted by the routes:

```bash
curl "{example_url}" -H "Authorization: Bearer <access_token>"
```

**3. Renew it** when a route answers `401 Access token expired` (after {ttl} seconds). The answer is a new
access token **and a new refresh token**; the old refresh token stops working:

```bash
curl -X POST "{refresh_url}" \\
  -H "Content-Type: application/json" \\
  -d '{{"refresh_token": "<refresh_token>"}}'
```

When the refresh token itself expires (after {refresh_ttl} seconds), repeat step 1.

**4. Log out** — ends the session: the refresh token and every access token of that login stop working:

```bash
curl -X POST "{revoke_url}" \\
  -H "Content-Type: application/json" \\
  -d '{{"token": "<refresh_token>"}}'
```

If your API Key and API Secret are replaced or your access is disabled, every token stops working at once.

To try the routes from this page, open **Authentication** and choose *OAuth2 client credentials*:
Client ID = API Key, Client Secret = API Secret, then *Authorize*.

## Queries

Collections accept OData options: `$select`, `$filter`, `$orderby`, `$top`, `$skip`, `$count`, e.g.
`?$filter=status eq 'Active' and modified ge 2026-09-01T00:00:00Z&$orderby=modified desc&$top=50&$count=true`.
Pages carry `@odata.nextLink` until the last one. Errors are `{{"error": {{"code", "message"}}}}`.
"""


def _components(schemas: dict) -> dict:
    responses = {
        f"Error{code}": {"description": ERROR_TEXT[code], "content": {"application/json": {
            "schema": _ref("Error"), "example": {"error": {"code": err, "message": msg}}}}}
        for code, (err, msg) in ERROR_EXAMPLES.items()
    }
    schemas.update({
        "Error": {"type": "object", "properties": {"error": {"type": "object", "properties": {
            "code": {"type": "string"}, "message": {"type": "string"}}}}},
        "TokenRequest": {"type": "object", "required": ["api_key", "api_secret"], "properties": {
            "api_key": {"type": "string", "description": "API Key provided to you"},
            "api_secret": {"type": "string", "format": "password", "description": "API Secret provided to you"}}},
        "ClientCredentials": {"type": "object", "required": ["grant_type", "client_id", "client_secret"], "properties": {
            "grant_type": {"type": "string", "enum": ["client_credentials"]},
            "client_id": {"type": "string", "description": "API Key"},
            "client_secret": {"type": "string", "format": "password", "description": "API Secret"}}},
        "TokenResponse": {"type": "object", "properties": {
            "access_token": {"type": "string"},
            "token_type": {"type": "string", "enum": ["Bearer"]},
            "expires_in": {"type": "integer", "description": "Seconds until the access token expires"},
            "refresh_token": {"type": "string", "description": "Send to /auth/refresh; valid once"},
            "refresh_expires_in": {"type": "integer", "description": "Seconds until the refresh token expires"}}},
        "RefreshRequest": {"type": "object", "required": ["refresh_token"], "properties": {
            "refresh_token": {"type": "string"}}},
        "RevokeRequest": {"type": "object", "required": ["token"], "properties": {
            "token": {"type": "string", "description": "Refresh token (or access token) of the session to end"}}},
        "TokenError": {"type": "object", "properties": {
            "error": {"type": "string"}, "error_description": {"type": "string"}}},
    })
    return {
        "securitySchemes": {
            "clientCredentials": {
                "type": "oauth2",
                "description": "Client ID = API Key, Client Secret = API Secret.",
                "flows": {"clientCredentials": {
                    "tokenUrl": router.gateway_url("/auth/token"),
                    "refreshUrl": router.gateway_url("/auth/refresh"),
                    "scopes": {},
                }},
            },
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "`access_token` returned by POST /auth/token.",
            },
        },
        "responses": responses,
        "schemas": schemas,
    }


def build() -> dict:
    exposed = config.exposed_doctypes()
    specs = sorted(
        (s for s in config.routes().values() if s.doctype in exposed),
        key=lambda s: config.SUB_ROUTES.index(s.sub_route) if s.sub_route in config.SUB_ROUTES else 99,
    )
    schemas: dict = {}
    paths: dict = _auth_paths()
    tags = [{"name": AUTH_TAG, "x-displayName": "Access tokens",
             "description": "Get, refresh and revoke the tokens every route needs."}]
    # Folders in Scalar: one group per Sub Route, one tag (sub-folder) per Sub Category.
    groups: dict[str, list[str]] = {"Authentication": [AUTH_TAG]}

    for spec in specs:
        entity = model.entity(spec.doctype)
        schema = _entity_schemas(entity, spec.public_name, schemas)
        collection, item = _operations(spec, entity, schema)
        if collection:
            paths[spec.path] = collection
        if item:
            paths[f"{spec.path}/{{name}}"] = item
        # Routes with a Path share the sub-folder of their Sub Category.
        group = groups.setdefault(spec.sub_route.capitalize(), [])
        if _tag(spec) not in group:
            group.append(_tag(spec))
            tags.append({"name": _tag(spec), "x-displayName": spec.sub_category.capitalize()})

    first_list = next((s.path for s in specs if "GET" in s.verbs), None)
    return {
        "openapi": "3.0.3",
        "info": {"title": DOCS_TITLE, "version": __version__, "description": _description(first_list)},
        "servers": [{"url": router.gateway_url(), "description": frappe.local.site}],
        "security": [{"bearerAuth": []}, {"clientCredentials": []}],
        "tags": tags,
        "x-tagGroups": [{"name": name, "tags": group} for name, group in groups.items()],
        "paths": paths,
        "components": _components(schemas),
    }
