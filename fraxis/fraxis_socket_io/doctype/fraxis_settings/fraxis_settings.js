// Copyright (c) 2026, Picurit and contributors
// For license information, please see license.txt

// Live preview of the Route column; the server recomputes it on save (config.route_label).
function fraxis_route_label(frm, row) {
	const base = "/" + (frm.doc.base_path || "/fraxis").trim().replace(/^\/+|\/+$/g, "");
	if (!row.sub_route || !row.sub_category) return "";
	const item = ["PATCH", "DELETE"].includes(row.http_method) ? "/{name}" : "";
	return `${base}/${row.sub_route}/${row.sub_category}${item}`;
}

function fraxis_set_route(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	frappe.model.set_value(cdt, cdn, "route", fraxis_route_label(frm, row));
}

frappe.ui.form.on("Fraxis Settings", {
	base_path(frm) {
		(frm.doc.routes || []).forEach((row) => fraxis_set_route(frm, row.doctype, row.name));
	},
});

frappe.ui.form.on("Fraxis Route", {
	http_method: fraxis_set_route,
	sub_route: fraxis_set_route,
	sub_category: fraxis_set_route,
});
