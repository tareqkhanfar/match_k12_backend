app_name = "match_schools"
app_title = "Match Schools"
app_publisher = "Match Systems"
app_description = "School management customizations and APIs for K-12"
app_email = "tareqkhanfar29@gmail.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "match_schools",
# 		"logo": "/assets/match_schools/logo.png",
# 		"title": "Match Schools",
# 		"route": "/match_schools",
# 		"has_permission": "match_schools.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/match_schools/css/match_schools.css"
# app_include_js = "/assets/match_schools/js/match_schools.js"

# include js, css files in header of web template
# web_include_css = "/assets/match_schools/css/match_schools.css"
# web_include_js = "/assets/match_schools/js/match_schools.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "match_schools/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# سكربت شاشة فاتورة المبيعات: يملأ العميل من الطالب، ويصفّي التسجيلات،
# ويجلب بنود خطة الرسوم. القواعد الخادمية في `ms_billing.py` تبقى الحارس.
doctype_js = {"Sales Invoice": "public/js/sales_invoice.js"}

# بريد الطالب اختياري: رفع الإلزام وحده ينقل الخطأ إلى إنشاء الحساب،
# فيُتجاوز المتحكّم ليتخطّاه حين لا بريد. انظر `ms_student.py`.
override_doctype_class = {"Student": "match_schools.ms_student.MSStudent"}

# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "match_schools/public/icons.svg"

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

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# What the school print formats need that the document does not carry: the
# family behind a Customer, and the running balance. See `ms_print.py`.
#
# Listed as individual functions, not as the module path: Frappe exposes every
# function found in a hooked module, imports included, so a module path would
# put `flt` and friends into every template's namespace.
jinja = {
	"methods": [
		"match_schools.ms_print.ms_receipt_context",
		"match_schools.ms_print.ms_invoice_context",
	],
}

# Installation
# ------------

# before_install = "match_schools.install.before_install"
after_install = "match_schools.setup.install.after_install"
after_migrate = "match_schools.setup.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "match_schools.uninstall.before_uninstall"
# after_uninstall = "match_schools.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "match_schools.utils.before_app_install"
# after_app_install = "match_schools.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "match_schools.utils.before_app_uninstall"
# after_app_uninstall = "match_schools.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "match_schools.notifications.get_notification_config"

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

# Document Events
# ---------------
# Hook on document methods and events

# A school invoice must name the enrolment it belongs to, and must bill the
# student's own customer. Enforced as a document hook rather than inside an
# endpoint so it holds for the ERPNext desk and imports too — an accounting
# rule that only applies on one path is not a rule.
doc_events = {
	# Every MS record is stamped with the year and term it belongs to before
	# it is written. Hooked on "*" rather than on a list of doctypes because a
	# list is what rots: the next doctype someone adds is stamped too, without
	# them having to know this file exists.
	"*": {
		"before_insert": "match_schools.academic_stamp.stamp",
	},
	"Sales Invoice": {
		"validate": "match_schools.ms_billing.validate_student_invoice",
		# فاتورة رسوم معتمدة ترنّ هاتف الطالب ووليّ أمره.
		"on_submit": "match_schools.fee_notifications.on_invoice_submit",
	},
	"Payment Entry": {
		"on_submit": "match_schools.fee_notifications.on_payment_submit",
	},
	# Education builds `student_name` from three names and runs it in its own
	# validate. These hooks run afterwards, so rebuilding the name here is what
	# makes the fourth (grandfather's) name actually appear on the record.
	"Student": {
		"validate": "match_schools.api.students.set_full_name",
	},
	"Student Applicant": {
		"validate": "match_schools.api.students.set_full_name",
	},
}

# Scheduled Tasks
# ---------------

# Alert rules run themselves overnight, so a school does not depend on
# someone remembering to press a button.
scheduler_events = {
	# A message scheduled for 07:00 should not arrive at 07:59, so the queue
	# is checked every ten minutes rather than hourly.
	"cron": {
		"*/10 * * * *": [
			"match_schools.api.mail.deliver_due_messages",
			"match_schools.api.assignments.publish_due_assignments",
		],
	},
	"daily": [
		"match_schools.api.alerts.run_rules_scheduled",
		# Marks and results whose release date has arrived become visible to
		# families without anyone having to remember to publish them.
		"match_schools.api.grade_appeals.publish_due_marks",
	],
}

# scheduler_events = {
# 	"all": [
# 		"match_schools.tasks.all"
# 	],
# 	"daily": [
# 		"match_schools.tasks.daily"
# 	],
# 	"hourly": [
# 		"match_schools.tasks.hourly"
# 	],
# 	"weekly": [
# 		"match_schools.tasks.weekly"
# 	],
# 	"monthly": [
# 		"match_schools.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "match_schools.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "match_schools.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "match_schools.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "match_schools.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["match_schools.utils.before_request"]
# after_request = ["match_schools.utils.after_request"]

# Job Events
# ----------
# before_job = ["match_schools.utils.before_job"]
# after_job = ["match_schools.utils.after_job"]

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
# 	"match_schools.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

