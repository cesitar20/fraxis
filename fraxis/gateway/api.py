# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Gateway endpoints, reached through ``router.route_request``
(``<base_path>/auth/token`` -> ``/api/v2/method/fraxis.gateway.api.token``).

They are ``allow_guest`` because the gateway authenticates by itself: ``dispatch`` serves a
route only when ``auth.authenticate`` accepted its Bearer access token for this request, and
answers nothing when called at its raw ``/api`` path.
"""

import json

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import escape_html
from werkzeug.wrappers import Response

from fraxis.gateway import auth, config, documents, openapi, router
from fraxis.gateway.odata import serialize

ALL_VERBS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


@frappe.whitelist(allow_guest=True, methods=ALL_VERBS)
def dispatch():
    route = router.current()
    if not route or route.kind not in ("data", "error"):
        return serialize.error_response(404, "NotFound", _("Not found"))
    if route.kind == "error":
        if route.status == 405:
            return serialize.error_response(
                405,
                "MethodNotAllowed",
                _("{0} is not allowed on this route").format(frappe.request.method),
                {"Allow": ", ".join(route.allowed)},
            )
        return serialize.error_response(404, "NotFound", _("No route {0}").format(router.original_path()))

    if not getattr(frappe.local, "fraxis_user", None):
        error = getattr(frappe.local, "fraxis_auth_error", None) or auth.AuthError(_("Authentication required"))
        return serialize.error_response(
            error.status,
            "Unauthorized" if error.status == 401 else "Forbidden",
            str(error),
            {"WWW-Authenticate": 'Bearer realm="api", error="invalid_token"'} if error.status == 401 else None,
        )

    try:
        return documents.run(route)
    except Exception as e:
        # Frappe commits after a POST/PATCH/DELETE that returns normally: undo partial writes.
        frappe.db.rollback()
        return serialize.exception_response(e)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=30, seconds=60)
def token(
    api_key: str | None = None,
    api_secret: str | None = None,
    grant_type: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    refresh_token: str | None = None,
):
    """API Key + API Secret -> tokens (also as OAuth2 client credentials, and
    ``grant_type=refresh_token`` so OAuth2 clients such as Scalar can renew at the token URL)."""
    if router.current() is None:
        return serialize.error_response(404, "NotFound", _("Not found"))
    if grant_type == "refresh_token":
        return _token_response(auth.refresh_token, refresh_token)
    if grant_type and grant_type != "client_credentials":
        return _oauth_error("unsupported_grant_type", _("grant_type must be client_credentials or refresh_token"), 400)

    basic = auth.basic_credentials()
    key = api_key or client_id or (basic[0] if basic else None)
    secret = api_secret or client_secret or (basic[1] if basic else None)
    return _token_response(auth.issue_token, key, secret)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=60, seconds=60)
def refresh(refresh_token: str | None = None):
    """Refresh token -> new access + refresh token; the one sent stops working."""
    if router.current() is None:
        return serialize.error_response(404, "NotFound", _("Not found"))
    return _token_response(auth.refresh_token, refresh_token)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=60, seconds=60)
def revoke(token: str | None = None, refresh_token: str | None = None, token_type_hint: str | None = None):
    """End the session of a refresh or access token (RFC 7009: 200 even for unknown tokens)."""
    if router.current() is None:
        return serialize.error_response(404, "NotFound", _("Not found"))
    auth.revoke(token or refresh_token)
    return serialize.json_response({"revoked": True}, headers=NO_STORE)


def _token_response(issue, *args) -> Response:
    try:
        return serialize.json_response(issue(*args), headers=NO_STORE)
    except auth.AuthError as e:
        return _oauth_error(e.code, str(e), e.status)


def _oauth_error(code: str, description: str, status: int) -> Response:
    """RFC 6749 §5.2 error body."""
    return serialize.json_response({"error": code, "error_description": description}, status, NO_STORE)


# --- documentation ---------------------------------------------------------------------------

def _docs_language(lang: str | None) -> str:
    """The requested docs language when offered in Fraxis Settings, else the first one offered."""
    languages = config.docs_languages()
    return lang if lang in languages else languages[0]


# The docs and their document are public: clients read them without any login of the platform.
@frappe.whitelist(allow_guest=True, methods=["GET"])
def openapi_spec(lang: str | None = None):
    if router.current() is None:
        return serialize.error_response(404, "NotFound", _("Not found"))
    lang = _docs_language(lang)
    cache_key = f"{config.CACHE_PREFIX}openapi:{router.gateway_url()}:{lang}"
    spec = frappe.cache.get_value(cache_key)
    if spec is None:
        previous, frappe.local.lang = frappe.local.lang, lang  # every _() of the document
        try:
            spec = openapi.build()
        finally:
            frappe.local.lang = previous
        frappe.cache.set_value(cache_key, spec, expires_in_sec=300)
    return serialize.json_response(spec)


# Every Scalar client except Shell/curl: only raw HTTP examples, no client library is supported.
SCALAR_HIDDEN_CLIENTS = {
    "c": ["libcurl"], "clojure": ["clj_http"], "csharp": ["httpclient", "restsharp"], "dart": ["http"],
    "fsharp": ["httpclient"], "go": ["native"], "http": ["http1.1"],
    "java": ["asynchttp", "nethttp", "okhttp", "unirest"], "js": ["axios", "fetch", "jquery", "ofetch", "xhr"],
    "julia": ["http"], "kotlin": ["okhttp"], "node": ["axios", "fetch", "ofetch", "undici"],
    "objc": ["nsurlsession"], "ocaml": ["cohttp"], "php": ["curl", "guzzle", "laravel"],
    "powershell": ["restmethod", "webrequest"],
    "python": ["aiohttp", "httpx_async", "httpx_sync", "python3", "requests"], "r": ["httr2"],
    "ruby": ["native"], "rust": ["reqwest"], "shell": ["httpie", "wget"], "swift": ["nsurlsession"],
}

# "Try it" calls authenticate with the access token only. Sending the desk ``sid`` cookie would
# make Frappe enforce CSRF on POST/PATCH/DELETE before the gateway runs, so the page sends every
# gateway request except the spec itself without cookies. (Kept here: the served page never
# names the platform.)
_PAGE = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
#docs-language {{ position: fixed; top: 12px; right: 16px; z-index: 1000; font: 13px system-ui, sans-serif;
  padding: 4px 8px; border-radius: 6px; border: 1px solid #c9ccd1; background: #fff; color: #1f2328; }}
</style>
<script>
(function () {{
  const base = {base_json}, spec = base + "/openapi.json";
  const nativeFetch = window.fetch;
  window.fetch = function (input, init) {{
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (url.origin === location.origin && url.pathname.startsWith(base + "/") && url.pathname !== spec) {{
      if (input instanceof Request) input = new Request(input, {{ credentials: "omit" }});
      else init = Object.assign({{}}, init, {{ credentials: "omit" }});
    }}
    return nativeFetch.call(this, input, init);
  }};
}})();
</script>
</head>
<body style="margin:0">
{translate_ui}
{language_picker}
<div id="app"></div>
<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference@1"></script>
<script>Scalar.createApiReference("#app", {config_json})</script>
</body>
</html>"""

# Scalar's own labels have no translations: the page swaps these exact texts for the translated
# ones (Frappe's _(), fraxis/translations), leaving code blocks alone. Prefixes keep their tail.
SCALAR_UI = (
    "Authentication", "Auth Required", "Required", "required", "Bearer Token", "Body", "Responses", "Response",
    "Request", "Test Request", "Show Schema", "Hide Schema", "Server", "Server:", "Client Libraries",
    "Download OpenAPI Document", "Operations", "Query Parameters", "Path Parameters", "Headers", "Copy",
    "Copy as Markdown", "Search", "Models", "Introduction", "Show Password", "Clear Value", "Open Search",
    "Open Menu", "Status:", "Type:", "Format:", "Selected Content Type:", "More", "Show More", "Show Less",
    "Select from all clients", "Keyboard Shortcut:", "Credentials", "Example", "Examples", "Default", "Send",
    "Close", "Cancel", "Value", "Key",
)
SCALAR_UI_PREFIXES = ("Selected Auth Type: ", "Close Group - ", "Open Group - ", "Copy link to ", "Request Example for ")

_TRANSLATE_UI = """<script>
(function () {{
  const UI = {ui_json}, PREFIXES = {prefixes_json};
  if (!Object.keys(UI).length && !Object.keys(PREFIXES).length) return;
  // Only a label that is the whole visible text of its element: "Request" inside "TokenRequest"
  // stays. Screen-reader-only prefixes do not count ("Agent" + "Operations" is still a label).
  const visible = (el) => {{
    let text = el.textContent;
    el.querySelectorAll(".screenreader-only").forEach((s) => (text = text.replace(s.textContent, "")));
    return text.trim();
  }};
  const skip = (node, key) => !node.parentElement || node.parentElement.closest("pre, code, textarea, script, style")
    || visible(node.parentElement) !== key;
  function translate(node) {{
    const text = node.nodeValue, key = text.trim();
    if (!key || skip(node, key)) return;
    let out = UI[key];
    if (!out) for (const prefix in PREFIXES) if (key.startsWith(prefix)) {{ out = PREFIXES[prefix] + key.slice(prefix.length); break; }}
    if (out && out !== key) node.nodeValue = text.replace(key, out);
  }}
  function walk(root) {{
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) translate(node);
  }}
  new MutationObserver((mutations) => {{
    for (const m of mutations) {{
      if (m.type === "characterData") translate(m.target);
      else m.addedNodes.forEach((n) => (n.nodeType === 3 ? translate(n) : n.nodeType === 1 && walk(n)));
    }}
  }}).observe(document.documentElement, {{ childList: true, subtree: true, characterData: true }});
}})();
</script>"""


def _ui_translations() -> str:
    """The Scalar label translator for the current language, or nothing when none is translated."""
    ui = {text: _(text) for text in SCALAR_UI if _(text) != text}
    prefixes = {p: _(p.strip()) + p[len(p.rstrip()):] for p in SCALAR_UI_PREFIXES if _(p.strip()) != p.strip()}
    if not ui and not prefixes:
        return ""
    return _TRANSLATE_UI.format(ui_json=_json_for_script(ui), prefixes_json=_json_for_script(prefixes))


# Hidden from the page: the button that opens Scalar's hosted client and the "Powered by" link.
SCALAR_CSS = ".open-api-client-button, a[href*='utm_source=powered-by'] { display: none !important; }"


def _language_picker(current: str) -> str:
    """A select that reloads the docs in another language (only when more than one is offered)."""
    languages = config.docs_languages()
    if len(languages) < 2:
        return ""
    names = dict(frappe.get_all("Language", filters={"name": ["in", languages]}, fields=["name", "language_name"],
                                as_list=True))
    options = "".join(
        f'<option value="{escape_html(code)}"{" selected" if code == current else ""}>'
        f"{escape_html(names.get(code) or code)}</option>"
        for code in languages
    )
    return (
        f'<select id="docs-language" aria-label="{escape_html(_("Language"))}" '
        'onchange="location.search = \'?lang=\' + encodeURIComponent(this.value)">'
        f"{options}</select>"
    )


def _json_for_script(value) -> str:
    """JSON safe inside <script>: no "</script>" can close it early."""
    return json.dumps(value).replace("<", "\\u003c")


@frappe.whitelist(allow_guest=True, methods=["GET"])
def docs(lang: str | None = None):
    if router.current() is None:
        return serialize.error_response(404, "NotFound", _("Not found"))
    lang = _docs_language(lang)
    scalar_config = {
        "url": router.gateway_path("/openapi.json") + f"?lang={lang}",
        "withDefaultFonts": False,
        "persistAuth": True,
        "hideClientButton": True,
        "customCss": SCALAR_CSS,
        # Generated requests show "Authorization: Bearer <access_token>", as the guide does.
        "authentication": {
            "preferredSecurityScheme": openapi.SECURITY_SCHEME,
            "securitySchemes": {openapi.SECURITY_SCHEME: {"token": "<access_token>"}},
        },
        "hiddenClients": SCALAR_HIDDEN_CLIENTS,
    }
    previous, frappe.local.lang = frappe.local.lang, lang
    try:
        page = _PAGE.format(
            lang=escape_html(lang),
            title=escape_html(_(openapi.DOCS_TITLE)),
            base_json=_json_for_script(router.gateway_path()),
            language_picker=_language_picker(lang),
            translate_ui=_ui_translations(),
            config_json=_json_for_script(scalar_config),
        )
    finally:
        frappe.local.lang = previous
    return Response(page, mimetype="text/html")
