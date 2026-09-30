# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

app_name = "fraxis"
app_title = "Fraxis Socket IO"
app_publisher = "Picurit"
app_description = "Bidirectional axis for full CRUD Frappe-Database operations trough Socket IO"
app_email = "dev@picurit.com"
app_license = "mpl-2.0"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "fraxis",
# 		"logo": "/assets/fraxis/logo.png",
# 		"title": "Fraxis Socket IO",
# 		"route": "/fraxis",
# 		"has_permission": "fraxis.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/fraxis/css/fraxis.css"
# app_include_js = "/assets/fraxis/js/fraxis.js"

# include js, css files in header of web template
# web_include_css = "/assets/fraxis/css/fraxis.css"
# web_include_js = "/assets/fraxis/js/fraxis.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "fraxis/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "fraxis/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "fraxis.utils.jinja_methods",
# 	"filters": "fraxis.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "fraxis.install.before_install"
# after_install = "fraxis.install.after_install"
# REST gateway: persist Fraxis Settings defaults and generate the token signing secret.
after_install = "fraxis.gateway.config.ensure_settings"
after_migrate = ["fraxis.gateway.config.ensure_settings"]

# Uninstallation
# ------------

# before_uninstall = "fraxis.uninstall.before_uninstall"
# after_uninstall = "fraxis.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "fraxis.utils.before_app_install"
# after_app_install = "fraxis.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "fraxis.utils.before_app_uninstall"
# after_app_uninstall = "fraxis.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "fraxis.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# Generic document-event bridge: every ORM insert/update/delete is offered to the bridge,
# whose first action is the subscription gate — unsubscribed doctypes do zero work
# (fraxis_improvements_plan.md §A.3). Uses the "*" wildcard so no per-doctype registration
# is needed; the bridge stays decoupled from any consumer app.
doc_events = {
	"*": {
		"after_insert": "fraxis.runtime.doc_event_bridge.on_after_insert",
		"on_update": "fraxis.runtime.doc_event_bridge.on_update",
		"on_trash": "fraxis.runtime.doc_event_bridge.on_trash",
	},
	# REST gateway: User is only observed; its gateway data lives in Fraxis User Profile.
	"User": {
		"on_trash": "fraxis.gateway.user.delete_profile",
	},
}

# Scheduled Tasks
# ---------------

# REST gateway: drop refresh tokens that expired more than a week ago.
scheduler_events = {
	"daily": ["fraxis.gateway.tokens.purge_expired"],
}

# scheduler_events = {
# 	"all": [
# 		"fraxis.tasks.all"
# 	],
# 	"daily": [
# 		"fraxis.tasks.daily"
# 	],
# 	"hourly": [
# 		"fraxis.tasks.hourly"
# 	],
# 	"weekly": [
# 		"fraxis.tasks.weekly"
# 	],
# 	"monthly": [
# 		"fraxis.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "fraxis.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "fraxis.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "fraxis.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["fraxis.utils.before_request"]
# after_request = ["fraxis.utils.after_request"]

# REST gateway: <base_path>/... is resolved and rewritten onto fraxis.gateway.api (no-op elsewhere).
before_request = ["fraxis.gateway.router.route_request"]

# Job Events
# ----------
# Fraxis does NOT register a bench-global after_job hook. Job completion is reported via
# per-job RQ on_success/on_failure callbacks attached at enqueue time
# (fraxis.utils.job_hooks.on_job_success / on_job_failure), scoped to the dedicated
# `fraxis` queue only — no fan-out across every job in the bench (analysis §5.3b, S3-4).

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"fraxis.auth.validate"
# ]

# REST gateway: Bearer access token on gateway routes only.
auth_hooks = ["fraxis.gateway.auth.authenticate"]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

