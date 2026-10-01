# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Gateway settings, read from the ``Fraxis Settings`` Single (database only — nothing in
``site_config.json``).

``before_request`` runs on every request of the site, so the Single is memoised per request
(``frappe.get_cached_doc`` already keeps it in Redis and Frappe clears it on save) and every
reader falls back to a safe default instead of raising. Until the DocType is migrated onto a
site, the defaults apply.
"""

import re
from dataclasses import dataclass, field

import frappe
from frappe import _

SETTINGS = "Fraxis Settings"
CACHE_PREFIX = "fraxis_gateway:"
DEFAULT_BASE_PATH = "/fraxis"
# Frappe's own top-level routes: a gateway prefix here would shadow or be shadowed by them.
RESERVED_PREFIXES = ("/api", "/app", "/assets", "/files", "/private", "/backups", "/socket.io", "/login", "/desk")
SUB_ROUTES = ("stats", "assistants", "numbers", "campaigns")
# The gateway's own configuration and credentials are never served, whatever the settings say.
NEVER_EXPOSED = frozenset(
    {"Fraxis Settings", "Fraxis User Profile", "Fraxis Sub Category", "Fraxis Refresh Token", "DocType", "User"}
)
DEFAULTS = {"access_token_ttl": 15 * 60, "refresh_token_ttl": 30 * 24 * 60 * 60, "page_size": 100, "max_page_size": 1000}

_MISSING = object()


def settings():
    """``Fraxis Settings`` for this request, or ``None`` while the DocType is not migrated."""
    doc = getattr(frappe.local, "fraxis_settings", _MISSING)
    if doc is _MISSING:
        try:
            doc = frappe.get_cached_doc(SETTINGS)
        except Exception:
            doc = None
        frappe.local.fraxis_settings = doc
    return doc


def reset_request_cache() -> None:
    frappe.local.fraxis_settings = _MISSING


def get_int(key: str) -> int:
    """Int setting; a Single that was never saved loads Int fields as 0."""
    doc = settings()
    value = doc.get(key) if doc else None
    return value if (value or 0) > 0 else DEFAULTS[key]


def enabled() -> bool:
    # "Disable" (not "Enable") so that an unsaved Single — Check fields load as 0 — is on.
    doc = settings()
    return not (doc and doc.disable_gateway)


def normalise_base_path(raw) -> tuple[str, str | None]:
    """``(path, error)``: ``/segment[/segment]`` without trailing slash, or an error message."""
    path = "/" + str(raw or "").strip().strip("/")
    if path == "/":
        return path, _("Base Path cannot be empty")
    if not re.fullmatch(r"(/[A-Za-z0-9._~-]+)+", path):
        return path, _("Base Path may only contain letters, digits, '.', '_', '~', '-' and '/'")
    if any(path == r or path.startswith(r + "/") for r in RESERVED_PREFIXES):
        return path, _("Base Path {0} collides with a Frappe route").format(path)
    return path, None


def base_path() -> str:
    """Configured URL prefix; an invalid stored value falls back to ``/fraxis``."""
    doc = settings()
    path, error = normalise_base_path(doc.base_path if doc else None)
    return DEFAULT_BASE_PATH if error else path


def jwt_secret() -> str | None:
    """Token signing secret, stored encrypted in ``Fraxis Settings`` and generated on first save."""
    doc = settings()
    return doc.get_password("jwt_secret", raise_exception=False) if doc else None


# --- exposure ----------------------------------------------------------------------------

def _doctypes(rows) -> set[str]:
    return {row.ref_doctype for row in rows or [] if row.ref_doctype}


def compute_exposed(doc) -> set[str]:
    """DocTypes the routes may serve.

    ``Only Expose`` wins outright; otherwise the DocTypes of ``Exposed Apps`` (every installed
    app but frappe when empty) plus ``Also Expose`` minus ``Never Expose``.
    """
    exposed = _doctypes(doc.only_expose) if doc else set()
    if not exposed:
        apps = [line.strip() for line in ((doc.exposed_apps if doc else None) or "").splitlines() if line.strip()]
        if not apps:
            apps = [app for app in frappe.get_installed_apps() if app != "frappe"]
        modules = frappe.get_all("Module Def", filters={"app_name": ["in", apps]}, pluck="name")
        exposed = set(
            frappe.get_all(
                "DocType",
                filters={"module": ["in", modules or [""]], "istable": 0, "issingle": 0, "is_virtual": 0},
                pluck="name",
            )
        )
        if doc:
            exposed |= _doctypes(doc.also_expose)
            exposed -= _doctypes(doc.never_expose)
    return exposed - NEVER_EXPOSED


def exposed_doctypes() -> set[str]:
    """Cached per site; cleared when Fraxis Settings is saved."""
    key = CACHE_PREFIX + "exposed"
    cached = frappe.cache.get_value(key)
    if cached is None:
        cached = sorted(compute_exposed(settings()))
        frappe.cache.set_value(key, cached, expires_in_sec=300)
    return set(cached)


def split_fieldnames(value) -> list[str]:
    """``"sender, assigned_by"`` (commas or new lines) -> ``["sender", "assigned_by"]``, without repeats."""
    names = [name.strip() for name in re.split(r"[,\n]", str(value or ""))]
    return list(dict.fromkeys(name for name in names if name))


def excluded_fields() -> dict[str, set[str]]:
    """DocType -> fieldnames the gateway never returns nor accepts (one settings row per DocType)."""
    out: dict[str, set[str]] = {}
    doc = settings()
    for row in (doc.excluded_fields if doc else None) or []:
        if row.ref_doctype:
            out.setdefault(row.ref_doctype, set()).update(split_fieldnames(row.get("fieldnames")))
    return out


@dataclass
class FieldMapping:
    public_name: str
    show_description: bool = True
    example: str = ""
    lookup: tuple[str, str, str] | None = None  # (DocType, match field, value field) clients see instead
    lookup_restrict: bool = True  # limit rows to values of the lookup records the client sees
    returned_in: str = "Lists and Records"  # | "Records Only" | "Never" (write-only)


def field_mappings() -> dict[str, dict[str, FieldMapping]]:
    """DocType -> fieldname -> how clients see it; fields without a row keep their name and description."""
    out: dict[str, dict[str, FieldMapping]] = {}
    doc = settings()
    for row in (doc.get("field_mappings") if doc else None) or []:
        if row.ref_doctype and row.fieldname:
            out.setdefault(row.ref_doctype, {})[row.fieldname] = FieldMapping(
                (row.public_name or "").strip() or row.fieldname,
                bool(row.show_description),
                (row.get("example") or "").strip(),
                (row.lookup_doctype, row.lookup_field, row.get("lookup_value") or "name")
                if row.get("lookup_doctype") and row.get("lookup_field")
                else None,
                row.get("lookup_restrict") is None or bool(row.lookup_restrict),  # unset: restrict
                row.get("returned_in") or "Lists and Records",
            )
    return out


DEFAULT_DOCS_LANGUAGES = ("en", "es")


def docs_languages() -> list[str]:
    """Languages the API docs offer; the first is the default."""
    doc = settings()
    codes = [line.strip() for line in ((doc.get("docs_languages") if doc else None) or "").splitlines() if line.strip()]
    return list(dict.fromkeys(codes)) or list(DEFAULT_DOCS_LANGUAGES)


# --- routes ------------------------------------------------------------------------------

# HTTP method -> (row field, default verb) of each operation the row publishes: GET answers the
# collection and one document, the other methods one operation each.
OPERATION_NAMES = {
    "GET": (("list_name", "List"), ("get_name", "Get one")),
    "POST": (("create_name", "Create"),),
    "PATCH": (("update_name", "Update"),),
    "DELETE": (("delete_name", "Delete"),),
}


def default_operation_names(http_method: str, public_name: str) -> dict[str, str]:
    return {fieldname: f"{verb} {public_name}" for fieldname, verb in OPERATION_NAMES.get(http_method, ())}


@dataclass
class RouteSpec:
    """Every row of ``Fraxis Settings > Routes`` sharing one ``/<sub_route>[/<sub_category>][/<path>]``."""

    sub_route: str
    sub_category: str
    doctype: str
    sub_path: str = ""  # the row's optional Path: extra segments after the Sub Category
    public_name: str = ""  # what the docs call the resource instead of the DocType
    # HTTP method -> {"description": str, "names": {row field: operation name}, "id_only": bool}
    verbs: dict[str, dict] = field(default_factory=dict)

    @property
    def segments(self) -> tuple[str, ...]:
        return tuple(filter(None, (self.sub_route, self.sub_category, *self.sub_path.split("/"))))

    @property
    def path(self) -> str:
        return "/" + "/".join(self.segments)


def normalise_sub_path(raw) -> tuple[str, str | None]:
    """``(path, error)`` for a route's optional Path: ``segment[/segment]`` without outer slashes."""
    path = str(raw or "").strip().strip("/")
    if path and not re.fullmatch(r"[A-Za-z0-9._~-]+(/[A-Za-z0-9._~-]+)*", path):
        return path, _("Path {0} may only contain letters, digits, '.', '_', '~', '-' and '/'").format(path)
    return path, None


def route_label(base: str, sub_route: str, sub_category: str, sub_path: str, http_method: str) -> str:
    """What the Route column shows: the collection path, or ``/{name}`` for item-only verbs."""
    extra = f"/{sub_path}" if sub_path else ""
    suffix = "/{name}" if http_method in ("PATCH", "DELETE") else ""
    category = f"/{sub_category}" if sub_category else ""
    return f"{base}/{sub_route}{category}{extra}{suffix}"


def routes() -> dict[tuple[str, ...], RouteSpec]:
    """Path segments -> RouteSpec, in the order of the settings table."""
    out: dict[tuple[str, ...], RouteSpec] = {}
    doc = settings()
    for row in (doc.routes if doc else None) or []:
        sub_path, error = normalise_sub_path(row.get("path"))
        if error or not (row.sub_route and row.ref_doctype):
            continue
        public_name = row.get("public_name") or row.ref_doctype
        spec = RouteSpec(row.sub_route, row.sub_category or "", row.ref_doctype, sub_path, public_name)
        spec = out.setdefault(spec.segments, spec)
        if spec.doctype == row.ref_doctype:
            verb = row.http_method or "GET"
            names = default_operation_names(verb, spec.public_name)
            names.update({f: row.get(f) for f in names if row.get(f)})
            spec.verbs[verb] = {
                "description": row.description or "",
                "names": names,
                # POST only: answer the new record's ID instead of the whole record.
                "id_only": verb == "POST" and row.get("create_response") == "ID Only",
            }
    return out


def ensure_settings() -> None:
    """``after_install`` / ``after_migrate``: persist defaults and generate the signing secret."""
    if not frappe.db.exists("DocType", SETTINGS):
        return
    doc = frappe.get_single(SETTINGS)
    if not doc.get_password("jwt_secret", raise_exception=False):
        doc.base_path = doc.base_path or DEFAULT_BASE_PATH
        doc.save(ignore_permissions=True)  # validate() fills the defaults and generates the secret
