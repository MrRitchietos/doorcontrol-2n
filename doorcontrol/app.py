import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo, available_timezones

from flask import Flask, abort, g, jsonify, redirect, request, send_file, send_from_directory, session
from flask.sessions import SecureCookieSessionInterface
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from . import __version__, branding, intercom, tls
from .db import Database

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

ICONS = ("door", "gate", "barrier", "garage", "lock", "unlock", "key", "light", "bell", "elevator")
COLORS = ("", "blue", "green", "red", "orange", "purple", "gray")
PERMS = ("manage_users", "manage_doors", "manage_settings", "view_log", "all_doors")
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class Fail(Exception):
    def __init__(self, msg, code=400):
        self.msg = msg
        self.code = code


def text(data, key, maxlen=100, required=False):
    v = str(data.get(key) or "").strip()
    if required and not v:
        raise Fail(f"{key} is required")
    if len(v) > maxlen:
        raise Fail(f"{key} is too long")
    return v


def flag(data, key):
    return 1 if data.get(key) in (True, 1, "1", "true", "on") else 0


class LoginGuard:
    """Slows down password guessing: 5 misses per user+ip, then wait a minute (doubling)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.misses = {}

    def wait(self, key):
        with self.lock:
            n, until = self.misses.get(key, (0, 0))
            return max(0, int(until - time.time()))

    def miss(self, key):
        with self.lock:
            n, _ = self.misses.get(key, (0, 0))
            n += 1
            until = time.time() + (60 * 2 ** (n - 5) if n >= 5 else 0)
            self.misses[key] = (n, until)

    def clear(self, key):
        with self.lock:
            self.misses.pop(key, None)


class SessionByScheme(SecureCookieSessionInterface):
    """Cookie is marked Secure when the page came over https, so http and https can run side by side."""

    def get_cookie_secure(self, app):
        return app.config["SESSION_COOKIE_SECURE"] or request.is_secure


def create_app(data_dir=None, trust_proxy=None, secure_cookies=None, https_port=None, tls_control=None):
    data_dir = data_dir or os.environ.get("DOORCONTROL_DATA") or os.path.join(os.getcwd(), "data")
    os.makedirs(data_dir, exist_ok=True)
    if trust_proxy is None:
        trust_proxy = os.environ.get("DOORCONTROL_TRUST_PROXY") == "1"
    if secure_cookies is None:
        secure_cookies = os.environ.get("DOORCONTROL_SECURE_COOKIES") == "1"

    app = Flask(__name__, static_folder=None)
    if trust_proxy:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    key_file = os.path.join(data_dir, "secret.key")
    if not os.path.exists(key_file):
        with open(key_file, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(key_file, 0o600)
    with open(key_file) as f:
        app.secret_key = f.read().strip()

    app.config.update(
        SESSION_COOKIE_NAME="dc_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookies,
        MAX_CONTENT_LENGTH=6 * 1024 * 1024,
        JSON_SORT_KEYS=False,
    )

    app.session_interface = SessionByScheme()

    db = Database(os.path.join(data_dir, "doorcontrol.db"))
    guard = LoginGuard()
    app.db = db
    app.data_dir = data_dir

    def settings():
        return db.settings()

    if not os.path.exists(os.path.join(data_dir, "icon-192.png")):
        branding.build_icons(data_dir, settings()["accent"])

    def tz():
        try:
            return ZoneInfo(settings().get("timezone") or "UTC")
        except Exception:
            return ZoneInfo("UTC")

    def client_ip():
        return request.remote_addr or ""

    def write_log(event, target="", ok=True, detail="", user=None):
        user = user or getattr(g, "user", None)
        db.run(
            "INSERT INTO log(user_id, username, event, target, ok, detail, ip) VALUES (?,?,?,?,?,?,?)",
            (user["id"] if user else None, user["username"] if user else "", event, target, 1 if ok else 0,
             detail[:500], client_ip()),
        )

    def purge_log():
        try:
            days = int(settings().get("log_days") or 180)
        except ValueError:
            days = 180
        db.run("DELETE FROM log WHERE ts < datetime('now', ?)", (f"-{days} days",))

    purge_log()

    # ---------- users, roles, permissions ----------

    def load_user():
        uid = session.get("uid")
        if not uid:
            return None
        u = db.one(
            "SELECT u.*, r.name AS role_name, r.rank, r.is_admin, r.manage_users, r.manage_doors,"
            " r.manage_settings, r.view_log, r.all_doors, r.days, r.time_from, r.time_to"
            " FROM users u JOIN roles r ON r.id = u.role_id WHERE u.id = ?",
            (uid,),
        )
        if not u or not u["active"] or u["token"] != session.get("tok"):
            session.clear()
            return None
        if u["is_admin"]:
            for p in PERMS:
                u[p] = 1
        return u

    def perms(u):
        return {p: bool(u[p]) for p in PERMS} | {"is_admin": bool(u["is_admin"])}

    def in_time_window(u):
        """Returns '' when allowed now, otherwise the reason."""
        if u["is_admin"]:
            return ""
        now = datetime.now(tz())
        if str(now.isoweekday()) not in (u["days"] or ""):
            return "not allowed on this day"
        tf, tt = u["time_from"], u["time_to"]
        if tf and tt:
            cur = now.strftime("%H:%M")
            inside = tf <= cur < tt if tf <= tt else (cur >= tf or cur < tt)
            if not inside:
                return f"only allowed between {tf} and {tt}"
        return ""

    def allowed_button_ids(u):
        if u["all_doors"]:
            return None
        return {r["button_id"] for r in db.all("SELECT button_id FROM role_buttons WHERE role_id = ?", (u["role_id"],))}

    def need(perm=None):
        def deco(fn):
            @wraps(fn)
            def inner(*a, **kw):
                if not g.user:
                    return jsonify(error="not logged in"), 401
                if perm and not g.user[perm]:
                    return jsonify(error="you are not allowed to do this"), 403
                return fn(*a, **kw)
            return inner
        return deco

    def https_live():
        return bool(tls_control and tls_control.https_running())

    def https_url():
        s = settings()
        host = s.get("https_domain") or ""
        if not host or not https_port:
            return ""
        return f"https://{host}" + ("" if https_port == 443 else f":{https_port}")

    @app.before_request
    def to_https():
        # only when https really runs and a domain is set, and never for localhost, so you can't lock yourself out
        if request.is_secure or request.host.split(":")[0] in ("localhost", "127.0.0.1"):
            return None
        if settings().get("https_redirect") == "1" and https_live() and https_url():
            return redirect(https_url() + request.full_path.rstrip("?"), code=302)
        return None

    @app.before_request
    def before():
        g.user = None
        if request.path.startswith("/api/"):
            # every change must come from our own page (stops other sites posting with the cookie)
            if request.method not in ("GET", "HEAD") and request.headers.get("X-Requested-With") != "doorcontrol":
                return jsonify(error="bad request"), 400
            g.user = load_user()
            try:
                app.permanent_session_lifetime = timedelta(days=int(settings().get("session_days") or 30))
            except ValueError:
                app.permanent_session_lifetime = timedelta(days=30)

    @app.after_request
    def after(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(Fail)
    def on_fail(e):
        return jsonify(error=e.msg), e.code

    # ---------- pages and branding ----------

    @app.get("/")
    def index():
        resp = send_from_directory(STATIC, "index.html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/static/<path:name>")
    def static_files(name):
        resp = send_from_directory(STATIC, name)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/sw.js")
    def service_worker():
        resp = send_from_directory(STATIC, "sw.js")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/logo.png")
    def logo():
        if not branding.has_logo(data_dir):
            abort(404)
        return send_from_directory(data_dir, "logo.png", max_age=0)

    @app.get("/<any('icon-192.png','icon-512.png','apple-touch-icon.png','favicon.png'):name>")
    def icons(name):
        if name == "favicon.png":
            name = "icon-192.png"
        if not os.path.exists(os.path.join(data_dir, name)):
            branding.build_icons(data_dir, settings()["accent"])
        return send_from_directory(data_dir, name, max_age=0)

    @app.get("/manifest.webmanifest")
    def manifest():
        s = settings()
        v = s.get("logo_version", "0")
        resp = jsonify(
            name=s["app_name"],
            short_name=s["app_name"][:12],
            start_url="/",
            scope="/",
            display="standalone",
            orientation="portrait",
            background_color=s["accent"],
            theme_color=s["accent"],
            icons=[
                {"src": f"/icon-192.png?v={v}", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": f"/icon-512.png?v={v}", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {"src": f"/icon-512.png?v={v}", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            ],
        )
        resp.mimetype = "application/manifest+json"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    def public_branding():
        s = settings()
        return {
            "app_name": s["app_name"],
            "subtitle": s["subtitle"],
            "accent": s["accent"],
            "language": s["language"],
            "logo": f"/logo.png?v={s['logo_version']}" if branding.has_logo(data_dir) else "",
            "icon_version": s["logo_version"],
            "version": __version__,
            "needs_setup": needs_setup(),
        }

    @app.get("/api/branding")
    def api_branding():
        return jsonify(public_branding())

    # ---------- login ----------

    @app.post("/api/login")
    def login():
        data = request.get_json(silent=True) or {}
        username = text(data, "username", 64)
        password = str(data.get("password") or "")
        key = f"{username.lower()}|{client_ip()}"
        wait = guard.wait(key)
        if wait:
            return jsonify(error=f"too many tries, wait {wait} seconds"), 429
        u = db.one("SELECT * FROM users WHERE username = ?", (username,))
        if not u or not u["active"] or not check_password_hash(u["password_hash"], password):
            guard.miss(key)
            write_log("login failed", username, ok=False, user={"id": None, "username": username})
            return jsonify(error="wrong username or password"), 401
        guard.clear(key)
        session.clear()
        session["uid"] = u["id"]
        session["tok"] = u["token"]
        session.permanent = bool(data.get("remember", True))
        db.run("UPDATE users SET last_login = datetime('now') WHERE id = ?", (u["id"],))
        write_log("login", user=u)
        return jsonify(ok=True)

    setup_lock = threading.Lock()

    def needs_setup():
        return not db.one("SELECT 1 FROM users")

    @app.post("/api/setup")
    def first_setup():
        """First run only: make the admin account. Refused as soon as any user exists."""
        data = request.get_json(silent=True) or {}
        with setup_lock:
            if not needs_setup():
                return jsonify(error="setup is already done"), 403
            username = text(data, "username", 64, required=True)
            if not re.match(r"^[A-Za-z0-9._@-]+$", username):
                raise Fail("username: only letters, numbers and . _ @ -")
            pw = check_new_password(data.get("password"))
            app_name = text(data, "app_name", 40)
            role = db.one("SELECT id FROM roles WHERE is_admin = 1 ORDER BY id LIMIT 1")
            uid = db.run("INSERT INTO users(username, name, password_hash, role_id) VALUES (?,?,?,?)",
                         (username, text(data, "name", 80), generate_password_hash(pw), role["id"]))
        if app_name:
            db.set_setting("app_name", app_name)
        if data.get("language") in ("en", "nl"):
            db.set_setting("language", data["language"])
        u = db.one("SELECT * FROM users WHERE id = ?", (uid,))
        session.clear()
        session["uid"] = uid
        session["tok"] = u["token"]
        session.permanent = True
        write_log("admin created at first run", username, user=u)
        return jsonify(ok=True)

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/me")
    @need()
    def me():
        u = g.user
        return jsonify(
            user={"id": u["id"], "username": u["username"], "name": u["name"], "role": u["role_name"], "rank": u["rank"]},
            perms=perms(u),
            branding=public_branding(),
        )

    @app.post("/api/me/password")
    @need()
    def my_password():
        data = request.get_json(silent=True) or {}
        u = db.one("SELECT * FROM users WHERE id = ?", (g.user["id"],))
        if not check_password_hash(u["password_hash"], str(data.get("current") or "")):
            raise Fail("current password is wrong")
        new = check_new_password(data.get("new"))
        db.run("UPDATE users SET password_hash = ?, token = token + 1 WHERE id = ?",
               (generate_password_hash(new), u["id"]))
        session["tok"] = u["token"] + 1
        write_log("password changed")
        return jsonify(ok=True)

    def check_new_password(pw):
        pw = str(pw or "")
        if len(pw) < 6:
            raise Fail("password must be at least 6 characters")
        if len(pw) > 200:
            raise Fail("password is too long")
        return pw

    # ---------- doors for the home screen ----------

    @app.get("/api/doors")
    @need()
    def doors():
        u = g.user
        allowed = allowed_button_ids(u)
        rows = db.all("SELECT * FROM buttons WHERE enabled = 1 ORDER BY sort, name")
        out = []
        for b in rows:
            if allowed is not None and b["id"] not in allowed:
                continue
            out.append({k: b[k] for k in ("id", "name", "section", "icon", "color", "hold_to_open")})
        return jsonify(doors=out, blocked=in_time_window(u))

    @app.post("/api/doors/<int:bid>/open")
    @need()
    def open_door(bid):
        u = g.user
        b = db.one("SELECT * FROM buttons WHERE id = ? AND enabled = 1", (bid,))
        allowed = allowed_button_ids(u)
        if not b or (allowed is not None and bid not in allowed):
            return jsonify(error="you have no access to this door"), 403
        reason = in_time_window(u)
        if reason:
            write_log("open", b["name"], ok=False, detail=reason)
            return jsonify(error=reason), 403
        return fire(b, "open")

    def fire(b, event):
        pairs = []
        for s in db.all("SELECT * FROM steps WHERE button_id = ? ORDER BY id", (b["id"],)):
            d = db.one("SELECT * FROM devices WHERE id = ?", (s["device_id"],))
            if d:
                pairs.append((d, s))
        errors = intercom.run_steps(pairs)
        write_log(event, b["name"], ok=not errors, detail="; ".join(errors))
        if errors:
            return jsonify(error="; ".join(errors)), 502
        return jsonify(ok=True)

    # ---------- admin: settings ----------

    @app.get("/api/admin/settings")
    @need("manage_settings")
    def get_settings():
        s = settings()
        s.pop("logo_version", None)
        return jsonify(settings=s, has_logo=branding.has_logo(data_dir),
                       timezones=sorted(t for t in available_timezones() if "/" in t))

    @app.put("/api/admin/settings")
    @need("manage_settings")
    def put_settings():
        data = request.get_json(silent=True) or {}
        old = settings()
        new = {
            "app_name": text(data, "app_name", 40, required=True),
            "subtitle": text(data, "subtitle", 80),
            "accent": text(data, "accent", 7) or "#2563eb",
            "language": text(data, "language", 5) or "en",
            "timezone": text(data, "timezone", 64) or "Europe/Brussels",
            "session_days": text(data, "session_days", 4) or "30",
            "log_days": text(data, "log_days", 5) or "180",
        }
        if not HEX.match(new["accent"]):
            raise Fail("color must look like #2563eb")
        if new["language"] not in ("en", "nl"):
            raise Fail("unknown language")
        if new["timezone"] not in available_timezones():
            raise Fail("unknown timezone")
        for k in ("session_days", "log_days"):
            if not new[k].isdigit() or not 1 <= int(new[k]) <= 3650:
                raise Fail(f"{k} must be a number of days")
        for k, v in new.items():
            db.set_setting(k, v)
        if new["accent"] != old["accent"]:
            branding.build_icons(data_dir, new["accent"])
            db.set_setting("logo_version", int(old.get("logo_version") or 0) + 1)
        write_log("settings changed")
        return jsonify(ok=True, branding=public_branding())

    @app.post("/api/admin/logo")
    @need("manage_settings")
    def upload_logo():
        f = request.files.get("logo")
        if not f:
            raise Fail("no file")
        try:
            branding.save_logo(data_dir, f.read())
        except ValueError as e:
            raise Fail(str(e))
        bump_logo()
        write_log("logo changed")
        return jsonify(ok=True, branding=public_branding())

    @app.delete("/api/admin/logo")
    @need("manage_settings")
    def delete_logo():
        branding.remove_logo(data_dir)
        bump_logo()
        write_log("logo removed")
        return jsonify(ok=True, branding=public_branding())

    def bump_logo():
        s = settings()
        branding.build_icons(data_dir, s["accent"])
        db.set_setting("logo_version", int(s.get("logo_version") or 0) + 1)

    # ---------- admin: https certificate ----------

    def https_state():
        s = settings()
        domain = s.get("https_domain", "")
        return {
            "domain": domain,
            "redirect": s.get("https_redirect") == "1",
            "port": https_port,
            "running": https_live(),
            "error": tls_control.https_error if tls_control else "",
            "url": https_url(),
            "cert": tls.current(data_dir, domain),
            "secure_now": request.is_secure,
        }

    @app.get("/api/admin/https")
    @need("manage_settings")
    def get_https():
        return jsonify(https_state())

    def check_domain(d):
        d = d.strip().lower().rstrip(".")
        if d and not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", d):
            raise Fail("domain looks wrong, use something like door.company.be")
        return d

    @app.post("/api/admin/https")
    @need("manage_settings")
    def upload_cert():
        files = [f.read() for f in request.files.getlist("files")]
        if not files:
            raise Fail("pick the certificate file(s)")
        domain = check_domain(request.form.get("domain", ""))
        try:
            key, chain = tls.parse(files, request.form.get("password", ""))
        except tls.CertError as e:
            raise Fail(str(e))
        chain = tls.complete_chain(chain)
        i = tls.info(chain[0], len(chain), domain)
        if i["days_left"] < 0:
            raise Fail(f"this certificate expired on {i['not_after']}")
        if not domain:
            domain = next((n for n in i["names"] if not n.startswith("*")), "")
        tls.save(data_dir, key, chain)
        db.set_setting("https_domain", domain)
        if tls_control:
            tls_control.reload_tls()
        write_log("https certificate imported", i["subject"], detail=f"valid until {i['not_after']}")
        return jsonify(https_state())

    @app.put("/api/admin/https")
    @need("manage_settings")
    def put_https():
        data = request.get_json(silent=True) or {}
        domain = check_domain(str(data.get("domain") or ""))
        want_redirect = bool(data.get("redirect"))
        if want_redirect:
            cert = tls.current(data_dir, domain)
            if not (cert and domain and https_live()):
                raise Fail("https must run with a domain before http can be sent to it")
            if not cert["domain_ok"]:
                raise Fail("the certificate is not made for this domain")
        db.set_setting("https_domain", domain)
        db.set_setting("https_redirect", "1" if want_redirect else "0")
        write_log("https settings changed", domain, detail="redirect on" if want_redirect else "redirect off")
        return jsonify(https_state())

    @app.delete("/api/admin/https")
    @need("manage_settings")
    def delete_cert():
        tls.remove(data_dir)
        db.set_setting("https_redirect", "0")
        if tls_control:
            tls_control.reload_tls()
        write_log("https certificate removed")
        return jsonify(https_state())

    @app.get("/api/admin/backup")
    @need("manage_settings")
    def backup():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        dst = sqlite3.connect(tmp.name)
        db.conn().backup(dst)
        dst.close()
        name = "doorcontrol-" + datetime.now().strftime("%Y%m%d-%H%M") + ".db"
        write_log("backup downloaded")
        resp = send_file(tmp.name, as_attachment=True, download_name=name)
        resp.call_on_close(lambda: os.remove(tmp.name))
        return resp

    # ---------- admin: roles (ranks) ----------

    def role_out(r):
        r = dict(r)
        r["buttons"] = [x["button_id"] for x in db.all("SELECT button_id FROM role_buttons WHERE role_id = ?", (r["id"],))]
        r["users"] = db.one("SELECT COUNT(*) AS n FROM users WHERE role_id = ?", (r["id"],))["n"]
        return r

    @app.get("/api/admin/roles")
    @need()
    def list_roles():
        if not (g.user["manage_users"] or g.user["is_admin"]):
            return jsonify(error="you are not allowed to do this"), 403
        return jsonify(roles=[role_out(r) for r in db.all("SELECT * FROM roles ORDER BY rank, name")])

    def role_from(data, current=None):
        r = {"name": text(data, "name", 40, required=True)}
        try:
            r["rank"] = int(data.get("rank") or 10)
        except ValueError:
            raise Fail("rank must be a number")
        if not 2 <= r["rank"] <= 99 and not (current and current["is_admin"]):
            raise Fail("rank must be between 2 and 99 (1 is the admin)")
        for p in PERMS:
            r[p] = flag(data, p)
        days = "".join(sorted(set(c for c in str(data.get("days") or "") if c in "1234567")))
        r["days"] = days
        r["time_from"] = text(data, "time_from", 5)
        r["time_to"] = text(data, "time_to", 5)
        if bool(r["time_from"]) != bool(r["time_to"]):
            raise Fail("fill in both times or none")
        for k in ("time_from", "time_to"):
            if r[k] and not HHMM.match(r[k]):
                raise Fail("times must look like 07:30")
        return r

    def save_role_buttons(rid, ids):
        db.run("DELETE FROM role_buttons WHERE role_id = ?", (rid,))
        for bid in ids or []:
            if db.one("SELECT 1 FROM buttons WHERE id = ?", (int(bid),)):
                db.run("INSERT OR IGNORE INTO role_buttons(role_id, button_id) VALUES (?, ?)", (rid, int(bid)))

    @app.post("/api/admin/roles")
    @need("is_admin")
    def add_role():
        data = request.get_json(silent=True) or {}
        r = role_from(data)
        rid = db.run(
            "INSERT INTO roles(name, rank, manage_users, manage_doors, manage_settings, view_log, all_doors,"
            " days, time_from, time_to) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["name"], r["rank"], r["manage_users"], r["manage_doors"], r["manage_settings"], r["view_log"],
             r["all_doors"], r["days"], r["time_from"], r["time_to"]),
        )
        save_role_buttons(rid, data.get("buttons"))
        write_log("rank added", r["name"])
        return jsonify(ok=True, id=rid)

    @app.put("/api/admin/roles/<int:rid>")
    @need("is_admin")
    def edit_role(rid):
        cur = db.one("SELECT * FROM roles WHERE id = ?", (rid,))
        if not cur:
            abort(404)
        data = request.get_json(silent=True) or {}
        r = role_from(data, cur)
        if cur["is_admin"]:
            # the admin rank always keeps everything, only the name can change
            db.run("UPDATE roles SET name = ? WHERE id = ?", (r["name"], rid))
        else:
            db.run(
                "UPDATE roles SET name=?, rank=?, manage_users=?, manage_doors=?, manage_settings=?, view_log=?,"
                " all_doors=?, days=?, time_from=?, time_to=? WHERE id = ?",
                (r["name"], r["rank"], r["manage_users"], r["manage_doors"], r["manage_settings"], r["view_log"],
                 r["all_doors"], r["days"], r["time_from"], r["time_to"], rid),
            )
            save_role_buttons(rid, data.get("buttons"))
        write_log("rank changed", r["name"])
        return jsonify(ok=True)

    @app.delete("/api/admin/roles/<int:rid>")
    @need("is_admin")
    def delete_role(rid):
        cur = db.one("SELECT * FROM roles WHERE id = ?", (rid,))
        if not cur:
            abort(404)
        if cur["is_admin"]:
            raise Fail("the admin rank cannot be deleted")
        if db.one("SELECT 1 FROM users WHERE role_id = ?", (rid,)):
            raise Fail("move the users in this rank to another rank first")
        db.run("DELETE FROM roles WHERE id = ?", (rid,))
        write_log("rank deleted", cur["name"])
        return jsonify(ok=True)

    # ---------- admin: users ----------

    def can_touch_rank(rank):
        """Non-admin user managers only handle people below their own rank."""
        return g.user["is_admin"] or rank > g.user["rank"]

    def active_admins(exclude=None):
        return db.one(
            "SELECT COUNT(*) AS n FROM users u JOIN roles r ON r.id = u.role_id"
            " WHERE r.is_admin = 1 AND u.active = 1 AND u.id != ?",
            (exclude or 0,),
        )["n"]

    @app.get("/api/admin/users")
    @need("manage_users")
    def list_users():
        rows = db.all(
            "SELECT u.id, u.username, u.name, u.role_id, u.active, u.created_at, u.last_login,"
            " r.name AS role_name, r.rank FROM users u JOIN roles r ON r.id = u.role_id ORDER BY r.rank, u.username"
        )
        for r in rows:
            r["editable"] = can_touch_rank(r["rank"]) or r["id"] == g.user["id"]
        return jsonify(users=rows)

    def target_role(data):
        try:
            role = db.one("SELECT * FROM roles WHERE id = ?", (int(data.get("role_id") or 0),))
        except ValueError:
            role = None
        if not role:
            raise Fail("pick a rank")
        if not can_touch_rank(role["rank"]):
            raise Fail("you can only give ranks below your own", 403)
        return role

    @app.post("/api/admin/users")
    @need("manage_users")
    def add_user():
        data = request.get_json(silent=True) or {}
        username = text(data, "username", 64, required=True)
        if not re.match(r"^[A-Za-z0-9._@-]+$", username):
            raise Fail("username: only letters, numbers and . _ @ -")
        role = target_role(data)
        pw = check_new_password(data.get("password"))
        if db.one("SELECT 1 FROM users WHERE username = ?", (username,)):
            raise Fail("this username already exists")
        uid = db.run(
            "INSERT INTO users(username, name, password_hash, role_id, active) VALUES (?,?,?,?,?)",
            (username, text(data, "name", 80), generate_password_hash(pw), role["id"], 1 if data.get("active", True) else 0),
        )
        write_log("user added", username)
        return jsonify(ok=True, id=uid)

    @app.put("/api/admin/users/<int:uid>")
    @need("manage_users")
    def edit_user(uid):
        cur = db.one("SELECT u.*, r.rank, r.is_admin FROM users u JOIN roles r ON r.id = u.role_id WHERE u.id = ?", (uid,))
        if not cur:
            abort(404)
        if not can_touch_rank(cur["rank"]) and uid != g.user["id"]:
            raise Fail("this user has the same or a higher rank than you", 403)
        data = request.get_json(silent=True) or {}
        username = text(data, "username", 64, required=True)
        if not re.match(r"^[A-Za-z0-9._@-]+$", username):
            raise Fail("username: only letters, numbers and . _ @ -")
        if db.one("SELECT 1 FROM users WHERE username = ? AND id != ?", (username, uid)):
            raise Fail("this username already exists")
        if uid == g.user["id"]:
            role = db.one("SELECT * FROM roles WHERE id = ?", (cur["role_id"],))
            active = 1
        else:
            role = target_role(data)
            active = 1 if data.get("active", True) else 0
        if cur["is_admin"] and cur["active"] and (not role["is_admin"] or not active) and active_admins(uid) == 0:
            raise Fail("this is the last admin, make someone else admin first")
        token_bump = 1 if (not active or role["id"] != cur["role_id"]) else 0
        db.run(
            "UPDATE users SET username=?, name=?, role_id=?, active=?, token = token + ? WHERE id = ?",
            (username, text(data, "name", 80), role["id"], active, token_bump, uid),
        )
        if data.get("password"):
            pw = check_new_password(data.get("password"))
            db.run("UPDATE users SET password_hash = ?, token = token + 1 WHERE id = ?", (generate_password_hash(pw), uid))
            if uid == g.user["id"]:
                session["tok"] = db.one("SELECT token FROM users WHERE id = ?", (uid,))["token"]
        write_log("user changed", username)
        return jsonify(ok=True)

    @app.delete("/api/admin/users/<int:uid>")
    @need("manage_users")
    def delete_user(uid):
        cur = db.one("SELECT u.*, r.rank, r.is_admin FROM users u JOIN roles r ON r.id = u.role_id WHERE u.id = ?", (uid,))
        if not cur:
            abort(404)
        if uid == g.user["id"]:
            raise Fail("you cannot delete yourself")
        if not can_touch_rank(cur["rank"]):
            raise Fail("this user has the same or a higher rank than you", 403)
        if cur["is_admin"] and cur["active"] and active_admins(uid) == 0:
            raise Fail("this is the last admin")
        db.run("DELETE FROM users WHERE id = ?", (uid,))
        write_log("user deleted", cur["username"])
        return jsonify(ok=True)

    # ---------- admin: intercoms ----------

    def device_from(data):
        d = {
            "name": text(data, "name", 60, required=True),
            "host": text(data, "host", 120, required=True),
            "scheme": text(data, "scheme", 5) or "https",
            "username": text(data, "username", 64),
            "verify_tls": flag(data, "verify_tls"),
        }
        d["host"] = re.sub(r"^https?://", "", d["host"]).rstrip("/")
        if not re.match(r"^[A-Za-z0-9.\-:\[\]]+$", d["host"]):
            raise Fail("IP / host looks wrong")
        if d["scheme"] not in ("http", "https"):
            raise Fail("scheme must be http or https")
        return d

    def device_public(d):
        d = dict(d)
        d["has_password"] = bool(d.pop("password", ""))
        d["buttons"] = db.one("SELECT COUNT(DISTINCT button_id) AS n FROM steps WHERE device_id = ?", (d["id"],))["n"]
        return d

    @app.get("/api/admin/devices")
    @need("manage_doors")
    def list_devices():
        return jsonify(devices=[device_public(d) for d in db.all("SELECT * FROM devices ORDER BY name")])

    @app.post("/api/admin/devices")
    @need("manage_doors")
    def add_device():
        data = request.get_json(silent=True) or {}
        d = device_from(data)
        did = db.run(
            "INSERT INTO devices(name, host, scheme, username, password, verify_tls) VALUES (?,?,?,?,?,?)",
            (d["name"], d["host"], d["scheme"], d["username"], str(data.get("password") or ""), d["verify_tls"]),
        )
        write_log("intercom added", f"{d['name']} ({d['host']})")
        return jsonify(ok=True, id=did)

    @app.put("/api/admin/devices/<int:did>")
    @need("manage_doors")
    def edit_device(did):
        cur = db.one("SELECT * FROM devices WHERE id = ?", (did,))
        if not cur:
            abort(404)
        data = request.get_json(silent=True) or {}
        d = device_from(data)
        # empty password field = keep the stored one, unless the user name was cleared
        pw = str(data.get("password") or "") or (cur["password"] if d["username"] else "")
        db.run(
            "UPDATE devices SET name=?, host=?, scheme=?, username=?, password=?, verify_tls=? WHERE id = ?",
            (d["name"], d["host"], d["scheme"], d["username"], pw, d["verify_tls"], did),
        )
        detail = f"IP {cur['host']} -> {d['host']}" if cur["host"] != d["host"] else ""
        write_log("intercom changed", d["name"], detail=detail)
        return jsonify(ok=True)

    @app.delete("/api/admin/devices/<int:did>")
    @need("manage_doors")
    def delete_device(did):
        cur = db.one("SELECT * FROM devices WHERE id = ?", (did,))
        if not cur:
            abort(404)
        db.run("DELETE FROM devices WHERE id = ?", (did,))
        write_log("intercom deleted", cur["name"])
        return jsonify(ok=True)

    @app.post("/api/admin/devices/<int:did>/test")
    @need("manage_doors")
    def test_device(did):
        d = db.one("SELECT * FROM devices WHERE id = ?", (did,))
        if not d:
            abort(404)
        try:
            return jsonify(ok=True, info=intercom.device_info(d))
        except intercom.IntercomError as e:
            if intercom.reachable(d):
                return jsonify(ok=True, reachable_only=True, error=str(e))
            return jsonify(ok=False, error=str(e))

    # ---------- admin: doors (buttons) ----------

    def button_out(b):
        b = dict(b)
        b["steps"] = db.all("SELECT device_id, kind, value, action FROM steps WHERE button_id = ? ORDER BY id", (b["id"],))
        return b

    @app.get("/api/admin/buttons")
    @need("manage_doors")
    def list_buttons():
        return jsonify(
            buttons=[button_out(b) for b in db.all("SELECT * FROM buttons ORDER BY sort, name")],
            icons=ICONS,
            colors=COLORS,
            actions=intercom.SWITCH_ACTIONS,
        )

    def button_from(data):
        b = {
            "name": text(data, "name", 40, required=True),
            "section": text(data, "section", 40),
            "icon": text(data, "icon", 20) or "door",
            "color": text(data, "color", 10),
            "hold_to_open": flag(data, "hold_to_open"),
            "enabled": 1 if data.get("enabled", True) else 0,
        }
        if b["icon"] not in ICONS:
            raise Fail("unknown icon")
        if b["color"] not in COLORS:
            raise Fail("unknown color")
        steps = []
        for s in data.get("steps") or []:
            try:
                did = int(s.get("device_id") or 0)
            except ValueError:
                did = 0
            if not db.one("SELECT 1 FROM devices WHERE id = ?", (did,)):
                raise Fail("pick an intercom for every action")
            kind = s.get("kind") or "switch"
            value = str(s.get("value") or "").strip()
            action = s.get("action") or "trigger"
            if kind == "switch":
                if not value.isdigit() or not 1 <= int(value) <= 8:
                    raise Fail("switch number must be 1 to 8")
                if action not in intercom.SWITCH_ACTIONS:
                    raise Fail("unknown switch action")
            elif kind == "trigger":
                if not re.match(r"^[A-Za-z0-9_.\-]{1,60}$", value):
                    raise Fail("trigger name: letters, numbers, _ . - only")
            elif kind == "path":
                if not value or len(value) > 200 or not re.match(r"^/?[\x21-\x7e]+$", value):
                    raise Fail("path looks wrong")
            else:
                raise Fail("unknown action type")
            steps.append((did, kind, value, action))
        return b, steps

    def save_steps(bid, steps):
        db.run("DELETE FROM steps WHERE button_id = ?", (bid,))
        for did, kind, value, action in steps:
            db.run("INSERT INTO steps(button_id, device_id, kind, value, action) VALUES (?,?,?,?,?)",
                   (bid, did, kind, value, action))

    @app.post("/api/admin/buttons")
    @need("manage_doors")
    def add_button():
        b, steps = button_from(request.get_json(silent=True) or {})
        sort = (db.one("SELECT MAX(sort) AS m FROM buttons")["m"] or 0) + 1
        bid = db.run(
            "INSERT INTO buttons(name, section, icon, color, hold_to_open, enabled, sort) VALUES (?,?,?,?,?,?,?)",
            (b["name"], b["section"], b["icon"], b["color"], b["hold_to_open"], b["enabled"], sort),
        )
        save_steps(bid, steps)
        write_log("door added", b["name"])
        return jsonify(ok=True, id=bid)

    @app.put("/api/admin/buttons/<int:bid>")
    @need("manage_doors")
    def edit_button(bid):
        if not db.one("SELECT 1 FROM buttons WHERE id = ?", (bid,)):
            abort(404)
        b, steps = button_from(request.get_json(silent=True) or {})
        db.run(
            "UPDATE buttons SET name=?, section=?, icon=?, color=?, hold_to_open=?, enabled=? WHERE id = ?",
            (b["name"], b["section"], b["icon"], b["color"], b["hold_to_open"], b["enabled"], bid),
        )
        save_steps(bid, steps)
        write_log("door changed", b["name"])
        return jsonify(ok=True)

    @app.delete("/api/admin/buttons/<int:bid>")
    @need("manage_doors")
    def delete_button(bid):
        cur = db.one("SELECT * FROM buttons WHERE id = ?", (bid,))
        if not cur:
            abort(404)
        db.run("DELETE FROM buttons WHERE id = ?", (bid,))
        write_log("door deleted", cur["name"])
        return jsonify(ok=True)

    @app.post("/api/admin/buttons/order")
    @need("manage_doors")
    def order_buttons():
        ids = (request.get_json(silent=True) or {}).get("ids") or []
        for i, bid in enumerate(ids):
            db.run("UPDATE buttons SET sort = ? WHERE id = ?", (i, int(bid)))
        return jsonify(ok=True)

    @app.post("/api/admin/buttons/<int:bid>/test")
    @need("manage_doors")
    def test_button(bid):
        b = db.one("SELECT * FROM buttons WHERE id = ?", (bid,))
        if not b:
            abort(404)
        return fire(b, "test")

    # ---------- admin: log ----------

    @app.get("/api/admin/log")
    @need("view_log")
    def get_log():
        q = request.args.get("q", "").strip()
        try:
            before = int(request.args.get("before") or 0)
        except ValueError:
            before = 0
        sql = "SELECT * FROM log WHERE 1=1"
        args = []
        if before:
            sql += " AND id < ?"
            args.append(before)
        if q:
            sql += " AND (username LIKE ? OR target LIKE ? OR event LIKE ? OR ip LIKE ?)"
            args += [f"%{q}%"] * 4
        sql += " ORDER BY id DESC LIMIT 100"
        rows = db.all(sql, args)
        zone = tz()
        for r in rows:
            t = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
            r["ts"] = t.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")
        return jsonify(log=rows)

    return app


def make_admin(data_dir, username="admin", password=None):
    """Creates the admin user or resets its password. Returns the password."""
    app = create_app(data_dir)
    db = app.db
    password = password or secrets.token_urlsafe(9)
    role = db.one("SELECT id FROM roles WHERE is_admin = 1 ORDER BY id LIMIT 1")
    u = db.one("SELECT * FROM users WHERE username = ?", (username,))
    if u:
        db.run("UPDATE users SET password_hash = ?, role_id = ?, active = 1, token = token + 1 WHERE id = ?",
               (generate_password_hash(password), role["id"], u["id"]))
    else:
        db.run("INSERT INTO users(username, name, password_hash, role_id) VALUES (?,?,?,?)",
               (username, "Administrator", generate_password_hash(password), role["id"]))
    return password


def import_cert(data_dir, cert_file, key_file="", password="", domain=""):
    files = []
    for p in (cert_file, key_file):
        if p:
            with open(p, "rb") as f:
                files.append(f.read())
    key, chain = tls.parse(files, password)
    chain = tls.complete_chain(chain)
    i = tls.info(chain[0], len(chain), domain)
    tls.save(data_dir, key, chain)
    db = Database(os.path.join(data_dir, "doorcontrol.db"))
    db.set_setting("https_domain", domain or next((n for n in i["names"] if not n.startswith("*")), ""))
    return f"{i['subject']}, valid until {i['not_after']}"


def https_off(data_dir):
    tls.remove(data_dir)
    Database(os.path.join(data_dir, "doorcontrol.db")).set_setting("https_redirect", "0")


def has_users(data_dir):
    path = os.path.join(data_dir, "doorcontrol.db")
    if not os.path.exists(path):
        return False
    return bool(Database(path).one("SELECT 1 FROM users"))


def restore(data_dir, backup_file):
    con = sqlite3.connect(backup_file)
    con.execute("SELECT COUNT(*) FROM users").fetchone()
    con.close()
    dst = os.path.join(data_dir, "doorcontrol.db")
    for ext in ("-wal", "-shm"):
        if os.path.exists(dst + ext):
            os.remove(dst + ext)
    shutil.copyfile(backup_file, dst)
    os.chmod(dst, 0o600)
