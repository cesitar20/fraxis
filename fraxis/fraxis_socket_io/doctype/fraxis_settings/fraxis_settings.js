// Copyright (c) 2026, Picurit and contributors
// For license information, please see license.txt

// Live preview of the Route column; the server recomputes it on save (config.route_label).
function fraxis_route_label(frm, row) {
	const base = "/" + (frm.doc.base_path || "/fraxis").trim().replace(/^\/+|\/+$/g, "");
	if (!row.sub_route || !row.sub_category) return "";
	const path = (row.path || "").trim().replace(/^\/+|\/+$/g, "");
	const item = ["PATCH", "DELETE"].includes(row.http_method) ? "/{name}" : "";
	return `${base}/${row.sub_route}/${row.sub_category}${path ? "/" + path : ""}${item}`;
}

function fraxis_set_route(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	frappe.model.set_value(cdt, cdn, "route", fraxis_route_label(frm, row));
}

// API Names of a route row (server: config.OPERATION_NAMES / default_operation_names).
const FRAXIS_OPERATION_NAMES = {
	GET: [
		["list_name", "List"],
		["get_name", "Get one"],
	],
	POST: [["create_name", "Create"]],
	PATCH: [["update_name", "Update"]],
	DELETE: [["delete_name", "Delete"]],
};
const fraxis_last_public = {};

// Fill empty names, and names still carrying the previous default, from the Public Name.
function fraxis_set_names(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	const public_name = (row.public_name || "").trim() || row.ref_doctype;
	const previous = fraxis_last_public[cdn];
	fraxis_last_public[cdn] = public_name;
	if (!public_name) return;
	(FRAXIS_OPERATION_NAMES[row.http_method] || []).forEach(([fieldname, verb]) => {
		const current = row[fieldname];
		if (!current || (previous && current === `${verb} ${previous}`)) {
			frappe.model.set_value(cdt, cdn, fieldname, `${verb} ${public_name}`);
		}
	});
}

// Excluded Fields > Field lists what the row's DocType can return (server: model.publishable_fields).
const fraxis_fields_cache = {};

function fraxis_doctype_fields(doctype) {
	if (!fraxis_fields_cache[doctype]) {
		fraxis_fields_cache[doctype] = frappe
			.xcall("fraxis.fraxis_socket_io.doctype.fraxis_settings.fraxis_settings.get_doctype_fields", {
				doctype,
			})
			.then((fields) =>
				fields.map((f) => ({ value: f.fieldname, label: `${__(f.label)} (${f.fieldname})` }))
			);
	}
	return fraxis_fields_cache[doctype];
}

function fraxis_set_field_options(frm, cdn) {
	const row = locals["Fraxis Excluded Field"][cdn];
	const docfield = frappe.meta.get_docfield("Fraxis Excluded Field", "fieldname", cdn);
	if (!row || !docfield) return;
	if (!row.ref_doctype) {
		docfield.options = [];
		return;
	}
	fraxis_doctype_fields(row.ref_doctype).then((options) => {
		docfield.options = [{ value: "", label: "" }, ...options];
		const grid_row = frm.fields_dict.excluded_fields.grid.get_row(cdn);
		if (grid_row) grid_row.refresh_field("fieldname");
	});
}

frappe.ui.form.on("Fraxis Settings", {
	refresh(frm) {
		(frm.doc.excluded_fields || []).forEach((row) => fraxis_set_field_options(frm, row.name));
		(frm.doc.routes || []).forEach((row) => {
			fraxis_last_public[row.name] = (row.public_name || "").trim() || row.ref_doctype;
		});
	},
	base_path(frm) {
		(frm.doc.routes || []).forEach((row) => fraxis_set_route(frm, row.doctype, row.name));
	},
});

frappe.ui.form.on("Fraxis Route", {
	http_method(frm, cdt, cdn) {
		fraxis_set_route(frm, cdt, cdn);
		fraxis_set_names(frm, cdt, cdn);
	},
	sub_route: fraxis_set_route,
	sub_category: fraxis_set_route,
	path: fraxis_set_route,
	ref_doctype: fraxis_set_names,
	public_name: fraxis_set_names,
});

frappe.ui.form.on("Fraxis Excluded Field", {
	excluded_fields_add(frm, cdt, cdn) {
		fraxis_set_field_options(frm, cdn);
	},
	ref_doctype(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, "fieldname", "");
		fraxis_set_field_options(frm, cdn);
	},
	form_render(frm, cdt, cdn) {
		fraxis_set_field_options(frm, cdn);
	},
});
