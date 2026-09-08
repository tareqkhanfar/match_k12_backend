"""إرسال إشعارات الدفع عبر Firebase Cloud Messaging (HTTP v1).

الإعداد المطلوب في `site_config.json`:

	"fcm_service_account": "/home/frappe/frappe-bench/sites/<site>/fcm.json"

وهو **مفتاح حساب خدمة** يُنزَّل من Firebase Console →
Project settings → Service accounts → Generate new private key.
ملف `google-services.json` الخاص بالتطبيق لا يصلح هنا: إنه إعداد العميل
ولا يحمل أي صلاحية إرسال.

إن لم يُضبط المفتاح، تبقى الإشعارات داخل التطبيق فقط (الاستطلاع الدوري)
ولا يُرفع أي خطأ — نسجّل تنبيهاً مرّة واحدة لكل طلب ونمضي.
"""

from __future__ import annotations

import json
import logging

import frappe

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_ENDPOINT = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"

# ترسل FCM رسالة واحدة لكل جهاز؛ نحدّ العدد حتى لا يعلق طلب المستخدم.
MAX_TOKENS_PER_CALL = 500

# أخطاء تعني أن الرمز لم يعد صالحاً — نعطّل الجهاز بدل إعادة المحاولة للأبد.
DEAD_TOKEN_ERRORS = {"UNREGISTERED", "INVALID_ARGUMENT", "NOT_FOUND"}

# كم فشلاً متتالياً نحتمله قبل تعطيل الجهاز.
MAX_FAILURES = 5


def _logger():
	logger = frappe.logger("ms_push", allow_site=True, max_size=2_000_000)
	if logger.level > logging.INFO:
		logger.setLevel(logging.INFO)
	return logger


def _credentials():
	"""بيانات اعتماد حساب الخدمة، أو None إن لم تُضبط."""
	path = frappe.conf.get("fcm_service_account")
	if not path:
		return None, None
	try:
		from google.oauth2 import service_account

		with open(path, encoding="utf-8") as fh:
			info = json.load(fh)
		creds = service_account.Credentials.from_service_account_info(info, scopes=[FCM_SCOPE])
		return creds, info.get("project_id")
	except Exception:
		_logger().warning(f"fcm: تعذّرت قراءة مفتاح الخدمة من {path}")
		return None, None


def _access_token(creds) -> str | None:
	try:
		from google.auth.transport.requests import Request

		creds.refresh(Request())
		return creds.token
	except Exception:
		_logger().warning("fcm: فشل تجديد رمز الوصول")
		return None


def is_configured() -> bool:
	return bool(frappe.conf.get("fcm_service_account"))


def send_to_users(users, title: str, body: str, data: dict | None = None) -> dict:
	"""إرسال إشعار إلى كل أجهزة مجموعة مستخدمين.

	يعيد إحصاء `{sent, failed, skipped}`. لا يرفع استثناءً أبداً: الإشعار
	مكمّل للواجهة لا بديل عنها، وفشل الإرسال يجب ألّا يُسقط العملية التي
	تسبّبت به (نشر إعلان، إرسال رسالة…).
	"""
	users = [u for u in dict.fromkeys(users or []) if u]
	if not users:
		return {"sent": 0, "failed": 0, "skipped": 0}

	from match_schools.api.notifications import tokens_for

	tokens = tokens_for(users)
	if not tokens:
		return {"sent": 0, "failed": 0, "skipped": 0}

	return send_to_tokens(tokens, title, body, data)


def send_to_tokens(tokens, title: str, body: str, data: dict | None = None) -> dict:
	creds, project = _credentials()
	if not creds or not project:
		return {"sent": 0, "failed": 0, "skipped": len(tokens)}

	token = _access_token(creds)
	if not token:
		return {"sent": 0, "failed": 0, "skipped": len(tokens)}

	import requests

	# FCM لا تقبل إلا قيماً نصّية في data.
	payload_data = {str(k): str(v) for k, v in (data or {}).items() if v is not None}

	url = FCM_ENDPOINT.format(project=project)
	headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
	sent = failed = 0
	dead: list[str] = []

	for device in tokens[:MAX_TOKENS_PER_CALL]:
		message = {
			"message": {
				"token": device,
				"notification": {"title": title, "body": body},
				"data": payload_data,
				"android": {
					"priority": "high",
					"notification": {"channel_id": payload_data.get("channel", "general")},
				},
			}
		}
		try:
			resp = requests.post(url, headers=headers, json=message, timeout=10)
		except Exception:
			failed += 1
			continue

		if resp.status_code < 300:
			sent += 1
			continue

		failed += 1
		reason = ""
		try:
			details = resp.json().get("error", {}).get("details", [])
			for d in details:
				if d.get("errorCode"):
					reason = d["errorCode"]
					break
			reason = reason or resp.json().get("error", {}).get("status", "")
		except Exception:
			pass
		if reason in DEAD_TOKEN_ERRORS or resp.status_code == 404:
			dead.append(device)

	if dead:
		_retire(dead)
	if failed:
		_logger().warning(f"fcm: أُرسل {sent}، فشل {failed}، مُعطَّل {len(dead)}")

	return {"sent": sent, "failed": failed, "skipped": max(0, len(tokens) - MAX_TOKENS_PER_CALL)}


def _retire(tokens: list[str]) -> None:
	"""تعطيل الأجهزة التي رفضتها FCM نهائياً."""
	try:
		for name in frappe.get_all("MS Device Token", filters={"token": ["in", tokens]}, pluck="name"):
			frappe.db.set_value("MS Device Token", name, "is_active", 0, update_modified=False)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()


def notify(users, title: str, body: str, **data) -> None:
	"""النداء الذي تستخدمه مواضع الأحداث.

	يُجدول الإرسال في مهمة خلفية ولا ينفّذه هنا. الإرسال نداء HTTP لكل جهاز
	بمهلة عشر ثوانٍ، فتنفيذه داخل الطلب يعني أن إرسال رسالة إلى ثلاثين مستلماً
	يعلّق واجهة المرسل دقائق. والجدولة `after_commit` تضمن ألّا يرنّ الهاتف
	بإشعار عن رسالة لم تُحفَظ أصلاً لأن المعاملة تراجعت.
	"""
	users = [u for u in dict.fromkeys(users or []) if u]
	if not users:
		return
	try:
		frappe.enqueue(
			"match_schools.push.send_to_users",
			queue="short",
			enqueue_after_commit=True,
			users=users,
			title=title,
			body=body,
			data=data,
		)
	except Exception:
		# طابور معطّل يجب ألّا يُسقط العملية التي تسبّبت بالإشعار.
		_logger().warning(f"fcm: تعذّرت الجدولة — {frappe.get_traceback(with_context=False)[:400]}")
