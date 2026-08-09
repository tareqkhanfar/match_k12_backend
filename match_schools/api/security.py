# Copyright (c) 2026, Match Systems and contributors
# For license information, please see license.txt

"""Account security: sign-in history, active sessions, and session lifetime.

Everything here reads Frappe's own audit trail rather than keeping a second
one. `tabActivity Log` already records every login attempt with its IP and
outcome, and `tabSessions` is the live session table Frappe authenticates
against — so a session revoked here is genuinely dead, not just hidden.
"""

import frappe
from frappe.utils import cint, get_datetime, now_datetime

from match_schools.api.utils import (
	BACK_OFFICE,
	ms_endpoint,
)

# How much history a user is shown. Enough to spot "someone signed in from a
# city I've never been to" without turning the screen into a log viewer.
HISTORY_LIMIT = 20


def _device_from_agent(agent: str) -> dict:
	"""A readable device/browser label from a User-Agent string.

	Deliberately coarse. The goal is for a parent to recognise "my phone" or
	notice "Windows — that isn't me", not to fingerprint anyone.
	"""
	ua = (agent or "").lower()

	if "android" in ua:
		os_name, os_ar = "Android", "أندرويد"
	elif any(x in ua for x in ("iphone", "ipad", "ios")):
		os_name, os_ar = "iOS", "آيفون / آيباد"
	elif "windows" in ua:
		os_name, os_ar = "Windows", "ويندوز"
	elif "mac os" in ua or "macintosh" in ua:
		os_name, os_ar = "macOS", "ماك"
	elif "linux" in ua:
		os_name, os_ar = "Linux", "لينكس"
	else:
		os_name, os_ar = "", ""

	# Order matters: Edge and Chrome both contain "chrome"/"safari".
	if "edg/" in ua or "edge" in ua:
		browser = "Edge"
	elif "opr/" in ua or "opera" in ua:
		browser = "Opera"
	elif "chrome" in ua or "crios" in ua:
		browser = "Chrome"
	elif "firefox" in ua or "fxios" in ua:
		browser = "Firefox"
	elif "safari" in ua:
		browser = "Safari"
	else:
		browser = ""

	is_mobile = any(x in ua for x in ("mobile", "android", "iphone", "ipad"))

	label_en = " · ".join([p for p in (browser, os_name) if p]) or "Unknown device"
	label_ar = " · ".join([p for p in (browser, os_ar) if p]) or "جهاز غير معروف"

	return {
		"browser": browser,
		"os": os_name,
		"isMobile": is_mobile,
		"label": label_en,
		"labelAr": label_ar,
	}


def _session_expiry_seconds() -> int:
	"""System Settings stores this as "HH:MM" (or "HH:MM:SS")."""
	raw = frappe.db.get_single_value("System Settings", "session_expiry") or "24:00"
	parts = str(raw).split(":")
	try:
		hours = cint(parts[0])
		minutes = cint(parts[1]) if len(parts) > 1 else 0
	except Exception:
		hours, minutes = 24, 0
	seconds = hours * 3600 + minutes * 60
	# A zero/garbage setting would expire every session instantly.
	return seconds or 24 * 3600


@frappe.whitelist()
@ms_endpoint()
def sign_in_history(limit: int = HISTORY_LIMIT, persona: str = None):
	"""This user's recent sign-in attempts, most recent first.

	Failed attempts are included on purpose: a run of failures the user does
	not recognise is the clearest early signal that someone is guessing at
	their password.
	"""
	limit = min(max(cint(limit) or HISTORY_LIMIT, 1), 50)

	rows = frappe.get_all(
		"Activity Log",
		filters={"user": frappe.session.user, "operation": ["in", ["Login", "Logout"]]},
		fields=["name", "operation", "status", "ip_address", "creation"],
		order_by="creation desc",
		limit=limit,
	)

	history = []
	for r in rows:
		success = (r.status or "").lower() in ("success", "successful")
		history.append(
			{
				"id": r.name,
				"operation": r.operation,
				"success": success,
				"status": r.status,
				"ip": r.ip_address or "",
				"at": str(r.creation),
			}
		)

	failed_recent = sum(1 for h in history if h["operation"] == "Login" and not h["success"])
	last_success = next(
		(h for h in history if h["operation"] == "Login" and h["success"]), None
	)

	return {
		"history": history,
		# The previous successful sign-in — "last time you were here" — which
		# is the one worth showing, not the session happening right now.
		"lastSignIn": (
			[h for h in history if h["operation"] == "Login" and h["success"]][1]
			if len([h for h in history if h["operation"] == "Login" and h["success"]]) > 1
			else None
		),
		"currentSignIn": last_success,
		"failedAttempts": failed_recent,
	}


@frappe.whitelist()
@ms_endpoint()
def active_sessions(persona: str = None):
	"""Every device currently signed in as this user."""
	current_sid = frappe.session.sid
	rows = frappe.db.sql(
		"""
		SELECT sid, ipaddress, lastupdate, sessiondata
		  FROM tabSessions
		 WHERE user = %(user)s
		 ORDER BY lastupdate DESC
		""",
		{"user": frappe.session.user},
		as_dict=True,
	)

	sessions = []
	for r in rows:
		agent = ""
		session_ip = ""
		# The `ipaddress` column is frequently NULL, while the session blob
		# carries `session_ip` — so the blob is the reliable source and the
		# column is only a fallback.
		try:
			data = frappe.parse_json(r.sessiondata) if r.sessiondata else {}
			if isinstance(data, dict):
				agent = data.get("user_agent") or ""
				session_ip = data.get("session_ip") or ""
		except Exception:
			pass

		sessions.append(
			{
				# Never expose a full session id — it is a bearer credential.
				# The last 6 characters are enough to tell two rows apart.
				"ref": (r.sid or "")[-6:],
				"isCurrent": r.sid == current_sid,
				"ip": session_ip or r.ipaddress or "",
				"lastActive": str(r.lastupdate) if r.lastupdate else None,
				"device": _device_from_agent(agent),
			}
		)

	return {
		"sessions": sessions,
		"total": len(sessions),
		"expirySeconds": _session_expiry_seconds(),
	}


@frappe.whitelist(methods=["POST"])
@ms_endpoint()
def revoke_other_sessions(persona: str = None):
	"""Sign out every device except the one making this request.

	The remedy offered next to the history: if a user sees a sign-in they do
	not recognise, this ends it immediately.
	"""
	current_sid = frappe.session.sid

	# Guard against signing the caller out of their own device. Outside a real
	# HTTP request `frappe.session.sid` is a placeholder such as "Administrator"
	# that matches no row, and "delete everything except <no row>" would revoke
	# the current session too.
	is_real_session = bool(current_sid) and frappe.db.sql(
		"""SELECT 1 FROM tabSessions WHERE sid = %(sid)s LIMIT 1""",
		{"sid": current_sid},
	)
	if not is_real_session:
		frappe.throw(
			frappe._("This action must be performed from an active browser session."),
			frappe.ValidationError,
		)

	count = frappe.db.sql(
		"""SELECT COUNT(*) FROM tabSessions WHERE user = %(user)s AND sid != %(sid)s""",
		{"user": frappe.session.user, "sid": current_sid},
	)[0][0]

	frappe.db.sql(
		"""DELETE FROM tabSessions WHERE user = %(user)s AND sid != %(sid)s""",
		{"user": frappe.session.user, "sid": current_sid},
	)
	frappe.cache.hdel("session", frappe.session.user)
	frappe.db.commit()

	return {
		"revoked": cint(count),
		"message_ar": f"تم إنهاء {cint(count)} جلسة أخرى.",
	}


@frappe.whitelist()
@ms_endpoint()
def session_status(persona: str = None):
	"""What the header needs: server time and how long this session has left.

	The countdown is computed from the server clock. A device with a wrong
	clock would otherwise show a wrong — and alarming — expiry.
	"""
	expiry = _session_expiry_seconds()
	now = now_datetime()

	# Raw SQL on purpose: `tabSessions` has no `creation` column, and
	# frappe.db.get_value() appends "ORDER BY creation", which errors out.
	row = frappe.db.sql(
		"""SELECT lastupdate FROM tabSessions WHERE sid = %(sid)s LIMIT 1""",
		{"sid": frappe.session.sid},
		as_dict=True,
	)
	last_update = row[0].lastupdate if row else None

	remaining = expiry
	if last_update:
		elapsed = (now - get_datetime(last_update)).total_seconds()
		remaining = max(int(expiry - elapsed), 0)

	return {
		"serverTime": str(now),
		"expirySeconds": expiry,
		"remainingSeconds": remaining,
		"lastActivity": str(last_update) if last_update else None,
	}


@frappe.whitelist()
@ms_endpoint(*BACK_OFFICE)
def failed_login_report(days: int = 7, limit: int = 100, persona: str = None):
	"""School-wide failed sign-ins — the admin's view of attacks in progress.

	Grouped by account and IP so a spray against many accounts from one address
	is as visible as repeated guesses at a single account.
	"""
	days = min(max(cint(days) or 7, 1), 90)
	limit = min(max(cint(limit) or 100, 1), 500)

	rows = frappe.db.sql(
		"""
		SELECT user, ip_address, COUNT(*) AS attempts, MAX(creation) AS last_attempt
		  FROM `tabActivity Log`
		 WHERE operation = 'Login'
		   AND status NOT IN ('Success', 'Successful')
		   AND creation >= DATE_SUB(NOW(), INTERVAL %(days)s DAY)
		 GROUP BY user, ip_address
		 ORDER BY attempts DESC, last_attempt DESC
		 LIMIT %(limit)s
		""",
		{"days": days, "limit": limit},
		as_dict=True,
	)

	entries = [
		{
			"user": r.user,
			"ip": r.ip_address or "",
			"attempts": cint(r.attempts),
			"lastAttempt": str(r.last_attempt),
		}
		for r in rows
	]

	return {
		"entries": entries,
		"totalAttempts": sum(e["attempts"] for e in entries),
		"distinctAccounts": len({e["user"] for e in entries}),
		"distinctIps": len({e["ip"] for e in entries if e["ip"]}),
		"days": days,
	}
