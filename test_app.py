import base64, hashlib, io, os, sys, tempfile
os.environ["ORG_DATA_DIR"] = tempfile.mkdtemp()
if not os.environ.get("PG_SCHEMA"):  # الافتراضي: SQLite مؤقتة. لاختبار Postgres عيّن PG_SCHEMA و DATABASE_URL
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("POSTGRES_URL", None)
os.environ["ADMIN_PASSWORD"] = "AdminPass123"
os.environ["ORG_NAME"] = "مؤسسة النور"
import app as A
from PIL import Image, ImageDraw
from pypdf import PdfReader

def Q(sql, args=(), one=False):
    with A.app.app_context():
        rows = [dict(r) for r in A.q(sql, args)]
    return (rows[0] if rows else None) if one else rows
def X(sql, args=()):
    with A.app.app_context():
        A.ex(sql, args)
def stock(i):
    with A.app.app_context():
        return A.stock(i)
def sigdata():
    im = Image.new("RGBA", (300, 100), (0, 0, 0, 0)); ImageDraw.Draw(im).line([(10, 80), (80, 20), (140, 90), (290, 30)], fill=(16, 40, 107, 255), width=4)
    b = io.BytesIO(); im.save(b, "PNG"); return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()

def client(user=None, pw=None):
    c = A.app.test_client(); c.pw = pw
    with c.session_transaction() as s: s["csrf"] = "t"
    if user:
        r = c.post("/login", data={"csrf": "t", "username": user, "password": pw}); assert r.status_code == 302, (user, r.status_code)
        with c.session_transaction() as s: s["csrf"] = "t"
    return c
def post(c, url, **d):
    d["csrf"] = "t"
    if url.endswith("/act") and "password" not in d: d["password"] = c.pw
    return c.post(url, data=d, follow_redirects=False)
def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond: ok.failed += 1
ok.failed = 0
last = lambda: Q("SELECT id FROM requests ORDER BY id DESC", one=True)["id"]
status = lambda rid: Q("SELECT status FROM requests WHERE id=?", (rid,), one=True)["status"]

adm = client("admin", "AdminPass123")
ok(A.app.test_client().post("/login", data={"username": "admin", "password": "x"}).status_code == 400, "POST بدون CSRF مرفوض")
c0 = client(); r = c0.post("/login", data={"csrf": "t", "username": "admin", "password": "bad"}); ok(r.status_code == 200, "دخول خاطئ يبقى في الصفحة")
for n, sal in [("أحمد", 3000), ("سارة", 4000), ("خالد", 3500), ("منى", 3200), ("ياسر", 2800)]:
    post(adm, "/employees/new", name=n, salary=str(sal), leave_balance="21", job_title="x", department="d")
ok(len(Q("SELECT * FROM employees")) == 5, "إضافة 5 موظفين")
mk = lambda u, n, role, eid: post(adm, "/users", username=u, full_name=n, role=role, password="Passw0rd!x", employee_id=str(eid))
mk("emp1", "أحمد", "employee", 1); mk("mgr", "سارة", "manager", 2); mk("hr1", "خالد", "hr", 3); mk("acc", "منى", "accountant", 4); mk("store", "ياسر", "store", 5)
ok(len(Q("SELECT * FROM users")) == 6, "إنشاء 5 مستخدمين")
emp, mgr, hr, acc, store = [client(u, "Passw0rd!x") for u in ("emp1", "mgr", "hr1", "acc", "store")]
# التوقيعات الإلكترونية
ok(post(adm, "/signature", data="data:image/png;base64,AAAA").status_code == 200, "رفض توقيع غير صالح")
blank = Image.new("RGBA", (50, 20), (0, 0, 0, 0)); b = io.BytesIO(); blank.save(b, "PNG")
post(emp, "/signature", data="data:image/png;base64," + base64.b64encode(b.getvalue()).decode())
ok(Q("SELECT signature FROM users WHERE username='emp1'", one=True)["signature"] is None, "رفض توقيع فارغ")
for c in (emp, mgr, hr, acc, adm):
    ok(post(c, "/signature", data=sigdata()).status_code == 302, "حفظ توقيع مستخدم")

# صلاحيات
ok(emp.get("/employees").status_code == 403, "الموظف لا يرى الموظفين")
ok(emp.get("/accounting").status_code == 403, "الموظف لا يرى المحاسبة")
ok(mgr.get("/accounting").status_code == 403, "المدير لا يرى المحاسبة")
ok(hr.get("/users").status_code == 403, "HR لا يدير المستخدمين")
ok("3000" not in mgr.get("/employees").get_data(as_text=True), "المدير يرى الموظفين دون الرواتب")
ok(A.app.test_client().get("/employees").status_code == 302, "غير المسجل يُحوَّل للدخول")
ok(emp.get("/requests/new?type=advance").status_code == 404, "النوع القديم مخفي")

# ===== إجازة: موظف -> مدير -> HR (توقيع بكلمة مرور)
post(emp, "/requests/new", type="leave", date_from="2026-11-01", date_to="2026-11-03", details="x"); rid = last()
ok(emp.post(f"/requests/{rid}/act", data={"csrf": "t", "action": "approve", "password": "Passw0rd!x"}).status_code == 403, "صاحب الطلب لا يعتمد طلبه")
ok(hr.post(f"/requests/{rid}/act", data={"csrf": "t", "action": "approve", "password": "Passw0rd!x"}).status_code == 403, "HR لا يعتمد قبل دوره")
post(mgr, f"/requests/{rid}/act", action="approve", password="wrong")
ok(status(rid) == "pending" and Q("SELECT status FROM request_steps WHERE request_id=? AND step_no=1", (rid,), one=True)["status"] == "pending", "كلمة مرور خاطئة = لا توقيع")
post(mgr, f"/requests/{rid}/act", action="approve", note="موافق")
post(hr, f"/requests/{rid}/act", action="approve")
ok(status(rid) == "approved", "اعتماد نهائي للإجازة")
ok(Q("SELECT leave_balance b FROM employees WHERE id=1", one=True)["b"] == 18, "خصم 3 أيام من الرصيد")
ok(len(Q("SELECT * FROM attendance WHERE employee_id=1 AND status='leave'")) == 3, "تسجيل الإجازة في الحضور")
ok(Q("SELECT seal_code FROM requests WHERE id=?", (rid,), one=True)["seal_code"] is not None, "ختم تلقائي لطلب الإجازة")
post(emp, "/requests/new", type="leave", date_from="2026-12-01", date_to="2026-12-30")
ok(Q("SELECT COUNT(*) c FROM requests", one=True)["c"] == 1, "رفض إجازة أكبر من الرصيد")
post(emp, "/requests/new", type="general", title="طلب", details="تفاصيل"); r2 = last()
post(mgr, f"/requests/{r2}/act", action="reject", note=""); ok(status(r2) == "pending", "الرفض بدون سبب مرفوض")
post(mgr, f"/requests/{r2}/act", action="reject", note="غير مناسب"); ok(status(r2) == "rejected", "الرفض بسبب")

# ===== الطلب المالي: موظف -> مدير -> مالية (صرف) -> إدارة (اعتماد) -> ملف مختوم
post(emp, "/requests/new", type="financial", category="شراء", amount="500", title="شراء أحبار", details="أحبار للطابعة"); r3 = last()
st = Q("SELECT role, kind FROM request_steps WHERE request_id=? ORDER BY step_no", (r3,))
ok([(s["role"], s["kind"]) for s in st] == [("manager", "approve"), ("accountant", "disburse"), ("admin", "ack")], "مسار الطلب المالي: مدير ← مالية(صرف) ← إدارة(اعتماد)")
ok(Q("SELECT COUNT(*) c FROM notifications WHERE user_id=(SELECT id FROM users WHERE username='mgr') AND text LIKE ?", (f"%#{r3}%",), one=True)["c"] == 1, "إشعار للمدير بالطلب الجديد")
ok(acc.post(f"/requests/{r3}/act", data={"csrf": "t", "action": "approve", "password": "Passw0rd!x"}).status_code == 403, "المالية لا تصرف قبل موافقة المدير")
post(mgr, f"/requests/{r3}/act", action="approve", note="موافق")
ok(Q("SELECT COUNT(*) c FROM notifications WHERE user_id=(SELECT id FROM users WHERE username='acc') AND text LIKE ?", (f"%#{r3}%",), one=True)["c"] == 1, "إشعار للمالية بالصرف")
post(acc, f"/requests/{r3}/act", action="approve", paid_amount="0", paid_account="الصندوق", paid_date="2026-10-17")
ok(Q("SELECT status FROM request_steps WHERE request_id=? AND step_no=2", (r3,), one=True)["status"] == "pending", "الصرف يتطلب مبلغاً صحيحاً")
post(acc, f"/requests/{r3}/act", action="approve", paid_amount="480", paid_account="الصندوق", paid_date="2026-10-17", paid_ref="RCPT-77", note="صُرف نقداً")
rq = Q("SELECT * FROM requests WHERE id=?", (r3,), one=True)
ok(rq["paid_amount"] == 480 and rq["paid_ref"] == "RCPT-77" and rq["status"] == "pending", "تسجيل بيانات الصرف والطلب بانتظار الإدارة")
ok(Q("SELECT amount, kind, account FROM transactions WHERE request_id=?", (r3,), one=True) == {"amount": 480.0, "kind": "expense", "account": "الصندوق"}, "قيد مصروف تلقائي بالمبلغ الفعلي")
ok(Q("SELECT COUNT(*) c FROM notifications WHERE user_id=(SELECT id FROM users WHERE username='admin') AND text LIKE ?", (f"%#{r3}%",), one=True)["c"] >= 1, "إشعار للإدارة بإتمام الصرف")
ok(Q("SELECT COUNT(*) c FROM notifications WHERE user_id=(SELECT id FROM users WHERE username='emp1') AND text LIKE ?", (f"%صرف مبلغ%#{r3}%",), one=True)["c"] == 1, "إشعار للموظف بالصرف")
ok("اعتماد" in adm.get(f"/requests/{r3}").get_data(as_text=True), "صفحة الطلب تعرض اعتماد الإدارة")
post(adm, f"/requests/{r3}/act", action="approve", note="اعتماد نهائي")
rq = Q("SELECT * FROM requests WHERE id=?", (r3,), one=True)
ok(rq["status"] == "approved" and rq["seal_code"] and len(rq["seal_hash"]) == 64, "اعتماد الإدارة + رمز الختم")
with A.app.app_context():
    pdf_bytes = A.get_file(f"sealed/{r3}.pdf") or b""
ok(pdf_bytes[:5] == b"%PDF-", "توليد ملف PDF مختوم")
ok(len(PdfReader(io.BytesIO(pdf_bytes)).pages) >= 1 and hashlib.sha256(pdf_bytes).hexdigest() == rq["sealed_pdf_hash"], "PDF صالح وبصمته مسجلة")
sig_rows = Q("SELECT * FROM request_steps WHERE request_id=? ORDER BY step_no", (r3,))
ok(all(s["sig"] and s["chain_hash"] and s["actor_name"] for s in sig_rows) and len({s["chain_hash"] for s in sig_rows}) == 3, "كل توقيع محفوظ ببصمة متسلسلة")
ok(emp.get(f"/requests/{r3}/sealed").status_code == 200 and acc.get(f"/requests/{r3}/sealed").status_code == 200, "تنزيل الملف المختوم لأطراف الطلب")
ok(store.get(f"/requests/{r3}/sealed").status_code == 403, "غير المعنيين لا ينزّلون الملف المختوم")
ok(Q("SELECT COUNT(*) c FROM notifications WHERE user_id=(SELECT id FROM users WHERE username='emp1') AND text LIKE ?", (f"%اكتمل طلبك #{r3}%",), one=True)["c"] == 1, "إشعار للموظف باكتمال الختم")
pub = A.app.test_client()
code = rq["seal_code"]; vr = pub.get(f"/verify?code={code}").get_data(as_text=True)
ok("ملف مختوم صحيح" in vr and "480.00" in vr, "صفحة التحقق العامة (بلا دخول)")
with pub.session_transaction() as s: s["csrf"] = "t"
good = pub.post("/verify", data={"csrf": "t", "code": code, "file": (io.BytesIO(pdf_bytes), "a.pdf")}, content_type="multipart/form-data").get_data(as_text=True)
bad = pub.post("/verify", data={"csrf": "t", "code": code, "file": (io.BytesIO(b"%PDF-fake"), "a.pdf")}, content_type="multipart/form-data").get_data(as_text=True)
ok("مطابق للأصل" in good and "لا يطابق" in bad, "فحص بصمة ملف PDF: الأصلي مقبول والمعدّل مرفوض")
ok("لا يوجد ملف" in pub.get("/verify?code=ZZZZ-ZZZZ-ZZZZ").get_data(as_text=True), "رمز ختم غير موجود")
# كشف التلاعب
X("UPDATE requests SET amount=9999 WHERE id=?", (r3,))
ok("تم العبث" in pub.get(f"/verify?code={code}").get_data(as_text=True) and "تم تعديل بيانات" in adm.get(f"/requests/{r3}").get_data(as_text=True), "كشف تعديل المبلغ بعد التوقيع")
X("UPDATE requests SET amount=500 WHERE id=?", (r3,))
ok("ملف مختوم صحيح" in pub.get(f"/verify?code={code}").get_data(as_text=True), "عودة السلامة بعد استرجاع القيمة")
X("UPDATE request_steps SET note='مزوّر' WHERE request_id=? AND step_no=1", (r3,))
ok("تم العبث" in pub.get(f"/verify?code={code}").get_data(as_text=True), "كشف تعديل ملاحظة توقيع")
X("UPDATE request_steps SET note='موافق' WHERE request_id=? AND step_no=1", (r3,))
# إنابة المسؤول
post(emp, "/requests/new", type="financial", category="سلفة", amount="100", details="سلفة"); r5 = last()
post(adm, f"/requests/{r5}/act", action="approve")
ok(Q("SELECT on_behalf FROM request_steps WHERE request_id=? AND step_no=1", (r5,), one=True)["on_behalf"] == 1, "توقيع المسؤول بالإنابة يُسجَّل")
# مخزون + صرف مواد (مع توقيع أمين المخزن)
post(store, "/inventory", name="ورق A4", unit="رزمة", min_qty="5"); post(store, "/inventory/move", item_id="1", kind="in", qty="10", note="شراء")
ok(stock(1) == 10, "إدخال 10 للمخزون")
post(store, "/inventory/move", item_id="1", kind="out", qty="50"); ok(stock(1) == 10, "منع إخراج أكبر من الرصيد")
post(emp, "/requests/new", type="material", item_id="1", qty="4", details="للمكتب"); r4 = last()
post(mgr, f"/requests/{r4}/act", action="approve")
rs = post(store, f"/requests/{r4}/act", action="approve")
ok(rs.status_code == 302 and "/signature" in rs.headers["Location"] and status(r4) == "pending", "أمين المخزن بلا توقيع يُحوَّل لتسجيله")
post(store, "/signature", data=sigdata()); post(store, f"/requests/{r4}/act", action="approve")
ok(stock(1) == 6 and status(r4) == "approved", "صرف المواد يخصم من المخزون بعد التوقيع")
post(mgr, "/requests/new", type="leave", date_from="2026-11-10", date_to="2026-11-10"); rr = last()
ok([s["role"] for s in Q("SELECT role FROM request_steps WHERE request_id=?", (rr,))] == ["hr"], "تخطي خطوة المدير لطلب المدير نفسه")
# الرواتب
post(adm, "/attendance", day="2026-11-05", **{"st_1": "absent", "st_2": "present", "st_3": "present", "st_4": "present", "st_5": "present"})
post(hr, "/payroll", month="2026-11"); p1 = Q("SELECT * FROM payroll WHERE employee_id=1 AND month='2026-11'", one=True)
ok(len(Q("SELECT * FROM payroll WHERE month='2026-11'")) == 5 and abs(p1["deductions"] - 100) < 0.01, "توليد الرواتب وخصم يوم غياب = 100")
post(hr, f"/payroll/{p1['id']}/edit", allowances="100", deductions="20", note="حافز")
p1 = Q("SELECT * FROM payroll WHERE id=?", (p1["id"],), one=True); ok(p1["net"] == 3080, "تعديل البدلات: الصافي 3080")
ok(hr.post(f"/payroll/{p1['id']}/pay", data={"csrf": "t", "account": "الصندوق"}).status_code == 403, "HR لا يصرف الراتب")
post(acc, f"/payroll/{p1['id']}/pay", account="الصندوق"); post(acc, f"/payroll/{p1['id']}/pay", account="الصندوق")
ok(Q("SELECT COUNT(*) c FROM transactions WHERE category='رواتب'", one=True)["c"] == 1, "صرف الراتب مرة واحدة فقط")
# محاسبة
post(acc, "/accounting", day="2026-11-02", kind="income", category="تبرعات", amount="10000", account="البنك", description="x")
ok("10,000.00" in acc.get("/accounting?month=2026-11").get_data(as_text=True), "صفحة المحا��بة")
post(acc, "/accounting", day="2026-11-02", kind="expense", category="=cmd", amount="-5", account="البنك")
ok(Q("SELECT COUNT(*) c FROM transactions WHERE category='=cmd'", one=True)["c"] == 0, "رفض مبلغ سالب")
post(acc, "/accounting", day="2026-11-03", kind="expense", category="=HYPERLINK(1)", amount="5", account="البنك")
ok("'=HYPERLINK" in acc.get("/accounting/export?month=2026-11").get_data(as_text=True), "حماية CSV من حقن الصيغ")
ok(acc.post("/accounting/1/delete", data={"csrf": "t"}).status_code == 403, "المحاسب لا يحذف القيود")
# مرفقات و XSS
r = emp.post("/requests/new", data={"csrf": "t", "type": "general", "title": "م", "details": "d", "file": (io.BytesIO(b"x"), "evil.exe")}, content_type="multipart/form-data"); ok(r.status_code == 200, "رفض مرفق .exe")
emp.post("/requests/new", data={"csrf": "t", "type": "general", "title": "م", "details": "d", "file": (io.BytesIO(b"%PDF-1"), "a.pdf")}, content_type="multipart/form-data"); ra = last()
ok(emp.get(f"/requests/{ra}/file").status_code == 200 and store.get(f"/requests/{ra}/file").status_code == 403, "المرفق لأصحابه فقط")
post(emp, "/requests/new", type="general", title="<script>alert(1)</script>", details="d"); rx = last()
ok("<script>alert(1)</script>" not in emp.get(f"/requests/{rx}").get_data(as_text=True), "ترميز المخرجات ضد XSS")
# فلترة وبحث وإشعارات
ok("شراء أحبار" in emp.get("/requests?q=أحبار").get_data(as_text=True) and "شراء أحبار" not in emp.get("/requests?q=zzz").get_data(as_text=True), "بحث الطلبات")
ok(emp.get("/search?q=x").status_code == 302 and emp.get("/notifications").status_code == 200, "البحث والإشعارات")
ok(Q("SELECT COUNT(*) c FROM notifications WHERE user_id=(SELECT id FROM users WHERE username='emp1') AND read=0", one=True)["c"] == 0, "قراءة الإشعارات تعلّمها مقروءة")
# المشاريع وملفاتها
ok(emp.post("/projects", data={"csrf": "t", "name": "x"}).status_code == 403, "الموظف لا ينشئ مشروعاً")
post(mgr, "/projects", name="مشروع المبنى", client="البلدية", description="وصف")
pj = Q("SELECT id FROM projects WHERE name='مشروع المبنى'", one=True)["id"]
emp.post(f"/projects/{pj}/upload", data={"csrf": "t", "files": [(io.BytesIO(b"%PDF-1 plan"), "خطة.pdf"), (io.BytesIO(b"doc"), "عقد.docx")]}, content_type="multipart/form-data")
ok(Q("SELECT COUNT(*) c FROM project_files WHERE project_id=?", (pj,), one=True)["c"] == 2, "رفع عدة ملفات للمشروع")
r = emp.post(f"/projects/{pj}/upload", data={"csrf": "t", "relpath": "مخططات/الدور الأول/a.dwg", "files": (io.BytesIO(b"dwg"), "a.dwg")}, content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
ok(r.status_code == 200 and r.get_json()["ok"] and Q("SELECT 1 FROM project_files WHERE original_name='مخططات/الدور الأول/a.dwg'", one=True), "رفع مجلد مع الحفاظ على المسار")
r = emp.post(f"/projects/{pj}/upload", data={"csrf": "t", "relpath": "../../evil.exe", "files": (io.BytesIO(b"MZ"), "evil.exe")}, content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
ok(r.status_code == 400 and not r.get_json()["ok"], "رفض ملف تنفيذي في المشاريع")
pf = Q("SELECT id FROM project_files WHERE original_name='خطة.pdf'", one=True)["id"]
r = store.get(f"/projects/{pj}/files/{pf}"); ok(r.status_code == 200 and r.data == b"%PDF-1 plan", "تنزيل ملف المشروع")
ok("مشروع المبنى" in store.get("/projects?q=المبنى").get_data(as_text=True) and "عقد.docx" in store.get(f"/projects/{pj}").get_data(as_text=True), "عرض المشاريع وملفاتها")
ok(store.post(f"/projects/{pj}/files/{pf}/delete", data={"csrf": "t"}).status_code == 403, "لا يحذف ملف غيره")
post(emp, f"/projects/{pj}/files/{pf}/delete")
ok(Q("SELECT COUNT(*) c FROM project_files WHERE project_id=?", (pj,), one=True)["c"] == 2, "صاحب الملف يحذفه")
ok(mgr.post(f"/projects/{pj}/delete", data={"csrf": "t"}).status_code == 403, "حذف المشروع للإدارة فقط")
post(mgr, f"/projects/{pj}/edit", name="مشروع المبنى", client="البلدية", status="done", description="")
ok(Q("SELECT status FROM projects WHERE id=?", (pj,), one=True)["status"] == "done", "تعديل حالة المشروع")
post(adm, "/projects", name="مشروع مؤقت"); pj2 = Q("SELECT id FROM projects WHERE name='مشروع مؤقت'", one=True)["id"]
emp.post(f"/projects/{pj2}/upload", data={"csrf": "t", "files": (io.BytesIO(b"x"), "a.txt")}, content_type="multipart/form-data")
post(adm, f"/projects/{pj2}/delete")
ok(not Q("SELECT 1 FROM projects WHERE id=?", (pj2,), one=True) and not Q("SELECT 1 FROM project_files WHERE project_id=?", (pj2,), one=True), "الإدارة تحذف المشروع وملفاته")
# كل الصفحات
for p in ["/", "/requests", "/requests?scope=all&status=approved", "/employees", "/attendance", "/payroll?month=2026-11", "/inventory", "/inventory/moves", "/accounting", "/users", "/audit", "/password", "/signature", f"/requests/{r3}", f"/requests/{r3}/print", f"/requests/{rid}", "/requests/new?type=financial", "/requests/new?type=material", "/requests/new?type=exit", "/employees/1/edit", "/projects", f"/projects/{pj}"]:
    ok(adm.get(p).status_code == 200, "GET " + p)
for c, n in ((emp, "emp"), (mgr, "mgr"), (hr, "hr"), (acc, "acc"), (store, "store")):
    ok(c.get("/").status_code == 200 and c.get("/requests").status_code == 200, "الصفحات الرئيسية لـ " + n)
r = adm.post("/backup", data={"csrf": "t"})
ok(r.status_code == 200 and (b'"users"' in r.data if A.USE_PG else r.data[:6] == b"SQLite"), "نسخة احتياطية صالحة")
ok(adm.post("/users/1/toggle", data={"csrf": "t"}).status_code == 403, "المدير لا يعطل نفسه")
print("\nالفاشلة:", ok.failed); sys.exit(1 if ok.failed else 0)
