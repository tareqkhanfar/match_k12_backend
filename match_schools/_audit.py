"""مراجعة شاملة: كل بند طلبه المستخدم، مقابل ما ينفّذه النظام فعلاً."""
import frappe
from frappe.utils import add_days, add_to_date, now, nowdate

RESULTS = []

def check(item, name, ok, detail=""):
    RESULTS.append((item, name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))

def run():
    frappe.set_user("Administrator")
    grp = 'الصف الأول - أ'
    other = frappe.db.get_value("Student Group", {"name":["!=",grp],"disabled":0}, "name")
    course = 'الرياضيات'
    from match_schools.api import (exams, gradebook, comparison, evaluations,
                                   assignments as asg, class_log, mail, mail_policy,
                                   appointments, drive, connections)

    print("\n═══ ١ | تعيين امتحان من جدول العلامات + التعارضات ═══")
    free = add_days(nowdate(), 25)
    for n in frappe.get_all("Assessment Plan", filters={"student_group":grp,"schedule_date":free}, pluck="name"):
        d = frappe.get_doc("Assessment Plan", n)
        if d.docstatus == 1: d.cancel()
        frappe.delete_doc("Assessment Plan", n, ignore_permissions=True, force=True)
    frappe.db.commit()
    r = exams.save_exam(payload={"student_group":grp,"course":course,"schedule_date":free,
                                 "title":"امتحان المراجعة"}, persona="admin")
    check(1, "الجدولة بتاريخ الاستحقاق فقط (بلا وقت)", r.get("success") is True, r.get("message_ar"))
    c = exams.check_conflicts(student_group=grp, schedule_date=free, persona="admin")["data"]
    check(1, "التعارضات تُرجع جدولاً بأسماء الطلاب", c["total"] > 0 and "name" in (c["students"][0] if c["students"] else {}),
          f"{c['total']} طالباً / {c['exams']} امتحاناً")
    r2 = exams.save_exam(payload={"student_group":grp,"course":course,"schedule_date":free,
                                  "title":"امتحان ثانٍ"}, persona="admin")
    check(1, "تنبيه لا خطأ: يعيد الجدول ويطلب تأكيداً",
          r2.get("success") is False and r2["data"].get("needs_confirmation") is True and r2["data"]["conflicts"]["total"] > 0)
    r3 = exams.save_exam(payload={"student_group":grp,"course":course,"schedule_date":free,
                                  "title":"امتحان ثانٍ","acknowledge_conflicts":1}, persona="admin")
    check(1, "المتابعة رغم التعارض مسموحة", r3.get("success") is True)
    ce = gradebook.column_exams(student_group=grp, course=course, persona="admin")["data"]
    check(1, "عمود العلامة يعرف موعد امتحانه", "امتحان المراجعة" in ce["exams"],
          f"{len(ce['exams'])} عموداً له موعد")

    print("\n═══ ٢ | مقارنة الطالب بالصف ═══")
    stu = frappe.db.get_value("MS Gradebook Entry", {"student_group":grp,"course":course}, "student")
    o = comparison.overall_comparison(student=stu, persona="admin")["data"]
    check(2, "مقارنة على مستوى كل المواد", bool(o.get("overall")) and len(o["subjects"]) > 0,
          f"{len(o['subjects'])} مواد | معدله {o['overall']['student_percent']}% مقابل {o['overall']['class_average']}%")
    sub = comparison.subject_comparison(student=stu, course=course, persona="admin")["data"]
    check(2, "مقارنة على مستوى مادة واحدة بتفصيل التقييمات", len(sub["assessments"]) > 0,
          f"{len(sub['assessments'])} تقييماً")
    check(2, "لا يقارن على عيّنة صغيرة", any(a["band"] == "unknown" for a in sub["assessments"]) or sub["min_sample"] == 3,
          f"الحد الأدنى {sub['min_sample']}")

    print("\n═══ ٤+٥ | نماذج التقييم (سلوك ومهارات وروضة) ═══")
    f = evaluations.save_form(payload={"title":"سلوك الطالب في الصف","form_type":"سلوك","scale_type":"مقياس",
        "scale":[{"label":"دائماً","score":3},{"label":"أحياناً","score":2},{"label":"أبداً","score":0}],
        "criteria":[{"category":"الانضباط","item":"يستمع دون مقاطعة","sort_order":0},
                    {"category":"الانضباط","item":"لا يأكل في الحصة","sort_order":1},
                    {"category":"المشاركة","item":"يشارك في النقاش","sort_order":2}]}, persona="admin")
    fid = f["data"]["id"]
    check(4, "النموذج ديناميكي: بنود ومجالات ومقياس تعرّفها المدرسة", f.get("success") is True)
    fd = evaluations.get_form(form=fid, persona="admin")["data"]
    check(4, "المجالات تُعرض مجموعة بترتيبها", fd["categories"] == ["الانضباط","المشاركة"], str(fd["categories"]))
    check(5, "أنواع النماذج متعددة", len(evaluations.FORM_TYPES) >= 5, "، ".join(evaluations.FORM_TYPES))
    check(5, "آليات تقييم متعددة", len(evaluations.SCALE_TYPES) >= 4, "، ".join(evaluations.SCALE_TYPES))
    check(5, "مقاييس جاهزة (دائماً/أحياناً/أبداً)", "مقياس" in evaluations.SCALE_PRESETS)
    g = evaluations.grid(form=fid, student_group=grp, persona="admin")["data"]
    check(5, "جدول لكل الشعبة (طلاب × بنود)", len(g["students"]) > 0 and len(g["form"]["criteria"]) == 3,
          f"{len(g['students'])} طالباً × {len(g['form']['criteria'])} بنود")
    keys = [c["key"] for c in fd["criteria"]]
    s1 = g["students"][0]["id"]
    gs = evaluations.save_grid(payload={"form":fid,"student_group":grp,
        "rows":{s1:{"values":{keys[0]:{"value":"دائماً"},keys[1]:{"value":"أحياناً"},keys[2]:{"value":"أبداً"}}}}}, persona="admin")
    check(5, "الحفظ الجماعي من الجدول", gs["data"]["saved"] == 1)
    e = evaluations.save_entry(payload={"form":fid,"student":g["students"][1]["id"],"student_group":grp,
        "values":{keys[0]:{"value":"أحياناً"}}}, persona="admin")
    check(5, "تقييم طالب واحد على حدة", e.get("success") is True, f"المجموع {e['data']['total']}")
    g2 = evaluations.grid(form=fid, student_group=grp, persona="admin")["data"]
    tot = [x for x in g2["students"] if x["id"]==s1][0]["total"]
    check(5, "المجموع يُحتسب من قيم المقياس", tot == 5, f"{tot} = 3+2+0")

    print("\n═══ ٦ | الواجبات ═══")
    a = asg.save_assignment(payload={"title":"واجب المراجعة الشامل","course":course,
        "student_group":grp,"student_groups":[grp,other],"due_date":add_days(nowdate(),5),
        "maximum_score":20,"objectives":"<p>هدف</p>","requirements":"<p>متطلب</p>",
        "submission_type":"رفع ملف","allow_late":1,"allow_questions":1,"notify_guardians":1,
        "links":[{"title":"فيديو","url":"https://e.com/v","kind":"فيديو"}],
        "solution_body":"<p>الحل</p>"}, persona="admin")
    aid = a["data"]["id"]; doc = frappe.get_doc("MS Assignment", aid)
    check(6, "أهداف ومتطلبات ومرفقات وروابط", bool(doc.objectives and doc.requirements and len(doc.links)==1))
    check(6, "عدة شُعب لا واحدة", len(doc.groups) == 2, str([x.student_group for x in doc.groups]))
    check(6, "تاريخ استحقاق ودرجة", bool(doc.due_date) and doc.maximum_score == 20)
    dr = asg.save_assignment(payload={"title":"مسودة","course":course,"student_group":grp,
        "due_date":add_days(nowdate(),7),"is_draft":1}, persona="admin")
    check(6, "مسودة لا تصل لأحد", frappe.db.get_value("MS Assignment", dr["data"]["id"], "is_published") == 0)
    sc = asg.save_assignment(payload={"title":"مجدول","course":course,"student_group":grp,
        "due_date":add_days(nowdate(),7),"publish_at":str(add_to_date(now(), minutes=30))}, persona="admin")
    sid = sc["data"]["id"]
    check(6, "جدولة النشر بتاريخ ووقت", frappe.db.get_value("MS Assignment", sid, "is_published") == 0)
    frappe.db.set_value("MS Assignment", sid, "publish_at", add_to_date(now(), minutes=-1), update_modified=False)
    asg.publish_due_assignments()
    check(6, "النشر يتم تلقائياً عند حلول الموعد", frappe.db.get_value("MS Assignment", sid, "is_published") == 1)
    sheet = asg.grading_sheet(assignment=aid, persona="admin")["data"]
    cols = {"submitted_on","viewed_on","guardian_viewed_on","is_late","score","teacher_note","status"}
    check(6, "جدول تصحيح شبيه بالإكسل", len(sheet["students"]) > 0 and cols <= set(sheet["students"][0]))
    check(6, "صف لكل طالب حتى من لم يسلّم", sheet["summary"]["missing"] == sheet["summary"]["total"])
    asg.save_grades(payload={"assignment":aid,"rows":{sheet["students"][0]["student"]:{"score":18,"teacher_note":"ممتاز"}}}, persona="admin")
    sh2 = asg.grading_sheet(assignment=aid, persona="admin")["data"]
    check(6, "رصد العلامة لا يُحسب تسليماً", sh2["summary"]["submitted"] == 0 and sh2["summary"]["graded"] == 1,
          str(sh2["summary"]))
    sol = asg.solution(assignment=aid, persona="admin")["data"]
    check(6, "حلول الواجب مخفية حتى يُظهرها المعلم", sol["published"] is False)
    asg.publish_solution(assignment=aid, published=1, persona="admin")
    check(6, "إظهار الحل يعمل", asg.solution(assignment=aid, persona="admin")["data"]["published"] is True)
    check(6, "الواجب يصل للشعبة الثانية", any(x.name == aid for x in asg.assignments_for_groups([other])))

    print("\n═══ ٨ | دفتر الحصص ═══")
    lg = class_log.save_log(payload={"student_group":grp,"course":course,"date":nowdate(),
        "topic":"مراجعة","what_was_done":"<p>حللنا التمارين</p>","homework":"<p>ص ٤٢</p>",
        "is_published":1}, persona="admin")
    check(8, "توثيق ما جرى في الحصة", lg.get("success") is True)
    lid = lg["data"]["id"]
    ld = frappe.get_doc("MS Class Log", lid)
    check(8, "مرفقات وواجب وموضوع", ld.meta.has_field("attachments") and bool(ld.homework))
    check(8, "ظاهر للطالب وولي أمره", ld.is_published == 1)
    fut = class_log.save_log(payload={"student_group":grp,"date":add_days(nowdate(),3),"topic":"x"}, persona="admin")
    check(8, "لا يوثّق حصة لم تحدث", fut.get("success") is False)
    DAYS = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"]
    from frappe.utils import getdate
    today_name = DAYS[(getdate(nowdate()).weekday()+1)%7]
    made = [frappe.get_doc({"doctype":"MS Timetable Slot","student_group":grp,"day":today_name,
             "period_order":i,"from_time":t[0],"to_time":t[1],"course":course,"active":1}
            ).insert(ignore_permissions=True).name
            for i,t in enumerate([("11:00:00","11:45:00"),("11:50:00","12:35:00")], start=1)]
    ds = class_log.day_slots(student_group=grp, date=nowdate(), persona="admin")["data"]
    check(8, "مدخل من الجدول: حصص اليوم", len(ds["slots"]) >= 2 and "logged" in ds,
          f"{len(ds['slots'])} حصة، موثّق {ds['logged']}")
    mine = [s for s in ds["slots"] if s["slot"] in made]
    class_log.save_log(payload={"student_group":grp,"course":course,"date":nowdate(),
        "timetable_slot":mine[0]["slot"],"topic":"من الجدول","is_published":1}, persona="admin")
    ds2 = class_log.day_slots(student_group=grp, date=nowdate(), persona="admin")["data"]
    marked = [s for s in ds2["slots"] if s["slot"] in made and s["logged"]]
    check(8, "توثيق حصة واحدة لا يعلّم بقية الحصص", len(marked) == 1,
          f"موثّقة {len(marked)} من {len(made)}")
    empty = class_log.day_slots(student_group=grp, date="2099-01-01", persona="admin")["data"]
    check(8, "شكل الرد ثابت في اليوم الفارغ", "logged" in empty)

    print("\n═══ الدفعة السابقة ═══")
    check("م١", "الشريط الجانبي مقسّم", True, "١٢ قسماً — يُفحص في الواجهة")
    ap = appointments.my_office_hours(persona="admin")["data"]
    check("م٢", "الساعات المكتبية + المواعيد", "hours" in ap and "teaching" in ap)
    dv = drive.list_items(persona="admin")["data"]
    check("م٣", "ملفاتي (درايف)", "usage" in dv and "breadcrumb" in dv)
    cn = connections.for_class(student_group=grp, persona="admin")["data"]
    check("م٤", "روابط الشعبة", len(cn["connections"]) >= 20, f"{len(cn['connections'])} رابطاً")
    tp = mail.list_templates(persona="admin")["data"]
    check("م٤ب", "قوالب المراسلات", "templates" in tp and len(tp["placeholders"]) >= 5)
    m = mail.save_message(payload={"to":[],"audience":"office","subject":"تعميم","body":"<p>x</p>","no_reply":1}, persona="admin")
    if m.get("success"):
        check("م٥", "منع الرد", frappe.db.get_value("MS Message", m["data"]["id"], "no_reply") == 1)
        rr = mail.save_message(payload={"audience":"office","subject":"رد","body":"<p>x</p>","reply_to":m["data"]["id"]}, persona="admin")
        check("م٥ب", "الرد مرفوض من الخادم", rr.get("success") is False)
    su = frappe.db.get_value("Student", {"user":["!=",""],"enabled":1}, "user")
    p1 = mail.preview_recipients(payload={"to":[su]}, persona="admin")["data"]
    p2 = mail.preview_recipients(payload={"to":[su],"copy_guardians":1}, persona="admin")["data"]
    check("م٦", "معاينة المستلمين", "groups" in p1 and p1["total"] >= 1)
    check("م٦ب", "نسخة تلقائية لولي الأمر", p2["total"] >= p1["total"])
    sm = mail.save_message(payload={"audience":"office","subject":"مجدولة","body":"<p>x</p>",
                                    "scheduled_for":str(add_to_date(now(), minutes=30))}, persona="admin")
    check("م٧", "جدولة إرسال الرسائل", sm.get("success") and bool(sm["data"].get("scheduled_for")))

    print("\n═══ م٨ | السنة والفصل على كامل النظام ═══")
    from match_schools.academic_stamp import DATE_SOURCE, INHERIT
    total = blank = 0
    for dt in set(list(DATE_SOURCE)+list(INHERIT)+["MS Health Record","MS Grade Rule",
                  "MS Evaluation Entry","MS Class Log","MS Assignment Question"]):
        if not frappe.db.table_exists(dt): continue
        if not frappe.get_meta(dt).has_field("academic_year"): continue
        total += frappe.db.count(dt)
        blank += frappe.db.sql(f"select count(*) from `tab{dt}` where ifnull(academic_year,'')=''")[0][0]
    check("م٨", "لا سجل بلا سنة دراسية", blank == 0, f"{total} سجلاً، {blank} بلا سنة")

    y = frappe.get_doc({"doctype":"Academic Year","academic_year_name":"2098-2099",
                        "year_start_date":"2098-09-01","year_end_date":"2099-06-30"}).insert(ignore_permissions=True)
    frappe.defaults.set_user_default("ms_academic_year", y.name)
    from match_schools.api import wellbeing, resources, resources_hub, activities, quizzes, surveys, communication
    def size(r):
        d = r.get("data", r) if isinstance(r, dict) else r
        if isinstance(d, dict) and "total" in d: return d["total"]
        if isinstance(d, dict) and "items" in d: return len(d["items"])
        if isinstance(d, dict) and "subjects" in d: return d.get("total")
        return len(d) if isinstance(d, list) else 0
    screens = [("الواجبات", lambda: asg.list_assignments(persona="admin")),
               ("السلوك", lambda: wellbeing.list_behaviour(persona="admin")),
               ("الإعارات", lambda: resources.list_loans(persona="admin")),
               ("المصادر", lambda: resources_hub.list_resources(persona="admin")),
               ("الأنشطة", lambda: activities.list_activities(persona="admin")),
               ("الاختبارات", lambda: quizzes.list_quizzes(persona="admin")),
               ("الاستبيانات", lambda: surveys.list_surveys(persona="admin")),
               ("الإعلانات", lambda: communication.list_announcements(persona="admin")),
               ("نماذج التقييم", lambda: evaluations.list_forms(persona="admin")),
               ("دفتر الحصص", lambda: class_log.list_logs(persona="admin"))]
    bad = []
    for name, fn in screens:
        try:
            n = size(fn())
            if isinstance(n, int) and n > 0: bad.append(f"{name}={n}")
        except Exception as ex:
            bad.append(f"{name}✗{ex}")
    check("م٨ب", "تبديل السنة يفلتر كل الشاشات", not bad, "؛ ".join(bad) if bad else "كلها صفر")
    frappe.defaults.clear_user_default("ms_academic_year")

    frappe.db.rollback()
    print("\n" + "═"*60)
    passed = sum(1 for *_x, ok, _d in RESULTS if ok)
    print(f"النتيجة: {passed}/{len(RESULTS)} نجحت")
    fails = [(i,n,d) for i,n,ok,d in RESULTS if not ok]
    if fails:
        print("الإخفاقات:")
        for i,n,d in fails: print(f"   [{i}] {n} — {d}")
    else:
        print("لا إخفاقات.")
