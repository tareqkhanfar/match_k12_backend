// Copyright (c) 2026, Match Systems and contributors
// For license information, please see license.txt

frappe.query_reports["Student Fee Collection Advanced"] = {
	filters: [
		{
			fieldname: "group_by",
			label: __("Group By"),
			fieldtype: "Select",
			options: [
				"Student",
				"Program",
				"Academic Year",
				"Academic Term",
				"Student Batch",
				"Student Category",
				"Fee Schedule",
			],
			default: "Student",
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
			fieldname: "student",
			label: __("Student"),
			fieldtype: "Link",
			options: "Student",
		},
		{
			fieldname: "student_batch_name",
			label: __("Student Batch"),
			fieldtype: "Link",
			options: "Student Batch Name",
		},
		{
			fieldname: "student_category",
			label: __("Student Category"),
			fieldtype: "Link",
			options: "Student Category",
		},
		{
			fieldname: "fee_schedule",
			label: __("Fee Schedule"),
			fieldtype: "Link",
			options: "Fee Schedule",
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
		{
			fieldname: "only_with_outstanding",
			label: __("Only Rows With Outstanding"),
			fieldtype: "Check",
			default: 0,
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (data && data.is_total) {
			return `<span style="font-weight:700; background-color:#e5e7eb;">${value}</span>`;
		}

		if (column.fieldname === "outstanding_amount" && data) {
			const color = data.outstanding_amount > 0 ? "#dc3545" : "#28a745";
			value = `<span style="color:${color}; font-weight:600; white-space:nowrap;">${value}</span>`;
		}
		if (column.fieldname === "paid_amount" && data && data.paid_amount > 0) {
			value = `<span style="color:#28a745; white-space:nowrap;">${value}</span>`;
		}

		// A collection rate is the number the finance office actually reads —
		// green above 90, amber above 60, red below.
		if (column.fieldname === "collection_rate" && data) {
			const rate = data.collection_rate || 0;
			const color = rate >= 90 ? "#15803d" : rate >= 60 ? "#b45309" : "#dc3545";
			value = `<span style="color:${color}; font-weight:600;">${value}</span>`;
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
