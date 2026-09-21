"""Verify .env loading and the Super-Admin-once-only bootstrap semantics.

    python test_env_config.py
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = r"C:\Users\user\Downloads\Crainbow_CBT_Phase3H_Live_Messages_Fix\crainbow"
DB1 = os.path.join(HERE, "envtest1.db")
DB2 = os.path.join(HERE, "envtest2.db")
for p in (DB1, DB2):
    if os.path.exists(p):
        os.remove(p)

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def run(code, env_overrides=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if env_overrides:
        env.update(env_overrides)
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p


# ---- 1. .env is actually loaded: BRIGHTSTARS_SECRET from .env reaches app.secret_key
r = run(
    "import app; print('SECRET_LEN=' + str(len(app.app.secret_key)))",
    {"CRAINBOW_DB": DB1},
)
check(".env loads (BRIGHTSTARS_SECRET applied)", "SECRET_LEN=32" in r.stdout or "SECRET_LEN=64" in r.stdout,
      r.stdout.strip() + " " + r.stderr[-300:])

# ---- 2. An env var already set beats .env (load_dotenv must not override=True)
r = run(
    "import app; print('ENV=' + app.ENVIRONMENT)",
    {"CRAINBOW_DB": DB1, "BRIGHTSTARS_ENV": "production_probe"},
)
check("existing env var overrides .env", "ENV=production_probe" in r.stdout, r.stdout + r.stderr[-300:])

# ---- 3. Fresh database: .env's School Admin username/password are used
r = run(
    "import app\n"
    "with app.app.app_context(): app.init_db()\n"
    "from models import Admin, AdminType, db\n"
    "from sqlalchemy import select\n"
    "with app.app.app_context():\n"
    "    a = db.session.scalars(select(Admin).join(AdminType, AdminType.id==Admin.admin_type_id).where(AdminType.is_system==1)).first()\n"
    "    print('USERNAME=' + a.username)\n"
    "    from werkzeug.security import check_password_hash\n"
    "    print('PW_MATCHES=' + str(check_password_hash(a.password_hash, 'FreshDbPass!2345')))\n",
    {"CRAINBOW_DB": DB1, "CRAINBOW_SUPERADMIN_USERNAME": "customroot",
     "CRAINBOW_ADMIN_PASSWORD": "FreshDbPass!2345"},
)
check("fresh DB uses .env-style username override", "USERNAME=customroot" in r.stdout, r.stdout + r.stderr[-500:])
check("fresh DB uses .env-style password override", "PW_MATCHES=True" in r.stdout, r.stdout + r.stderr[-500:])

# ---- 4. Existing database: changing the env vars must NOT create a 2nd admin
#         or touch the existing one.
r = run(
    "import app\n"
    "with app.app.app_context(): app.init_db()\n"
    "from models import Admin, AdminType, db\n"
    "from sqlalchemy import select, func\n"
    "with app.app.app_context():\n"
    "    total = db.session.scalar(select(func.count()).select_from(Admin))\n"
    "    supers = db.session.scalar(select(func.count()).select_from(Admin).join(AdminType, AdminType.id==Admin.admin_type_id).where(AdminType.is_system==1))\n"
    "    a = db.session.scalars(select(Admin).join(AdminType, AdminType.id==Admin.admin_type_id).where(AdminType.is_system==1)).first()\n"
    "    print(f'TOTAL={total} SUPERS={supers} USERNAME={a.username}')\n"
    "    from werkzeug.security import check_password_hash\n"
    "    print('OLD_PW_STILL_MATCHES=' + str(check_password_hash(a.password_hash, 'FreshDbPass!2345')))\n"
    "    print('NEW_PW_DOES_NOT_MATCH=' + str(not check_password_hash(a.password_hash, 'DifferentPass!999')))\n",
    {"CRAINBOW_DB": DB1, "CRAINBOW_SUPERADMIN_USERNAME": "attacker_or_typo",
     "CRAINBOW_ADMIN_PASSWORD": "DifferentPass!999"},
)
check("second init_db() creates no 2nd admin", "TOTAL=1" in r.stdout, r.stdout + r.stderr[-500:])
check("second init_db() creates no 2nd School Admin", "SUPERS=1" in r.stdout, r.stdout + r.stderr[-500:])
check("existing School Admin username unchanged", "USERNAME=customroot" in r.stdout, r.stdout + r.stderr[-500:])
check("existing School Admin password unchanged", "OLD_PW_STILL_MATCHES=True" in r.stdout, r.stdout + r.stderr[-500:])
check("new .env password was NOT applied", "NEW_PW_DOES_NOT_MATCH=True" in r.stdout, r.stdout + r.stderr[-500:])

# ---- 5. A second, independent fresh database still honours its own .env values
r = run(
    "import app\n"
    "with app.app.app_context(): app.init_db()\n"
    "from models import Admin, AdminType, db\n"
    "from sqlalchemy import select\n"
    "with app.app.app_context():\n"
    "    a = db.session.scalars(select(Admin).join(AdminType, AdminType.id==Admin.admin_type_id).where(AdminType.is_system==1)).first()\n"
    "    print('USERNAME=' + a.username)\n",
    {"CRAINBOW_DB": DB2, "CRAINBOW_SUPERADMIN_USERNAME": "secondfresh"},
)
check("a separate fresh DB still honours its own env override", "USERNAME=secondfresh" in r.stdout, r.stdout + r.stderr[-500:])

for p in (DB1, DB2):
    if os.path.exists(p):
        os.remove(p)

print()
failed = [x for x in results if not x[1]]
print(f"{len(results)-len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
