# -*- coding: utf-8 -*-
"""نظام إداري متكامل لمؤسسة صغيرة (~20 موظفاً)
موظفون + حضور + رواتب + مخزون + محاسبة مبسطة + طلبات إدارية بسلاسل موافقة.
Flask + SQLite، بلا اعتمادات ثقيلة.
"""
import base64
import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta
from functools import wraps

from arabic_pdf import build_sealed_pdf, data_url_to_png
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, send_from_directory, session, url_for)
from PIL import Image
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("ORG_DATA_DIR", os.path.join(BASE, "data"))
UPLOADS = os.path.join(DATA, "uploads")
SEALED = os.path.join(DATA, "sealed")
DB_PATH = os.path.join(DATA, "org.db")
os.makedirs(UPLOADS, exist_ok=True)
os.makedirs(SEALED, exist_ok=True)
ORG_NAME = os.environ.get("ORG_NAME", "المؤسسة")


def _secret():
    p = os.path.join(DATA, "secret.key")
    if not os.path.exists(p):
        with open(p, "w") as f:
            f.write(secrets.token_hex(32))
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    with open(p) as f:
        return f.read().strip()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_secret(),
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("HTTPS", "") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

ROLES = {"admin": "الإدارة العليا", "manager": "مدير", "hr": "موارد بشرية",
         "accountant": "محاسب", "store": "أمين مخزن", "employee": "موظف"}
STEP_KINDS = {"approve": "موافقة", "disburse": "صرف", "ack": "اعتماد الإدارة"}
CATEGORIES = ["سلفة", "شراء", "مصروف تشغيلي", "استرداد مصروفات", "أخرى"]
TYPES = {
    "leave": {"label": "طلب إجازة", "chain": [("manager", "approve"), ("hr", "approve")]},
    "financial": {"label": "طلب مالي", "chain": [("manager", "approve"), ("accountant", "disburse"), ("admin", "ack")]},
    "material": {"label": "طلب صرف مواد من المخزن", "chain": [("manager", "approve"), ("store", "approve")]},
    "exit": {"label": "إذن خروج", "chain": [("manager", "approve")]},
    "general": {"label": "طلب إداري عام", "chain": [("manager", "approve"), ("admin", "approve")]},
    # أنواع قديمة تبقى للقراءة فقط (الطلب المالي الموحد حلّ محلها)
    "advance": {"label": "طلب سلفة (قديم)", "hidden": True,
                "chain": [("manager", "approve"), ("accountant", "approve"), ("admin", "approve")]},
    "purchase": {"label": "طلب شراء (قديم)", "hidden": True,
                 "chain": [("manager", "approve"), ("accountant", "approve"), ("admin", "approve")]},
}
STATUS = {"pending": "قيد الموافقة", "approved": "معتمد",
          "rejected": "مرفوض", "cancelled": "ملغي"}
ATT = {"present": "حاضر", "absent": "غائب", "late": "متأخر", "leave": "إجازة"}
ACCOUNTS = ["الصندوق", "البنك"]
ALLOWED_EXT = {"pdf", "png", "jpg", "jpeg", "docx", "xlsx"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees(
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, job_title TEXT, department TEXT,
 phone TEXT, hire_date TEXT, salary REAL DEFAULT 0, leave_balance REAL DEFAULT 21,
 status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS users(
 id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
 full_name TEXT NOT NULL, role TEXT NOT NULL,
 employee_id INTEGER REFERENCES employees(id), active INTEGER DEFAULT 1, signature TEXT);
CREATE TABLE IF NOT EXISTS attendance(
 id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
 day TEXT NOT NULL, status TEXT NOT NULL, note TEXT, UNIQUE(employee_id, day));
CREATE TABLE IF NOT EXISTS payroll(
 id INTEGER PRIMARY KEY, month TEXT NOT NULL,
 employee_id INTEGER NOT NULL REFERENCES employees(id),
 base REAL, allowances REAL DEFAULT 0, deductions REAL DEFAULT 0, net REAL,
 note TEXT, paid INTEGER DEFAULT 0, UNIQUE(month, employee_id));
CREATE TABLE IF NOT EXISTS items(
 id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, unit TEXT, min_qty REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS stock_moves(
 id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id),
 qty REAL NOT NULL, note TEXT, user_id INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS transactions(
 id INTEGER PRIMARY KEY, day TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('income','expense')), category TEXT,
 amount REAL NOT NULL CHECK(amount>0), account TEXT NOT NULL, description TEXT,
 user_id INTEGER, request_id INTEGER);
CREATE TABLE IF NOT EXISTS requests(
 id INTEGER PRIMARY KEY, type TEXT NOT NULL,
 requester_id INTEGER NOT NULL REFERENCES users(id), title TEXT NOT NULL,
 details TEXT, amount REAL, date_from TEXT, date_to TEXT, days REAL,
 item_id INTEGER REFERENCES items(id), qty REAL, attachment TEXT, attachment_name TEXT,
 status TEXT NOT NULL DEFAULT 'pending', created_at TEXT,
 category TEXT, paid_amount REAL, paid_account TEXT, paid_ref TEXT, paid_date TEXT, paid_by INTEGER,
 seal_code TEXT, seal_hash TEXT, sealed_at TEXT, sealed_pdf_hash TEXT);
CREATE TABLE IF NOT EXISTS request_steps(
 id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES requests(id),
 step_no INTEGER NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 acted_by INTEGER, acted_at TEXT, note TEXT,
 kind TEXT DEFAULT 'approve', sig TEXT, sig_hash TEXT, chain_hash TEXT, ip TEXT,
 actor_name TEXT, actor_role TEXT, on_behalf INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS notifications(
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, text TEXT, url TEXT, read INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY, user_id INTEGER, action TEXT, at TEXT);
"""


# ----------------------------------------------------------------- قاعدة البيانات
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def q(sql, args=(), one=False):
    rows = db().execute(sql, args).fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def ex(sql, args=()):
    cur = db().execute(sql, args)
    db().commit()
    return cur.lastrowid


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def log(action):
    u = current_user()
    ex("INSERT INTO audit(user_id,action,at) VALUES(?,?,?)",
       (u["id"] if u else None, action, now()))


MIGRATIONS = [("users", "signature", "TEXT"), ("requests", "category", "TEXT"), ("requests", "paid_amount", "REAL"),
              ("requests", "paid_account", "TEXT"), ("requests", "paid_ref", "TEXT"), ("requests", "paid_date", "TEXT"),
              ("requests", "paid_by", "INTEGER"), ("requests", "seal_code", "TEXT"), ("requests", "seal_hash", "TEXT"),
              ("requests", "sealed_at", "TEXT"), ("requests", "sealed_pdf_hash", "TEXT"),
              ("request_steps", "kind", "TEXT DEFAULT 'approve'"), ("request_steps", "sig", "TEXT"),
              ("request_steps", "sig_hash", "TEXT"), ("request_steps", "chain_hash", "TEXT"), ("request_steps", "ip", "TEXT"),
              ("request_steps", "actor_name", "TEXT"), ("request_steps", "actor_role", "TEXT"),
              ("request_steps", "on_behalf", "INTEGER DEFAULT 0")]


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    for t, c, ddl in MIGRATIONS:  # ترقية قواعد بيانات الإصدار السابق
        if c not in [r[1] for r in con.execute(f"PRAGMA table_info({t})")]:
            con.execute(f"ALTER TABLE {t} ADD COLUMN {c} {ddl}")
    if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        pw = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(9)
        con.execute("INSERT INTO users(username,password_hash,full_name,role) VALUES(?,?,?,?)",
                    ("admin", generate_password_hash(pw), "مدير النظام", "admin"))
        con.commit()
        print("=" * 50)
        print("تم إنشاء المستخدم: admin")
        print("كلمة المرور:", pw, " (غيّرها بعد أول دخول)")
        print("=" * 50)
    con.commit()
    con.close()


# ----------------------------------------------------------------- مصادقة وصلاحيات
def current_user():
    if "_user" not in g:
        uid = session.get("uid")
        g._user = q("SELECT * FROM users WHERE id=? AND active=1", (uid,), one=True) if uid else None
    return g._user


def has_role(u, *roles):
    return bool(u) and (u["role"] == "admin" or u["role"] in roles)


def login_required(roles=None):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            u = current_user()
            if not u:
                return redirect(url_for("login", next=request.path))
            if roles and not has_role(u, *roles):
                abort(403)
            return f(*a, **k)
        return w
    return deco


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.before_request
def csrf_protect():
    if request.method == "POST":
        tok = session.get("csrf", "")
        if not tok or not secrets.compare_digest(tok, request.form.get("csrf", "")):
            abort(400)


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"
    return resp


@app.context_processor
def inject():
    u = current_user()
    pending = len(pending_for(u)) if u else 0
    unread = q("SELECT COUNT(*) c FROM notifications WHERE user_id=? AND read=0", (u["id"],), one=True)["c"] if u else 0
    return dict(user=u, org=ORG_NAME, ROLES=ROLES, TYPES=TYPES, STEP_KINDS=STEP_KINDS, CATEGORIES=CATEGORIES, unread=unread, STATUS=STATUS, ATT=ATT,
                ACCOUNTS=ACCOUNTS, csrf_token=csrf_token, pending_count=pending,
                can=lambda *r: has_role(u, *r), today=date.today().isoformat())


@app.template_filter("money")
def money(v):
    return "{:,.2f}".format(v or 0)


@app.errorhandler(403)
def e403(_):
    return render_template("msg.html", msg="ليست لديك صلاحية لهذه الصفحة."), 403


@app.errorhandler(404)
def e404(_):
    return render_template("msg.html", msg="الصفحة غير موجودة."), 404


@app.errorhandler(400)
def e400(_):
    return render_template("msg.html", msg="طلب غير صالح أو انتهت الجلسة، أعد المحاولة."), 400


@app.errorhandler(413)
def e413(_):
    return render_template("msg.html", msg="حجم الملف أكبر من 5 ميغابايت."), 413


ATTEMPTS = {}


def too_many(key):
    t = time.time()
    ATTEMPTS[key] = [x for x in ATTEMPTS.get(key, []) if t - x < 300]
    return len(ATTEMPTS[key]) >= 5


# ----------------------------------------------------------------- مساعدات
def num(s, allow_neg=False):
    try:
        v = float(str(s).strip())
    except (ValueError, TypeError):
        return None
    if v != v or v in (float("inf"), float("-inf")) or (v < 0 and not allow_neg):
        return None
    return v


def parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def valid_month(m):
    return bool(m) and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", m) is not None


def stock(item_id):
    r = q("SELECT COALESCE(SUM(qty),0) s FROM stock_moves WHERE item_id=?", (item_id,), one=True)
    return r["s"]


def csv_safe(v):
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


# ----------------------------------------------------------------- الدخول والمستخدمون
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        un = request.form.get("username", "").strip().lower()
        key = (request.remote_addr, un)
        if too_many(key):
            flash("محاولات كثيرة، انتظر 5 دقائق.", "error")
        else:
            u = q("SELECT * FROM users WHERE username=? AND active=1", (un,), one=True)
            if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
                session.clear()
                session.permanent = True
                session["uid"] = u["id"]
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
            ATTEMPTS.setdefault(key, []).append(time.time())
            flash("اسم المستخدم أو كلمة المرور غير صحيحة.", "error")
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
@login_required()
def password():
    u = current_user()
    if request.method == "POST":
        f = request.form
        if not check_password_hash(u["password_hash"], f.get("old", "")):
            flash("كلمة المرور الحالية غير صحيحة.", "error")
        elif len(f.get("new", "")) < 8 or f.get("new") != f.get("new2"):
            flash("كلمة المرور الجديدة 8 أحرف على الأقل ويجب أن تتطابق.", "error")
        else:
            ex("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(f["new"]), u["id"]))
            log("تغيير كلمة المرور")
            flash("تم تغيير كلمة المرور.", "ok")
            return redirect(url_for("dashboard"))
    return render_template("password.html")


@app.route("/users", methods=["GET", "POST"])
@login_required(())
def users():
    if not has_role(current_user(), "admin"):
        abort(403)
    if request.method == "POST":
        f = request.form
        un = f.get("username", "").strip().lower()
        pw = f.get("password", "")
        role = f.get("role")
        if not re.fullmatch(r"[a-z0-9_.]{3,30}", un):
            flash("اسم المستخدم: حروف إنجليزية صغيرة وأرقام (3-30).", "error")
        elif len(pw) < 8:
            flash("كلمة المرور 8 أحرف على الأقل.", "error")
        elif role not in ROLES or not f.get("full_name", "").strip():
            flash("بيانات ناقصة.", "error")
        elif q("SELECT 1 FROM users WHERE username=?", (un,), one=True):
            flash("اسم المستخدم مستخدم مسبقاً.", "error")
        else:
            eid = int(f["employee_id"]) if f.get("employee_id", "").isdigit() else None
            ex("INSERT INTO users(username,password_hash,full_name,role,employee_id) VALUES(?,?,?,?,?)",
               (un, generate_password_hash(pw), f["full_name"].strip(), role, eid))
            log("إنشاء مستخدم " + un)
            flash("تم إنشاء المستخدم.", "ok")
        return redirect(url_for("users"))
    return render_template("users.html",
                           rows=q("SELECT u.*, e.name emp FROM users u LEFT JOIN employees e ON e.id=u.employee_id ORDER BY u.id"),
                           emps=q("SELECT id,name FROM employees WHERE status='active' ORDER BY name"))


@app.post("/users/<int:uid>/toggle")
@login_required(())
def user_toggle(uid):
    u = current_user()
    if not has_role(u, "admin") or uid == u["id"]:
        abort(403)
    ex("UPDATE users SET active=1-active WHERE id=?", (uid,))
    log(f"تفعيل/تعطيل المستخدم #{uid}")
    return redirect(url_for("users"))


@app.post("/users/<int:uid>/reset")
@login_required(())
def user_reset(uid):
    if not has_role(current_user(), "admin"):
        abort(403)
    pw = request.form.get("password", "")
    if len(pw) < 8:
        flash("كلمة المرور 8 أحرف على الأقل.", "error")
    else:
        ex("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), uid))
        log(f"إعادة تعيين كلمة مرور المستخدم #{uid}")
        flash("تمت إعادة التعيين.", "ok")
    return redirect(url_for("users"))


# ----------------------------------------------------------------- لوحة التحكم
@app.route("/")
@login_required()
def dashboard():
    u = current_user()
    s = {}
    emp = q("SELECT * FROM employees WHERE id=?", (u["employee_id"],), one=True) if u["employee_id"] else None
    s["my_open"] = q("SELECT COUNT(*) c FROM requests WHERE requester_id=? AND status='pending'", (u["id"],), one=True)["c"]
    if has_role(u, "hr", "manager"):
        s["emps"] = q("SELECT COUNT(*) c FROM employees WHERE status='active'", one=True)["c"]
    if has_role(u, "store", "manager"):
        s["low"] = len([i for i in inventory_rows() if i["qty"] <= i["min_qty"]])
    if has_role(u, "accountant"):
        m = date.today().strftime("%Y-%m")
        t = q("SELECT kind, SUM(amount) a FROM transactions WHERE day LIKE ? GROUP BY kind", (m + "-%",))
        d = {r["kind"]: r["a"] for r in t}
        s["income"], s["expense"] = d.get("income", 0), d.get("expense", 0)
    mine = q(REQ_SQL + "WHERE r.requester_id=? ORDER BY r.id DESC LIMIT 6", (u["id"],))
    return render_template("dashboard.html", s=s, emp=emp, pending=pending_for(u)[:8], mine=mine)


# ----------------------------------------------------------------- الموظفون
def emp_form(f):
    name = f.get("name", "").strip()
    salary, bal = num(f.get("salary") or 0), num(f.get("leave_balance") or 0)
    if not name:
        return None, "الاسم مطلوب."
    if salary is None or bal is None:
        return None, "الراتب ورصيد الإجازات يجب أن يكونا أرقاماً غير سالبة."
    hd = f.get("hire_date", "")
    if hd and not parse_date(hd):
        return None, "تاريخ التعيين غير صالح."
    return (name, f.get("job_title", "").strip(), f.get("department", "").strip(),
            f.get("phone", "").strip(), hd, salary, bal,
            "inactive" if f.get("status") == "inactive" else "active"), None


@app.route("/employees")
@login_required(("hr", "manager"))
def employees():
    return render_template("employees.html", rows=q("SELECT * FROM employees ORDER BY status, name"))


@app.route("/employees/new", methods=["GET", "POST"])
@app.route("/employees/<int:eid>/edit", methods=["GET", "POST"])
@login_required(("hr",))
def employee_edit(eid=None):
    e = q("SELECT * FROM employees WHERE id=?", (eid,), one=True) if eid else None
    if eid and not e:
        abort(404)
    if request.method == "POST":
        vals, err = emp_form(request.form)
        if err:
            flash(err, "error")
        else:
            if e:
                ex("UPDATE employees SET name=?,job_title=?,department=?,phone=?,hire_date=?,salary=?,leave_balance=?,status=? WHERE id=?",
                   vals + (eid,))
                log(f"تعديل الموظف #{eid}")
            else:
                ex("INSERT INTO employees(name,job_title,department,phone,hire_date,salary,leave_balance,status) VALUES(?,?,?,?,?,?,?,?)", vals)
                log("إضافة موظف " + vals[0])
            flash("تم الحفظ.", "ok")
            return redirect(url_for("employees"))
    return render_template("employee_form.html", e=e)


# ----------------------------------------------------------------- الحضور
@app.route("/attendance", methods=["GET", "POST"])
@login_required(("hr", "manager"))
def attendance():
    day = request.values.get("day") or date.today().isoformat()
    if not parse_date(day):
        day = date.today().isoformat()
    can_edit = has_role(current_user(), "hr")
    if request.method == "POST":
        if not can_edit:
            abort(403)
        for e in q("SELECT id FROM employees WHERE status='active'"):
            st = request.form.get(f"st_{e['id']}")
            if st in ATT:
                db().execute("""INSERT INTO attendance(employee_id,day,status,note) VALUES(?,?,?,?)
                    ON CONFLICT(employee_id,day) DO UPDATE SET status=excluded.status, note=excluded.note""",
                             (e["id"], day, st, request.form.get(f"nt_{e['id']}", "").strip()[:200]))
        db().commit()
        log("حفظ حضور " + day)
        flash("تم حفظ الحضور.", "ok")
        return redirect(url_for("attendance", day=day))
    rows = q("""SELECT e.id, e.name, a.status, a.note FROM employees e
                LEFT JOIN attendance a ON a.employee_id=e.id AND a.day=?
                WHERE e.status='active' ORDER BY e.name""", (day,))
    return render_template("attendance.html", rows=rows, day=day, can_edit=can_edit)


# ----------------------------------------------------------------- الرواتب
@app.route("/payroll", methods=["GET", "POST"])
@login_required(("hr", "accountant"))
def payroll():
    month = request.values.get("month") or date.today().strftime("%Y-%m")
    if not valid_month(month):
        month = date.today().strftime("%Y-%m")
    u = current_user()
    if request.method == "POST":
        if not has_role(u, "hr"):
            abort(403)
        n = 0
        for e in q("SELECT * FROM employees WHERE status='active'"):
            if q("SELECT 1 FROM payroll WHERE month=? AND employee_id=?", (month, e["id"]), one=True):
                continue
            absent = q("SELECT COUNT(*) c FROM attendance WHERE employee_id=? AND day LIKE ? AND status='absent'",
                       (e["id"], month + "-%"), one=True)["c"]
            ded = round(e["salary"] / 30 * absent, 2)
            db().execute("INSERT INTO payroll(month,employee_id,base,allowances,deductions,net,note) VALUES(?,?,?,?,?,?,?)",
                         (month, e["id"], e["salary"], 0, ded, round(e["salary"] - ded, 2),
                          f"خصم {absent} يوم غياب" if absent else ""))
            n += 1
        db().commit()
        log(f"توليد رواتب {month}")
        flash(f"تم توليد {n} كشف راتب.", "ok")
        return redirect(url_for("payroll", month=month))
    rows = q("SELECT p.*, e.name FROM payroll p JOIN employees e ON e.id=p.employee_id WHERE p.month=? ORDER BY e.name", (month,))
    return render_template("payroll.html", rows=rows, month=month,
                           total=sum(r["net"] for r in rows))


@app.post("/payroll/<int:pid>/edit")
@login_required(("hr",))
def payroll_edit(pid):
    p = q("SELECT * FROM payroll WHERE id=?", (pid,), one=True)
    if not p or p["paid"]:
        abort(404)
    al, de = num(request.form.get("allowances") or 0), num(request.form.get("deductions") or 0)
    if al is None or de is None:
        flash("قيم غير صالحة.", "error")
    else:
        ex("UPDATE payroll SET allowances=?, deductions=?, net=?, note=? WHERE id=?",
           (al, de, round(p["base"] + al - de, 2), request.form.get("note", "").strip()[:200], pid))
        log(f"تعديل راتب #{pid}")
    return redirect(url_for("payroll", month=p["month"]))


@app.post("/payroll/<int:pid>/pay")
@login_required(("accountant",))
def payroll_pay(pid):
    p = q("SELECT p.*, e.name FROM payroll p JOIN employees e ON e.id=p.employee_id WHERE p.id=?", (pid,), one=True)
    acc = request.form.get("account")
    if not p or p["paid"] or acc not in ACCOUNTS or p["net"] <= 0:
        abort(400)
    d = db()
    d.execute("UPDATE payroll SET paid=1 WHERE id=?", (pid,))
    d.execute("INSERT INTO transactions(day,kind,category,amount,account,description,user_id) VALUES(?,?,?,?,?,?,?)",
              (date.today().isoformat(), "expense", "رواتب", p["net"], acc,
               f"راتب {p['name']} - {p['month']}", current_user()["id"]))
    d.commit()
    log(f"صرف راتب #{pid}")
    return redirect(url_for("payroll", month=p["month"]))


# ----------------------------------------------------------------- المخزون
def inventory_rows():
    return q("""SELECT i.*, COALESCE(SUM(m.qty),0) qty FROM items i
                LEFT JOIN stock_moves m ON m.item_id=i.id GROUP BY i.id ORDER BY i.name""")


@app.route("/inventory", methods=["GET", "POST"])
@login_required(("store", "manager"))
def inventory():
    u = current_user()
    if request.method == "POST":
        if not has_role(u, "store"):
            abort(403)
        f = request.form
        name, mn = f.get("name", "").strip(), num(f.get("min_qty") or 0)
        if not name or mn is None:
            flash("اسم الصنف مطلوب والحد الأدنى رقم.", "error")
        elif q("SELECT 1 FROM items WHERE name=?", (name,), one=True):
            flash("الصنف موجود مسبقاً.", "error")
        else:
            ex("INSERT INTO items(name,unit,min_qty) VALUES(?,?,?)", (name, f.get("unit", "").strip(), mn))
            log("إضافة صنف " + name)
            flash("تمت إضافة الصنف.", "ok")
        return redirect(url_for("inventory"))
    return render_template("inventory.html", rows=inventory_rows(), can_edit=has_role(u, "store"))


@app.post("/inventory/move")
@login_required(("store",))
def inventory_move():
    f = request.form
    it = q("SELECT * FROM items WHERE id=?", (f.get("item_id", 0),), one=True)
    qty, kind = num(f.get("qty")), f.get("kind")
    if not it or not qty or qty <= 0 or kind not in ("in", "out"):
        flash("بيانات الحركة غير صحيحة.", "error")
    elif kind == "out" and qty > stock(it["id"]):
        flash("الكمية المطلوبة أكبر من الرصيد المتاح.", "error")
    else:
        ex("INSERT INTO stock_moves(item_id,qty,note,user_id,created_at) VALUES(?,?,?,?,?)",
           (it["id"], qty if kind == "in" else -qty, f.get("note", "").strip()[:200], current_user()["id"], now()))
        log(f"حركة مخزون {kind} {qty} {it['name']}")
        flash("تم تسجيل الحركة.", "ok")
    return redirect(url_for("inventory"))


@app.route("/inventory/moves")
@login_required(("store", "manager"))
def inventory_moves():
    rows = q("""SELECT m.*, i.name, u.full_name who FROM stock_moves m JOIN items i ON i.id=m.item_id
                LEFT JOIN users u ON u.id=m.user_id ORDER BY m.id DESC LIMIT 200""")
    return render_template("moves.html", rows=rows)


# ----------------------------------------------------------------- المحاسبة المبسطة
@app.route("/accounting", methods=["GET", "POST"])
@login_required(("accountant",))
def accounting():
    month = request.values.get("month") or date.today().strftime("%Y-%m")
    if not valid_month(month):
        month = date.today().strftime("%Y-%m")
    if request.method == "POST":
        f = request.form
        amt, d = num(f.get("amount")), parse_date(f.get("day"))
        if f.get("kind") not in ("income", "expense") or not amt or amt <= 0 or not d or f.get("account") not in ACCOUNTS:
            flash("بيانات القيد غير صحيحة.", "error")
        else:
            ex("INSERT INTO transactions(day,kind,category,amount,account,description,user_id) VALUES(?,?,?,?,?,?,?)",
               (d.isoformat(), f["kind"], f.get("category", "").strip()[:60], amt, f["account"],
                f.get("description", "").strip()[:200], current_user()["id"]))
            log("قيد مالي " + f["kind"] + " " + str(amt))
            flash("تم تسجيل القيد.", "ok")
        return redirect(url_for("accounting", month=d.strftime("%Y-%m") if d else month))
    rows = q("SELECT * FROM transactions WHERE day LIKE ? ORDER BY day DESC, id DESC", (month + "-%",))
    inc = sum(r["amount"] for r in rows if r["kind"] == "income")
    exp = sum(r["amount"] for r in rows if r["kind"] == "expense")
    cats = q("""SELECT kind, COALESCE(NULLIF(category,''),'بدون تصنيف') c, SUM(amount) a FROM transactions
                WHERE day LIKE ? GROUP BY kind, c ORDER BY a DESC""", (month + "-%",))
    bal = q("""SELECT account, SUM(CASE kind WHEN 'income' THEN amount ELSE -amount END) b
               FROM transactions GROUP BY account""")
    return render_template("accounting.html", rows=rows, month=month, inc=inc, exp=exp, cats=cats,
                           bal={r["account"]: r["b"] for r in bal})


@app.route("/accounting/export")
@login_required(("accountant",))
def accounting_export():
    month = request.args.get("month", "")
    if not valid_month(month):
        abort(400)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["التاريخ", "النوع", "التصنيف", "المبلغ", "الحساب", "البيان"])
    for r in q("SELECT * FROM transactions WHERE day LIKE ? ORDER BY day, id", (month + "-%",)):
        w.writerow([r["day"], "إيراد" if r["kind"] == "income" else "مصروف", csv_safe(r["category"]),
                    r["amount"], r["account"], csv_safe(r["description"])])
    data = io.BytesIO(out.getvalue().encode("utf-8-sig"))
    return send_file(data, mimetype="text/csv", as_attachment=True, download_name=f"accounts-{month}.csv")


@app.post("/accounting/<int:tid>/delete")
@login_required(())
def transaction_delete(tid):
    if not has_role(current_user(), "admin"):
        abort(403)
    t = q("SELECT * FROM transactions WHERE id=?", (tid,), one=True)
    if not t:
        abort(404)
    ex("DELETE FROM transactions WHERE id=?", (tid,))
    log(f"حذف قيد #{tid} ({t['kind']} {t['amount']})")
    return redirect(url_for("accounting", month=t["day"][:7]))


# ----------------------------------------------------------------- الطلبات والتوقيع الإلكتروني
REQ_SQL = """SELECT r.*, u.full_name requester, u.role requester_role,
 (SELECT role FROM request_steps s WHERE s.request_id=r.id AND s.status='pending'
  ORDER BY step_no LIMIT 1) cur_role,
 (SELECT kind FROM request_steps s WHERE s.request_id=r.id AND s.status='pending'
  ORDER BY step_no LIMIT 1) cur_kind
 FROM requests r JOIN users u ON u.id=r.requester_id """


def req_status(r):
    """(نص الحالة، لون المؤشر) كما في مؤشرات ERPNext."""
    if r["status"] == "pending":
        if r["cur_kind"] == "disburse":
            return "بانتظار الصرف", "orange"
        if r["cur_kind"] == "ack":
            return "بانتظار اعتماد الإدارة", "blue"
        return "قيد الموافقة", "orange"
    return {"approved": ("مكتمل ومختوم" if r["seal_code"] else "معتمد", "green"),
            "rejected": ("مرفوض", "red"), "cancelled": ("ملغي", "gray")}[r["status"]]


app.jinja_env.globals["req_status"] = req_status


def can_act(u, r):
    """المسؤول يستطيع التوقيع بالإنابة عن أي خطوة؛ غيره بحسب دوره وليس على طلبه."""
    if r["status"] != "pending" or not r["cur_role"]:
        return False
    if u["role"] == "admin":
        return True
    return r["cur_role"] == u["role"] and r["requester_id"] != u["id"]


def pending_for(u):
    if not u:
        return []
    rows = q(REQ_SQL + "WHERE r.status='pending' ORDER BY r.id DESC")
    return [r for r in rows if can_act(u, r)]


def can_view(u, r):
    if r["requester_id"] == u["id"] or has_role(u, "manager", "hr"):
        return True
    return bool(q("SELECT 1 FROM request_steps WHERE request_id=? AND role=?", (r["id"], u["role"]), one=True))


def notify(user_ids, text, url):
    d = db()
    for uid in set(user_ids):
        d.execute("INSERT INTO notifications(user_id,text,url,created_at) VALUES(?,?,?,?)", (uid, text, url, now()))


def notify_role(role, text, url, exclude=None):
    ids = [x["id"] for x in q("SELECT id FROM users WHERE role=? AND active=1", (role,)) if x["id"] != exclude]
    if not ids and role != "admin":
        ids = [x["id"] for x in q("SELECT id FROM users WHERE role='admin' AND active=1") if x["id"] != exclude]
        text += " (بالإنابة)"
    notify(ids, text, url)


def build_steps(typ, u):
    chain = TYPES[typ]["chain"]
    if u["role"] == "admin":
        steps = [s for s in chain if s[1] != "approve"]
    else:
        steps = [s for s in chain if not (s[1] == "approve" and s[0] == u["role"])]
    return steps or [("admin", "approve")]


# ---- البصمات التشفيرية (سلسلة توقيعات لا تقبل التعديل دون كشفها)
def fingerprint(r):
    keys = ("type", "category", "title", "details", "amount", "date_from", "date_to",
            "days", "item_id", "qty", "requester_id", "created_at")
    return hashlib.sha256(json.dumps([r[k] for k in keys], ensure_ascii=False, default=str).encode()).hexdigest()


def genesis(rid, fp):
    return hashlib.sha256(f"REQ|{rid}|{fp}".encode()).hexdigest()


def paid_extra(r):
    return f"{r['paid_amount']}|{r['paid_account']}|{r['paid_ref']}|{r['paid_date']}"


def chain_hash(prev, fp, step_no, role, kind, actor_id, actor_name, acted_at, note, sig_hash, extra):
    s = "|".join(map(str, [prev, fp, step_no, role, kind, actor_id, actor_name, acted_at, note, sig_hash, extra]))
    return hashlib.sha256(s.encode()).hexdigest()


def verify_chain(rid):
    """True سليمة / False معدّلة / None لا توجد توقيعات مسجلة."""
    r = q("SELECT * FROM requests WHERE id=?", (rid,), one=True)
    steps = q("""SELECT * FROM request_steps WHERE request_id=? AND status='approved'
                 AND chain_hash IS NOT NULL ORDER BY step_no""", (rid,))
    if not r or not steps:
        return None
    fp = fingerprint(r)
    prev = genesis(rid, fp)
    for s in steps:
        if hashlib.sha256((s["sig"] or "").encode()).hexdigest() != s["sig_hash"]:
            return False
        extra = paid_extra(r) if s["kind"] == "disburse" else ""
        h = chain_hash(prev, fp, s["step_no"], s["role"], s["kind"], s["acted_by"], s["actor_name"],
                       s["acted_at"], s["note"] or "", s["sig_hash"], extra)
        if h != s["chain_hash"]:
            return False
        prev = h
    return True


def clean_signature(data):
    prefix = "data:image/png;base64,"
    if not data.startswith(prefix) or len(data) > 400000:
        raise ValueError("توقيع غير صالح.")
    try:
        im = Image.open(io.BytesIO(base64.b64decode(data[len(prefix):], validate=True)))
        im.load()
        im = im.convert("RGBA")
    except Exception:
        raise ValueError("توقيع غير صالح.")
    if im.getchannel("A").getbbox() is None:
        raise ValueError("ارسم توقيعك أولاً.")
    im.thumbnail((500, 200))
    out = io.BytesIO()
    im.save(out, "PNG")
    return prefix + base64.b64encode(out.getvalue()).decode()


@app.route("/signature", methods=["GET", "POST"])
@login_required()
def signature():
    u = current_user()
    nxt = request.args.get("next", "")
    nxt = nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard")
    if request.method == "POST":
        try:
            png = clean_signature(request.form.get("data", ""))
        except ValueError as e:
            flash(str(e), "error")
        else:
            ex("UPDATE users SET signature=? WHERE id=?", (png, u["id"]))
            log("تسجيل التوقيع الإلكتروني")
            flash("تم حفظ توقيعك الإلكتروني.", "ok")
            return redirect(nxt)
    return render_template("signature.html", nxt=nxt)


# ---- الإشعارات والبحث
@app.route("/notifications")
@login_required()
def notifications():
    u = current_user()
    rows = q("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 100", (u["id"],))
    resp = render_template("notifications.html", rows=rows)
    ex("UPDATE notifications SET read=1 WHERE user_id=? AND read=0", (u["id"],))
    return resp


@app.route("/search")
@login_required()
def search():
    scope = "all" if has_role(current_user(), "manager", "hr") else "mine"
    return redirect(url_for("requests_list", q=request.args.get("q", "").strip(), scope=scope))


# ---- قائمة الطلبات
@app.route("/requests")
@login_required()
def requests_list():
    u = current_user()
    scope = request.args.get("scope", "mine")
    typ, st, text = request.args.get("type", ""), request.args.get("status", ""), request.args.get("q", "").strip()
    where, args = [], []
    if not (scope == "all" and has_role(u, "manager", "hr")):
        scope = "mine"
        where.append("r.requester_id=?")
        args.append(u["id"])
    if typ in TYPES:
        where.append("r.type=?")
        args.append(typ)
    if st in STATUS:
        where.append("r.status=?")
        args.append(st)
    if text:
        where.append("(r.title LIKE ? OR CAST(r.id AS TEXT)=?)")
        args += [f"%{text}%", text.lstrip("#")]
    rows = q(REQ_SQL + ("WHERE " + " AND ".join(where) if where else "") + " ORDER BY r.id DESC LIMIT 300", args)
    return render_template("requests.html", rows=rows, scope=scope, typ=typ, st=st, text=text,
                           pending=pending_for(u))


@app.route("/requests/new", methods=["GET", "POST"])
@login_required()
def request_new():
    u = current_user()
    typ = request.values.get("type", "leave")
    if typ not in TYPES or TYPES[typ].get("hidden"):
        abort(404)
    items = q("SELECT * FROM items ORDER BY name")
    emp = q("SELECT * FROM employees WHERE id=?", (u["employee_id"],), one=True) if u["employee_id"] else None
    if request.method == "POST":
        f = request.form
        title = (f.get("title", "").strip() or TYPES[typ]["label"])[:200]
        details = f.get("details", "").strip()[:4000]
        amount = days = qty = item_id = d1s = d2s = category = None
        err = None
        if typ == "financial":
            amount = num(f.get("amount"))
            category = f.get("category")
            if not amount or amount <= 0:
                err = "المبلغ مطلوب ويجب أن يكون أكبر من صفر."
            elif category not in CATEGORIES:
                err = "اختر تصنيف الطلب المالي."
        elif typ == "leave":
            d1, d2 = parse_date(f.get("date_from")), parse_date(f.get("date_to"))
            if not emp:
                err = "حسابك غير مرتبط بملف موظف، راجع مدير النظام."
            elif not d1 or not d2 or d2 < d1:
                err = "تواريخ الإجازة غير صحيحة."
            else:
                days = (d2 - d1).days + 1
                if days > 365:
                    err = "المدة طويلة جداً."
                elif days > emp["leave_balance"]:
                    err = f"المدة ({days} يوم) أكبر من رصيدك ({emp['leave_balance']:g})."
                d1s, d2s = d1.isoformat(), d2.isoformat()
        elif typ == "exit":
            d1 = parse_date(f.get("date_from"))
            if not d1:
                err = "تاريخ الإذن مطلوب."
            else:
                d1s = d1.isoformat()
        elif typ == "material" and f.get("item_id"):
            it = q("SELECT id FROM items WHERE id=?", (f.get("item_id"),), one=True)
            qty = num(f.get("qty"))
            if not it or not qty or qty <= 0:
                err = "اختر صنفاً صحيحاً وكمية أكبر من صفر."
            else:
                item_id = it["id"]
        if not err and typ != "leave" and not details and not item_id:
            err = "اكتب تفاصيل الطلب."
        att = att_name = None
        file = request.files.get("file")
        if not err and file and file.filename:
            ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
            if ext not in ALLOWED_EXT:
                err = "نوع المرفق غير مسموح (pdf, png, jpg, docx, xlsx)."
            else:
                att = uuid.uuid4().hex + "." + ext
                att_name = os.path.basename(file.filename)[:100]
        if err:
            flash(err, "error")
        else:
            if att:
                file.save(os.path.join(UPLOADS, att))
            steps = build_steps(typ, u)
            d = db()
            rid = d.execute("""INSERT INTO requests(type,category,requester_id,title,details,amount,date_from,date_to,days,item_id,qty,attachment,attachment_name,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (typ, category, u["id"], title, details, amount, d1s, d2s, days, item_id, qty, att, att_name, now())).lastrowid
            for i, (role, kind) in enumerate(steps, 1):
                d.execute("INSERT INTO request_steps(request_id,step_no,role,kind) VALUES(?,?,?,?)", (rid, i, role, kind))
            notify_role(steps[0][0], f"طلب جديد #{rid} من {u['full_name']}: {title}", url_for("request_view", rid=rid), u["id"])
            d.commit()
            log(f"طلب جديد #{rid} ({TYPES[typ]['label']})")
            flash("تم إرسال الطلب.", "ok")
            return redirect(url_for("request_view", rid=rid))
    return render_template("request_new.html", typ=typ, items=items, emp=emp)


def get_request(rid):
    r = q(REQ_SQL + "WHERE r.id=?", (rid,), one=True)
    if not r:
        abort(404)
    if not can_view(current_user(), r):
        abort(403)
    return r


def steps_of(rid):
    return q("""SELECT s.*, u.full_name actor FROM request_steps s LEFT JOIN users u ON u.id=s.acted_by
                WHERE s.request_id=? ORDER BY s.step_no""", (rid,))


@app.route("/requests/<int:rid>")
@login_required()
def request_view(rid):
    u, r = current_user(), get_request(rid)
    item = q("SELECT * FROM items WHERE id=?", (r["item_id"],), one=True) if r["item_id"] else None
    paid_by = q("SELECT full_name FROM users WHERE id=?", (r["paid_by"],), one=True) if r["paid_by"] else None
    acting = can_act(u, r)
    return render_template("request_view.html", r=r, steps=steps_of(rid), item=item, can_act=acting,
                           is_owner=r["requester_id"] == u["id"], paid_by=paid_by,
                           chain_ok=verify_chain(rid), needs_sig=acting and not u["signature"],
                           sealed_ok=bool(r["seal_code"]) and os.path.exists(os.path.join(SEALED, f"{rid}.pdf")))


@app.route("/requests/<int:rid>/print")
@login_required()
def request_print(rid):
    r = get_request(rid)
    item = q("SELECT * FROM items WHERE id=?", (r["item_id"],), one=True) if r["item_id"] else None
    return render_template("request_print.html", r=r, steps=steps_of(rid), item=item)


@app.route("/requests/<int:rid>/file")
@login_required()
def request_file(rid):
    r = get_request(rid)
    if not r["attachment"]:
        abort(404)
    return send_from_directory(UPLOADS, r["attachment"], as_attachment=True,
                               download_name=r["attachment_name"] or r["attachment"])


@app.route("/requests/<int:rid>/sealed")
@login_required()
def request_sealed(rid):
    r = get_request(rid)
    path = os.path.join(SEALED, f"{rid}.pdf")
    if not r["seal_code"] or not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="application/pdf", as_attachment=True,
                     download_name=f"request-{rid}-sealed.pdf")


@app.post("/requests/<int:rid>/reseal")
@login_required()
def request_reseal(rid):
    u, r = current_user(), get_request(rid)
    if not has_role(u, "admin") or r["status"] != "approved":
        abort(403)
    if os.path.exists(os.path.join(SEALED, f"{rid}.pdf")) and r["seal_code"]:
        flash("الملف المختوم موجود مسبقاً.", "error")
    else:
        try:
            seal_request(rid)
            flash("تم توليد الملف المختوم.", "ok")
        except Exception as e:  # noqa
            flash(f"تعذر توليد الملف المختوم: {e}", "error")
    return redirect(url_for("request_view", rid=rid))


@app.post("/requests/<int:rid>/cancel")
@login_required()
def request_cancel(rid):
    u, r = current_user(), get_request(rid)
    if r["requester_id"] != u["id"] or r["status"] != "pending":
        abort(403)
    d = db()
    d.execute("UPDATE requests SET status='cancelled' WHERE id=?", (rid,))
    d.execute("UPDATE request_steps SET status='cancelled' WHERE request_id=? AND status='pending'", (rid,))
    d.commit()
    log(f"إلغاء طلب #{rid}")
    return redirect(url_for("request_view", rid=rid))


def finalize_check(r):
    """فحوص قبل الاعتماد النهائي. تعيد رسالة خطأ أو None."""
    if r["type"] == "leave" and r["days"]:
        e = q("SELECT e.leave_balance b FROM users u JOIN employees e ON e.id=u.employee_id WHERE u.id=?",
              (r["requester_id"],), one=True)
        if not e or e["b"] < r["days"]:
            return "رصيد إجازات الموظف لا يكفي الآن."
    if r["type"] == "material" and r["item_id"] and r["qty"] > stock(r["item_id"]):
        return "الرصيد المتاح في المخزن أقل من الكمية المطلوبة."
    return None


def apply_effects(r, actor):
    d = db()
    if r["type"] == "leave" and r["days"]:
        e = q("SELECT employee_id FROM users WHERE id=?", (r["requester_id"],), one=True)
        if e and e["employee_id"]:
            d.execute("UPDATE employees SET leave_balance=leave_balance-? WHERE id=?", (r["days"], e["employee_id"]))
            d1 = parse_date(r["date_from"])
            for i in range(int(r["days"])):
                d.execute("""INSERT INTO attendance(employee_id,day,status,note) VALUES(?,?,'leave',?)
                    ON CONFLICT(employee_id,day) DO UPDATE SET status='leave', note=excluded.note""",
                          (e["employee_id"], (d1 + timedelta(days=i)).isoformat(), f"إجازة معتمدة #{r['id']}"))
    elif r["type"] == "advance":  # طلبات قديمة (قبل الطلب المالي الموحد)
        d.execute("INSERT INTO transactions(day,kind,category,amount,account,description,user_id,request_id) VALUES(?,?,?,?,?,?,?,?)",
                  (date.today().isoformat(), "expense", "سلف", r["amount"], ACCOUNTS[0],
                   f"سلفة {r['requester']} - طلب #{r['id']}", actor["id"], r["id"]))
    elif r["type"] == "material" and r["item_id"]:
        d.execute("INSERT INTO stock_moves(item_id,qty,note,user_id,created_at) VALUES(?,?,?,?,?)",
                  (r["item_id"], -r["qty"], f"صرف حسب الطلب #{r['id']}", actor["id"], now()))


def seal_request(rid):
    """يولّد الملف PDF المختوم بعد اكتمال كل التوقيعات ويسجّل رمز الختم وبصمته."""
    r = q(REQ_SQL + "WHERE r.id=?", (rid,), one=True)
    steps = q("SELECT * FROM request_steps WHERE request_id=? AND status='approved' ORDER BY step_no", (rid,))
    if not r or r["status"] != "approved" or not steps:
        raise ValueError("الطلب غير مكتمل")
    code = r["seal_code"] or "-".join(secrets.token_hex(2).upper() for _ in range(3))
    stamp_time = r["sealed_at"] or now()
    seal_hash = steps[-1]["chain_hash"] or genesis(rid, fingerprint(r))
    base = os.environ.get("BASE_URL") or request.host_url
    verify_url = base.rstrip("/") + "/verify?code=" + code
    item = q("SELECT * FROM items WHERE id=?", (r["item_id"],), one=True) if r["item_id"] else None
    label = TYPES[r["type"]]["label"]
    rows = [("رقم الطلب", f"#{rid}"), ("نوع الطلب", label),
            ("مقدم الطلب", f"{r['requester']} ({ROLES[r['requester_role']]})"),
            ("تاريخ التقديم", r["created_at"]), ("العنوان", r["title"])]
    if r["category"]:
        rows.append(("التصنيف", r["category"]))
    if r["amount"]:
        rows.append(("المبلغ المطلوب", money(r["amount"])))
    if r["date_from"]:
        rows.append(("الفترة", r["date_from"] + (f" إلى {r['date_to']} ({r['days']:g} يوم)" if r["date_to"] else "")))
    if item:
        rows.append(("الصنف", f"{item['name']} × {r['qty']:g}"))
    rows.append(("التفاصيل", r["details"] or "-"))
    paid_rows = None
    if r["paid_amount"]:
        payer = q("SELECT full_name FROM users WHERE id=?", (r["paid_by"],), one=True)
        paid_rows = [("المبلغ المصروف", money(r["paid_amount"])), ("الحساب", r["paid_account"]),
                     ("رقم المرجع / الإيصال", r["paid_ref"] or "-"), ("تاريخ الصرف", r["paid_date"]),
                     ("صُرف بواسطة", payer["full_name"] if payer else "-")]
    signers = [dict(title=f"{ROLES[s['role']]} - {STEP_KINDS[s['kind'] or 'approve']}", name=s["actor_name"] or "",
                    when=s["acted_at"], note=s["note"] or "", signature=data_url_to_png(s["sig"]),
                    on_behalf=bool(s["on_behalf"]), hash=s["chain_hash"] or "") for s in steps]
    path = os.path.join(SEALED, f"{rid}.pdf")
    build_sealed_pdf(path, ORG_NAME, f"{label} رقم {rid}", code, verify_url, rows, paid_rows, signers,
                     seal_hash, stamp_time, os.path.join(DATA, "stamp.png"))
    with open(path, "rb") as fh:
        pdf_hash = hashlib.sha256(fh.read()).hexdigest()
    ex("UPDATE requests SET seal_code=?, seal_hash=?, sealed_at=?, sealed_pdf_hash=? WHERE id=?",
       (code, seal_hash, stamp_time, pdf_hash, rid))
    return code


@app.post("/requests/<int:rid>/act")
@login_required()
def request_act(rid):
    u, r = current_user(), get_request(rid)
    if not can_act(u, r):
        abort(403)
    f = request.form
    action, note = f.get("action"), f.get("note", "").strip()[:500]
    step = q("SELECT * FROM request_steps WHERE request_id=? AND status='pending' ORDER BY step_no LIMIT 1", (rid,), one=True)
    back = redirect(url_for("request_view", rid=rid))
    d = db()
    if action == "reject":
        if not note:
            flash("اكتب سبب الرفض.", "error")
            return back
        d.execute("""UPDATE request_steps SET status='rejected',acted_by=?,acted_at=?,note=?,actor_name=?,actor_role=?,on_behalf=?
                     WHERE id=?""", (u["id"], now(), note, u["full_name"], u["role"], int(u["role"] != step["role"]), step["id"]))
        d.execute("UPDATE requests SET status='rejected' WHERE id=?", (rid,))
        d.execute("UPDATE request_steps SET status='cancelled' WHERE request_id=? AND status='pending'", (rid,))
        notify([r["requester_id"]], f"تم رفض طلبك #{rid}: {note}", url_for("request_view", rid=rid))
        d.commit()
        log(f"رفض الطلب #{rid}")
        flash("تم تسجيل الرفض.", "ok")
        return redirect(url_for("requests_list"))
    if action != "approve":
        abort(400)
    # --- التوقيع الإلكتروني: كلمة المرور + صورة التوقيع المسجلة
    key = (request.remote_addr, "sign", u["id"])
    if too_many(key):
        flash("محاولات كثيرة، انتظر 5 دقائق.", "error")
        return back
    if not check_password_hash(u["password_hash"], f.get("password", "")):
        ATTEMPTS.setdefault(key, []).append(time.time())
        flash("كلمة المرور غير صحيحة، لم يتم التوقيع.", "error")
        return back
    if not u["signature"]:
        flash("سجّل توقيعك الإلكتروني أولاً ثم أعد المحاولة.", "error")
        return redirect(url_for("signature", next=url_for("request_view", rid=rid)))
    remaining = q("SELECT COUNT(*) c FROM request_steps WHERE request_id=? AND status='pending' AND id<>?",
                  (rid, step["id"]), one=True)["c"]
    paid = None
    if step["kind"] == "disburse":
        amt, acc, pdt = num(f.get("paid_amount")), f.get("paid_account"), parse_date(f.get("paid_date"))
        if not amt or amt <= 0 or acc not in ACCOUNTS or not pdt:
            flash("أكمل بيانات الصرف: المبلغ والحساب والتاريخ.", "error")
            return back
        paid = (amt, acc, f.get("paid_ref", "").strip()[:60], pdt.isoformat())
    if remaining == 0:
        err = finalize_check(r)
        if err:
            flash(err, "error")
            return back
    at, sig_hash = now(), hashlib.sha256(u["signature"].encode()).hexdigest()
    if paid:
        d.execute("UPDATE requests SET paid_amount=?,paid_account=?,paid_ref=?,paid_date=?,paid_by=? WHERE id=?",
                  paid + (u["id"], rid))
        d.execute("INSERT INTO transactions(day,kind,category,amount,account,description,user_id,request_id) VALUES(?,?,?,?,?,?,?,?)",
                  (paid[3], "expense", r["category"] or TYPES[r["type"]]["label"], paid[0], paid[1],
                   f"صرف الطلب #{rid} - {r['requester']}: {r['title']}"[:200], u["id"], rid))
    r2 = q("SELECT * FROM requests WHERE id=?", (rid,), one=True)
    fp = fingerprint(r2)
    last = q("SELECT chain_hash FROM request_steps WHERE request_id=? AND status='approved' AND chain_hash IS NOT NULL ORDER BY step_no DESC LIMIT 1",
             (rid,), one=True)
    prev = last["chain_hash"] if last else genesis(rid, fp)
    ch = chain_hash(prev, fp, step["step_no"], step["role"], step["kind"], u["id"], u["full_name"], at, note,
                    sig_hash, paid_extra(r2) if paid else "")
    d.execute("""UPDATE request_steps SET status='approved',acted_by=?,acted_at=?,note=?,sig=?,sig_hash=?,chain_hash=?,
                 ip=?,actor_name=?,actor_role=?,on_behalf=? WHERE id=?""",
              (u["id"], at, note, u["signature"], sig_hash, ch, request.remote_addr, u["full_name"], u["role"],
               int(u["role"] != step["role"]), step["id"]))
    url = url_for("request_view", rid=rid)
    sealed = None
    if remaining == 0:
        d.execute("UPDATE requests SET status='approved' WHERE id=?", (rid,))
        apply_effects(r, u)
    d.commit()
    if remaining == 0:
        try:
            sealed = seal_request(rid)
        except Exception as e:  # noqa
            flash(f"اكتمل الطلب لكن تعذر توليد الملف المختوم ({e}). يمكن للمسؤول إعادة توليده.", "error")
        notify([r["requester_id"]], f"اكتمل طلبك #{rid} وتم ختمه إلكترونياً" + (f" - رمز الختم {sealed}" if sealed else ""), url)
    else:
        nxt = q("SELECT role, kind FROM request_steps WHERE request_id=? AND status='pending' ORDER BY step_no LIMIT 1", (rid,), one=True)
        if paid:
            notify([r["requester_id"]], f"تم صرف مبلغ {money(paid[0])} لطلبك #{rid}", url)
            notify_role(nxt["role"], f"تم الصرف للطلب #{rid} ({money(paid[0])}) وبانتظار اعتماد الإدارة", url, u["id"])
        else:
            notify_role(nxt["role"], f"طلب #{rid} بانتظار {STEP_KINDS[nxt['kind']]}: {r['title']}", url, u["id"])
    d.commit()
    log(f"توقيع واعتماد الطلب #{rid} ({STEP_KINDS[step['kind']]})")
    flash("تم التوقيع وتسجيل قرارك." + (f" الملف المختوم جاهز - رمز الختم {sealed}" if sealed else ""), "ok")
    return redirect(url_for("request_view", rid=rid) if sealed else url_for("requests_list"))


# ---- التحقق العام من الملف المختوم (بلا تسجيل دخول)
@app.route("/verify", methods=["GET", "POST"])
def verify():
    code = (request.values.get("code") or "").strip().upper()[:20]
    r = q(REQ_SQL + "WHERE r.seal_code=?", (code,), one=True) if code else None
    file_ok = None
    if r and request.method == "POST":
        up = request.files.get("file")
        if up and up.filename:
            file_ok = hashlib.sha256(up.read()).hexdigest() == r["sealed_pdf_hash"]
    steps = q("SELECT * FROM request_steps WHERE request_id=? AND status='approved' ORDER BY step_no", (r["id"],)) if r else []
    return render_template("verify.html", r=r, code=code, steps=steps, file_ok=file_ok,
                           chain_ok=verify_chain(r["id"]) if r else None)


# ----------------------------------------------------------------- إدارة النظام
@app.route("/audit")
@login_required(())
def audit():
    if not has_role(current_user(), "admin"):
        abort(403)
    rows = q("SELECT a.*, u.full_name who FROM audit a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 300")
    return render_template("audit.html", rows=rows)


@app.post("/backup")
@login_required(())
def backup():
    if not has_role(current_user(), "admin"):
        abort(403)
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    dst = sqlite3.connect(path)
    db().backup(dst)
    dst.close()
    with open(path, "rb") as f:
        data = io.BytesIO(f.read())
    os.remove(path)
    log("نسخة احتياطية")
    return send_file(data, as_attachment=True, download_name=f"backup-{date.today().isoformat()}.db")


init_db()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 8000)))
