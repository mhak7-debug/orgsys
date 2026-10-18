# -*- coding: utf-8 -*-
"""نقل البيانات من SQLite (data/org.db) والملفات (data/uploads, data/sealed) إلى Postgres.

الاستخدام:
    DATABASE_URL=postgresql://... python migrate_to_postgres.py [مسار-org.db] [--force]

يرفض التنفيذ إن كانت قاعدة Postgres تحتوي بيانات (أكثر من مستخدم admin الافتراضي) إلا مع --force،
وعندها تُستبدل كل بيانات Postgres ببيانات SQLite.
"""
import os
import sqlite3
import sys

args = [a for a in sys.argv[1:] if not a.startswith("--")]
FORCE = "--force" in sys.argv
SRC = os.path.abspath(args[0] if args else os.path.join(os.path.dirname(__file__), "data", "org.db"))
SRC_DIR = os.path.dirname(SRC)

if not (os.environ.get("DATABASE_URL") or "").startswith(("postgres://", "postgresql://")):
    sys.exit("عيّن DATABASE_URL لقاعدة Postgres أولاً.")
if not os.path.isfile(SRC):
    sys.exit(f"الملف غير موجود: {SRC}")

import app as A  # noqa: E402  (ينشئ الجداول في Postgres)

TABLES = ["employees", "users", "attendance", "payroll", "items", "stock_moves", "transactions",
          "requests", "request_steps", "notifications", "audit"]

src = sqlite3.connect(SRC)
src.row_factory = sqlite3.Row
dst = A.connect()

existing = sum(dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES)
if existing > 1 and not FORCE:
    sys.exit(f"قاعدة Postgres تحتوي {existing} سجلاً. أعد التشغيل مع --force لاستبدالها.")

dst.execute("TRUNCATE " + ", ".join(TABLES) + ", files RESTART IDENTITY CASCADE")
for t in TABLES:
    dst_cols = {r["column_name"] for r in dst.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name=? AND table_schema=current_schema()", (t,)).fetchall()}
    rows = src.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
    if not rows:
        print(f"{t}: 0")
        continue
    cols = [c for c in rows[0].keys() if c in dst_cols]
    sql = f"INSERT INTO {t}({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
    for r in rows:
        dst.execute(sql, [r[c] for c in cols])
    dst.execute(f"SELECT setval(pg_get_serial_sequence('{t}','id'), (SELECT COALESCE(MAX(id),1) FROM {t}))")
    print(f"{t}: {len(rows)}")

n = 0
for sub in ("uploads", "sealed"):
    folder = os.path.join(SRC_DIR, sub)
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        with open(os.path.join(folder, name), "rb") as f:
            dst.execute("INSERT INTO files(name,data,created_at) VALUES(?,?,?)", (f"{sub}/{name}", f.read(), A.now()))
        n += 1
stamp = os.path.join(SRC_DIR, "stamp.png")
if os.path.isfile(stamp):
    with open(stamp, "rb") as f:
        dst.execute("INSERT INTO files(name,data,created_at) VALUES(?,?,?)", ("stamp.png", f.read(), A.now()))
    n += 1
print(f"files: {n}")

dst.commit()
dst.close()
print("تم النقل بنجاح.")
