# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
OpenAPI 3.0 document for the gateway, rendered by Scalar (``<base_path>/docs``).

Follows the shape Microsoft's OData -> OpenAPI converter (Microsoft.OpenApi.OData)
produces: one tag per entity set, ``/Set`` + ``/Set('{name}')`` + ``/Set/$count`` paths,
``$select/$filter/$orderby/$top/$skip/$count`` as reusable parameters, and a
``{"value": [...]}`` collection envelope. Schemas come from the same model as ``$metadata``.
"""

import frappe

from fraxis import __version__
from fraxis.gateway import config
from fraxis.gateway.odata import model
from fraxis.gateway.paths import gateway_path, gateway_url

EDM_TO_SCHEMA = {
    "Edm.String": {"type": "string"},
    "Edm.Int64": {"type": "integer", "format": "int64"},
    "Edm.Boolean": {"type": "boolean"},
    "Edm.Double": {"type": "number", "format": "double"},
    "Edm.Decimal": {"type": "number", "format": "decimal"},
    "Edm.Date": {"type": "string", "format": "date"},
    "Edm.DateTimeOffset": {"type": "string", "format": "date-time"},
}
READ_ONLY = {"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx"}


def _ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def _prop_schema(p: model.Prop) -> dict:
    schema = dict(EDM_TO_SCHEMA.get(p.edm, {"type": "string"}))
    if p.nullable:
        schema["nullable"] = True
    if p.label and p.label != p.name:
        schema["title"] = p.label
    if p.fieldtype == "Select" and p.options:
        choices = [c for c in p.options.split("\n") if c]
        if choices:
            schema["enum"] = choices + ([None] if p.nullable else [])
    elif p.fieldtype == "Link" and p.options:
        schema["description"] = f"Link to {p.options}"
    return schema


def _object(props: dict[str, model.Prop], collections: dict[str, str] | None = None, writable=False) -> dict:
    properties = {}
    for name, p in props.items():
        if writable and name in READ_ONLY:
            continue
        properties[name] = _prop_schema(p)
    for name, child in (collections or {}).items():
        properties[name] = {"type": "array", "items": _ref(model.set_name_for(child))}
    schema = {"type": "object", "properties": properties}
    required = [n for n, p in props.items() if not p.nullable and not (writable and n in READ_ONLY)]
    if writable and required:
        schema["required"] = required
    return schema


def _error_responses() -> dict:
    return {
        "400": {"$ref": "#/components/responses/Error"},
        "401": {"$ref": "#/components/responses/Unauthorized"},
        "403": {"$ref": "#/components/responses/Error"},
        "404": {"$ref": "#/components/responses/Error"},
        "409": {"$ref": "#/components/responses/Error"},
    }


def _write_responses() -> dict:
    return {**_error_responses(), "412": {"$ref": "#/components/responses/Error"}}


def _entity_paths(set_name: str, entity: model.EntityType) -> dict:
    tag = [entity.doctype]
    key_param = {"name": "name", "in": "path", "required": True, "description": "Document name", "schema": {"type": "string"}}
    body = {"required": True, "content": {"application/json": {"schema": _ref(f"{set_name}-write")}}}
    one = {"description": "Entity", "content": {"application/json": {"schema": _ref(set_name)}}}
    return {
        f"/{set_name}": {
            "get": {
                "tags": tag,
                "summary": f"List {entity.doctype}",
                "operationId": f"{set_name}.list",
                "parameters": [{"$ref": f"#/components/parameters/{p}"} for p in ("select", "filter", "orderby", "top", "skip", "count")],
                "responses": {
                    "200": {
                        "description": "Collection",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "@odata.context": {"type": "string"},
                                        "@odata.count": {"type": "integer"},
                                        "@odata.nextLink": {"type": "string"},
                                        "value": {"type": "array", "items": _ref(set_name)},
                                    },
                                }
                            }
                        },
                    },
                    **_error_responses(),
                },
            },
            "post": {
                "tags": tag,
                "summary": f"Create {entity.doctype}",
                "operationId": f"{set_name}.create",
                "parameters": [{"$ref": "#/components/parameters/prefer"}],
                "requestBody": body,
                "responses": {"201": one, "204": {"description": "Created (Prefer: return=minimal); see Location"}, **_error_responses()},
            },
        },
        f"/{set_name}/$count": {
            "get": {
                "tags": tag,
                "summary": f"Count {entity.doctype}",
                "operationId": f"{set_name}.count",
                "parameters": [{"$ref": "#/components/parameters/filter"}],
                "responses": {
                    "200": {"description": "Count", "content": {"text/plain": {"schema": {"type": "integer"}}}},
                    **_error_responses(),
                },
            }
        },
        f"/{set_name}('{{name}}')": {
            "parameters": [key_param],
            "get": {
                "tags": tag,
                "summary": f"Get {entity.doctype}",
                "operationId": f"{set_name}.get",
                "parameters": [{"$ref": "#/components/parameters/select"}],
                "responses": {"200": one, **_error_responses()},
            },
            "patch": {
                "tags": tag,
                "summary": f"Update {entity.doctype} (partial)",
                "operationId": f"{set_name}.update",
                "parameters": [{"$ref": "#/components/parameters/ifMatch"}, {"$ref": "#/components/parameters/prefer"}],
                "requestBody": {**body, "content": {"application/json": {"schema": _ref(f"{set_name}-update")}}},
                "responses": {"200": one, "204": {"description": "Updated (Prefer: return=minimal)"}, **_write_responses()},
            },
            "delete": {
                "tags": tag,
                "summary": f"Delete {entity.doctype}",
                "operationId": f"{set_name}.delete",
                "parameters": [{"$ref": "#/components/parameters/ifMatch"}],
                "responses": {"204": {"description": "Deleted"}, **_write_responses()},
            },
        },
        f"/{set_name}('{{name}}')/{{operation}}": {
            "parameters": [
                key_param,
                {
                    "name": "operation",
                    "in": "path",
                    "required": True,
                    "description": "Whitelisted controller method (e.g. submit, cancel, add_comment); "
                    "`Fraxis.<method>` is accepted too",
                    "schema": {"type": "string"},
                },
            ],
            "get": {
                "tags": tag,
                "summary": f"Run a bound function on {entity.doctype} (read permission)",
                "operationId": f"{set_name}.function",
                "responses": {"200": {"$ref": "#/components/responses/OperationResult"}, **_error_responses()},
            },
            "post": {
                "tags": tag,
                "summary": f"Run a bound action on {entity.doctype} (write permission)",
                "operationId": f"{set_name}.action",
                "parameters": [{"$ref": "#/components/parameters/ifMatch"}],
                "requestBody": {"content": {"application/json": {"schema": {"type": "object", "description": "Method arguments"}}}},
                "responses": {"200": {"$ref": "#/components/responses/OperationResult"}, **_write_responses()},
            },
        },
    }


def _auth_paths() -> dict:
    token_schema = {
        "type": "object",
        "properties": {
            "access_token": {"type": "string"},
            "token_type": {"type": "string", "example": "Bearer"},
            "expires_in": {"type": "integer"},
            "refresh_token": {"type": "string"},
            "refresh_expires_in": {"type": "integer"},
            "refresh_url": {"type": "string", "format": "uri", "description": "POST here with refresh_token before expires_in"},
            "revoke_url": {"type": "string", "format": "uri"},
        },
    }
    grant = {
        "type": "object",
        "required": ["grant_type"],
        "properties": {
            "grant_type": {"type": "string", "enum": ["password", "api_key", "refresh_token"]},
            "username": {"type": "string", "description": "grant_type=password"},
            "password": {"type": "string", "format": "password", "description": "grant_type=password"},
            "api_key": {"type": "string", "description": "grant_type=api_key"},
            "api_secret": {"type": "string", "format": "password", "description": "grant_type=api_key"},
            "refresh_token": {"type": "string", "description": "grant_type=refresh_token"},
        },
    }
    oauth_error = {"description": "OAuth2 error", "content": {"application/json": {"schema": _ref("OAuthError")}}}
    content = {"application/json": {"schema": grant}, "application/x-www-form-urlencoded": {"schema": grant}}
    return {
        "/auth/token": {
            "post": {
                "tags": ["Auth"],
                "summary": "Issue an access + refresh token pair",
                "description": "grant_type=api_key also accepts the key pair as `Authorization: Bearer <api_key>:<api_secret>` "
                "(grant_type may then be omitted).",
                "security": [],
                "requestBody": {"required": True, "content": content},
                "responses": {
                    "200": {"description": "Tokens", "content": {"application/json": {"schema": token_schema}}},
                    "400": oauth_error,
                    "401": oauth_error,
                },
            }
        },
        "/auth/refresh": {
            "post": {
                "tags": ["Auth"],
                "summary": "Rotate a refresh token",
                "security": [],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {"type": "object", "required": ["refresh_token"], "properties": {"refresh_token": {"type": "string"}}}}},
                },
                "responses": {
                    "200": {"description": "Tokens", "content": {"application/json": {"schema": token_schema}}},
                    "400": oauth_error,
                },
            }
        },
        "/auth/revoke": {
            "post": {
                "tags": ["Auth"],
                "summary": "Revoke a refresh token (and its rotation family)",
                "security": [],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {"type": "object", "required": ["token"], "properties": {"token": {"type": "string"}}}}},
                },
                "responses": {"200": {"description": "Revoked (also returned for unknown tokens, RFC 7009)"}},
            }
        },
        "/auth/me": {
            "get": {
                "tags": ["Auth"],
                "summary": "Authenticated user, roles and Fraxis User Profile custom data",
                "responses": {
                    "200": {
                        "description": "`{\"data\": {user, full_name, user_type, roles, custom_data}}`",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    },
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                },
            }
        },
        "/odata/{operation}": {
            "parameters": [
                {
                    "name": "operation",
                    "in": "path",
                    "required": True,
                    "description": "Dotted path of a whitelisted method",
                    "schema": {"type": "string"},
                    "example": "frappe.auth.get_logged_user",
                }
            ],
            "get": {
                "tags": ["OData operations"],
                "summary": "Unbound function (GET, arguments in the query string)",
                "responses": {"200": {"$ref": "#/components/responses/OperationResult"}, **_error_responses()},
            },
            "post": {
                "tags": ["OData operations"],
                "summary": "Unbound action (POST, arguments in the JSON body)",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                "responses": {"200": {"$ref": "#/components/responses/OperationResult"}, **_error_responses()},
            },
        },
        "/api/method/{method}": {
            "parameters": [{"name": "method", "in": "path", "required": True, "schema": {"type": "string"}, "example": "frappe.auth.get_logged_user"}],
            "get": {
                "tags": ["Frappe REST v2"],
                "summary": "Call a whitelisted method (passthrough to /api/v2/method)",
                "responses": {"200": {"description": "`{\"data\": ...}`"}, **_error_responses()},
            },
            "post": {
                "tags": ["Frappe REST v2"],
                "summary": "Call a whitelisted method (passthrough to /api/v2/method)",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                "responses": {"200": {"description": "`{\"data\": ...}`"}, **_error_responses()},
            },
        },
    }


def _components() -> dict:
    return {
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "api_key:api_secret | JWT",
                "description": "`Authorization: Bearer <api_key>:<api_secret>` (User > API Access > Generate Keys), "
                "or a Fraxis access token from `POST /auth/token`.",
            },
            "oauth2Password": {
                "type": "oauth2",
                "flows": {"password": {"tokenUrl": gateway_url("/auth/token"), "refreshUrl": gateway_url("/auth/refresh"), "scopes": {}}},
            },
        },
        "parameters": {
            "select": {"name": "$select", "in": "query", "description": "Comma-separated properties", "schema": {"type": "string"}},
            "filter": {
                "name": "$filter",
                "in": "query",
                "description": "eq ne gt ge lt le, in (...), contains/startswith/endswith, and/or/not. Shape: `a and b and (c or d)`",
                "schema": {"type": "string"},
                "example": "status eq 'Open' and contains(description,'demo')",
            },
            "orderby": {"name": "$orderby", "in": "query", "schema": {"type": "string"}, "example": "modified desc"},
            "top": {"name": "$top", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": int(config.get("odata_max_page_size"))}},
            "skip": {"name": "$skip", "in": "query", "schema": {"type": "integer", "minimum": 0}},
            "count": {"name": "$count", "in": "query", "schema": {"type": "boolean"}},
            "ifMatch": {
                "name": "If-Match",
                "in": "header",
                "description": "ETag from a previous read (`@odata.etag`); 412 if the entity changed since",
                "schema": {"type": "string"},
            },
            "prefer": {
                "name": "Prefer",
                "in": "header",
                "description": "`return=minimal` answers 204 without a body",
                "schema": {"type": "string", "enum": ["return=representation", "return=minimal"]},
            },
        },
        "responses": {
            "Error": {"description": "OData error", "content": {"application/json": {"schema": _ref("ODataError")}}},
            "OperationResult": {
                "description": "Method result",
                "content": {"application/json": {"schema": {"type": "object", "properties": {"@odata.context": {"type": "string"}, "value": {}}}}},
            },
            "Unauthorized": {"description": "Missing, expired or revoked access token", "content": {"application/json": {"schema": _ref("ODataError")}}},
        },
        "schemas": {
            "ODataError": {
                "type": "object",
                "properties": {"error": {"type": "object", "properties": {"code": {"type": "string"}, "message": {"type": "string"}}}},
            },
            "OAuthError": {"type": "object", "properties": {"error": {"type": "string"}, "error_description": {"type": "string"}}},
        },
    }


def build(sets: dict[str, str], title_suffix: str = "") -> dict:
    components = _components()
    schemas = components["schemas"]
    paths: dict = {}
    tags = [{"name": "Auth"}, {"name": "OData operations"}, {"name": "Frappe REST v2"}]
    odata_paths: dict = {}
    for set_name, doctype in sets.items():
        entity = model.entity_type(doctype)
        schemas[set_name] = _object(entity.props, entity.collections)
        write = _object(entity.props, entity.collections, writable=True)
        schemas[f"{set_name}-write"] = write
        schemas[f"{set_name}-update"] = {k: v for k, v in write.items() if k != "required"}
        for child in entity.collections.values():
            child_name = model.set_name_for(child)
            if child_name not in schemas:
                schemas[child_name] = _object(model.complex_type(child))
        odata_paths.update(_entity_paths(set_name, entity))
        tags.append({"name": doctype, "description": entity.description or f"OData entity set `{set_name}`"})

    paths.update(_auth_paths())
    paths.update({f"/odata{path}": item for path, item in odata_paths.items()})
    return {
        "openapi": "3.0.3",
        "info": {
            "title": f"Fraxis REST gateway{title_suffix}",
            "version": __version__,
            "description": (
                "Bearer-protected envelope over Frappe's REST API.\n\n"
                "**Authentication** — send on every call:\n\n"
                "    Authorization: Bearer <api_key>:<api_secret>\n\n"
                "with the User's Frappe API key pair (User > API Access > Generate Keys).\n\n"
                "Alternatively, exchange credentials for a short-lived JWT: `POST /auth/token` "
                "(`grant_type=password`, or `api_key` / the same Bearer header), send `Bearer <access_token>`, "
                "and `POST /auth/refresh` before it expires (refresh tokens rotate on every use).\n\n"
                "The user needs a **Fraxis User Profile** with *API Enabled*. Frappe permissions apply unchanged.\n\n"
                "**OData V2** (for V2-only clients such as SAP pyodata) is served at `/odata/v2/` with its own "
                "`$metadata`; FunctionImports are configured in Fraxis Settings. This document describes the V4 dialect."
            ),
        },
        "servers": [{"url": gateway_url(), "description": frappe.local.site}],
        "security": [{"bearerAuth": []}, {"oauth2Password": []}],
        "tags": tags,
        "paths": paths,
        "components": components,
    }
