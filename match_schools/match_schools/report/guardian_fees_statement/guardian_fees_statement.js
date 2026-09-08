// Copyright (c) 2026, Match Systems and contributors
// For license information, please see license.txt

frappe.query_reports["Guardian Fees Statement"] = {
	filters: [
		{
			fieldname: "guardian",
			label: __("Guardian"),
			fieldtype: "Link",
			options: "Guardian",
			reqd: 1,
		},
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "academic_year",
			label: __("Academic Year"),
			fieldtype: "Link",
			options: "Academic Year",
		},
		{
			fieldname: "academic_term",
			label: __("Academic Term"),
			fieldtype: "Link",
			options: "Academic Term",
		},
		{
			fieldname: "program",
			label: __("Program"),
			fieldtype: "Link",
			options: "Program",
		},
		{
			fieldname: "from_date",
			label: __("From Posting Date"),
			fieldtype: "Date",
		},
		{
			fieldname: "to_date",
			label: __("To Posting Date"),
			fieldtype: "Date",
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: "\nAll\nFully Paid\nPartially Paid\nUnpaid",
			default: "All",
		},
	],

	// Rows carry `indent` (0=student, 1=year, 2=term, 3=invoice) so the report
	// view nests the Student -> Year -> Term -> Invoice hierarchy.
	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (data && data.is_group) {
			let bg = "#eef2ff";
			if (data.group_level === "year") bg = "#f0f9ff";
			if (data.group_level === "term") bg = "#f7fee7";
			return `<span style="font-weight:600; background-color:${bg};">${value}</span>`;
		}

		if (data && data.is_subtotal) {
			let bg = "#f3f4f6";
			let weight = 600;
			if (data.subtotal_kind === "grand") {
				bg = "#e5e7eb";
				weight = 700;
			} else if (data.subtotal_kind === "student") {
				bg = "#eaeaf5";
			}
			return `<span style="font-weight:${weight}; background-color:${bg};">${value}</span>`;
		}

		if (column.fieldname === "outstanding_amount" && data) {
			const color = data.outstanding_amount > 0 ? "#dc3545" : "#28a745";
			value = `<span style="color:${color}; white-space:nowrap;">${value}</span>`;
		}
		if (column.fieldname === "paid_amount" && data && data.paid_amount > 0) {
			value = `<span style="color:#28a745; white-space:nowrap;">${value}</span>`;
		}

		if (column.fieldname === "status" && data && data.status) {
			let color = "#6c757d",
				bg = "#e9ecef";
			if (data.status === "Fully Paid") {
				color = "#0f5132";
				bg = "#d1e7dd";
			} else if (data.status === "Partially Paid") {
				color = "#664d03";
				bg = "#fff3cd";
			} else if (data.status === "Unpaid") {
				color = "#842029";
				bg = "#f8d7da";
			}
			value = `<span style="color:${color}; background-color:${bg}; padding:2px 8px; border-radius:10px; white-space:nowrap;">${__(
				data.status
			)}</span>`;
		}

		return value;
	},
};
