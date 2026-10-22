import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from doorcontrol.app import create_app, make_admin, restore

H = {"X-Requested-With": "doorcontrol"}


class Fake2N(BaseHTTPRequestHandler):
    """Pretends to be a 2N on localhost. /api needs digest auth (like a real one), /enu/trigger does not."""
    calls = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Fake2N.calls.append(self.path)
        if self.path.startswith("/api/"):
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Digest") or 'username="api"' not in auth:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Digest realm="HTTP API", qop="auth", nonce="abc123", algorithm=MD5')
                self.end_headers()
                return
            if self.path.startswith("/api/system/info"):
                return self.json({"success": True, "result": {"variant": "2N IP Verso", "swVersion": "2.44.0", "serialNumber": "54-1"}})
            if self.path.startswith("/api/switch/caps"):
                return self.json({"success": True, "result": {"switches": [{"switch": 1, "enabled": True}, {"switch": 2, "enabled": False}]}})
            if self.path.startswith("/api/switch/ctrl"):
                if "switch=9" in self.path:
                    return self.json({"success": False, "error": {"code": 12, "description": "invalid parameter value"}})
                return self.json({"success": True})
        if self.path.startswith("/enu/trigger/redirect"):
            self.send_response(302)
            self.send_header("Location", "https://127.0.0.1/")
            self.end_headers()
            return
        if self.path.startswith("/enu/trigger/"):
            self.send_response(200)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="session")
def fake2n():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake2N)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def env(tmp_path):
    data = tmp_path / "data"
    pw = make_admin(str(data), "admin", "adminpass")
    app = create_app(str(data))
    app.testing = True
    return app, pw


def login(client, user="admin", pw="adminpass"):
    return client.post("/api/login", json={"username": user, "password": pw}, headers=H)


def setup_door(c, host, **step):
    d = c.post("/api/admin/devices", json={"name": "Gate", "host": host, "scheme": "http", "username": "api", "password": "x"}, headers=H).get_json()
    s = {"device_id": d["id"], "kind": "switch", "value": "1", "action": "trigger"} | step
    b = c.post("/api/admin/buttons", json={"name": "Poort 1", "section": "Gates", "steps": [s]}, headers=H).get_json()
    return d["id"], b["id"]


def test_login_and_me(env):
    app, _ = env
    c = app.test_client()
    assert c.get("/api/me").status_code == 401
    assert login(c, pw="wrong").status_code == 401
    assert login(c).status_code == 200
    me = c.get("/api/me").get_json()
    assert me["perms"]["is_admin"] and me["user"]["username"] == "admin"


def test_post_without_header_is_refused(env):
    app, _ = env
    c = app.test_client()
    r = c.post("/api/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 400


def test_login_lockout(env):
    app, _ = env
    c = app.test_client()
    for _ in range(5):
        login(c, pw="nope")
    assert login(c).status_code == 429


def test_open_door_switch_api(env, fake2n):
    app, _ = env
    c = app.test_client()
    login(c)
    _, bid = setup_door(c, fake2n)
    Fake2N.calls.clear()
    r = c.post(f"/api/doors/{bid}/open", headers=H)
    assert r.status_code == 200, r.get_json()
    assert any("/api/switch/ctrl?switch=1&action=trigger" in p for p in Fake2N.calls)
    log = c.get("/api/admin/log").get_json()["log"]
    assert log[0]["event"] == "open" and log[0]["ok"] == 1


def test_open_door_http_trigger_two_devices(env, fake2n):
    app, _ = env
    c = app.test_client()
    login(c)
    d1 = c.post("/api/admin/devices", json={"name": "A", "host": fake2n, "scheme": "http"}, headers=H).get_json()["id"]
    d2 = c.post("/api/admin/devices", json={"name": "B", "host": "http://" + fake2n + "/", "scheme": "http"}, headers=H).get_json()["id"]
    steps = [{"device_id": d1, "kind": "trigger", "value": "hold"}, {"device_id": d2, "kind": "trigger", "value": "hold"}]
    bid = c.post("/api/admin/buttons", json={"name": "Both hold", "steps": steps}, headers=H).get_json()["id"]
    Fake2N.calls.clear()
    assert c.post(f"/api/doors/{bid}/open", headers=H).status_code == 200
    assert Fake2N.calls.count("/enu/trigger/hold") == 2


def test_device_error_is_reported(env, fake2n):
    app, _ = env
    c = app.test_client()
    login(c)
    did = c.post("/api/admin/devices", json={"name": "Gate", "host": fake2n, "scheme": "http", "username": "api", "password": "x"}, headers=H).get_json()["id"]
    # bypass the 1-8 check to hit the device error path
    bid = c.post("/api/admin/buttons", json={"name": "X", "steps": [{"device_id": did, "kind": "path", "value": "/api/switch/ctrl?switch=9&action=on"}]}, headers=H).get_json()["id"]
    r = c.post(f"/api/doors/{bid}/open", headers=H)
    assert r.status_code == 502
    assert "invalid parameter" in r.get_json()["error"]


def test_unreachable_device(env):
    app, _ = env
    c = app.test_client()
    login(c)
    _, bid = setup_door(c, "127.0.0.1:1")
    r = c.post(f"/api/doors/{bid}/open", headers=H)
    assert r.status_code == 502


def test_device_test_button(env, fake2n):
    app, _ = env
    c = app.test_client()
    login(c)
    did, _ = setup_door(c, fake2n)
    r = c.post(f"/api/admin/devices/{did}/test", headers=H).get_json()
    assert r["ok"] and r["info"]["model"] == "2N IP Verso"
    assert r["info"]["switches"][0] == {"switch": 1, "enabled": True}


def test_change_ip_keeps_password(env, fake2n):
    app, _ = env
    c = app.test_client()
    login(c)
    did, _ = setup_door(c, "10.9.9.9")
    r = c.put(f"/api/admin/devices/{did}", json={"name": "Gate", "host": fake2n, "scheme": "http", "username": "api", "password": ""}, headers=H)
    assert r.status_code == 200
    assert app.db.one("SELECT password FROM devices WHERE id = ?", (did,))["password"] == "x"
    devs = c.get("/api/admin/devices").get_json()["devices"]
    assert "password" not in devs[0] and devs[0]["has_password"]


def test_rank_limits_doors_and_admin(env, fake2n):
    app, _ = env
    c = app.test_client()
    login(c)
    _, b1 = setup_door(c, fake2n)
    b2 = c.post("/api/admin/buttons", json={"name": "Office", "steps": []}, headers=H).get_json()["id"]
    rid = c.post("/api/admin/roles", json={"name": "Staff", "rank": 20, "all_doors": False, "buttons": [b1], "days": "1234567"}, headers=H).get_json()["id"]
    assert c.post("/api/admin/users", json={"username": "jan", "password": "secret1", "role_id": rid}, headers=H).status_code == 200

    u = app.test_client()
    assert login(u, "jan", "secret1").status_code == 200
    doors = u.get("/api/doors").get_json()["doors"]
    assert [d["id"] for d in doors] == [b1]
    assert u.post(f"/api/doors/{b2}/open", headers=H).status_code == 403
    assert u.post(f"/api/doors/{b1}/open", headers=H).status_code == 200
    assert u.get("/api/admin/users").status_code == 403
    assert u.get("/api/admin/devices").status_code == 403


def test_time_window_blocks(env, fake2n):
    app, _ = env
    c = app.test_client()
    login(c)
    _, b1 = setup_door(c, fake2n)
    rid = c.post("/api/admin/roles", json={"name": "Never", "rank": 20, "all_doors": True, "days": "1234567",
                                          "time_from": "00:00", "time_to": "00:01"}, headers=H).get_json()["id"]
    # make the window impossible to hit during the test: from == to minus nothing
    app.db.run("UPDATE roles SET time_from = '23:59', time_to = '23:59' WHERE id = ?", (rid,))
    c.post("/api/admin/users", json={"username": "nacht", "password": "secret1", "role_id": rid}, headers=H)
    u = app.test_client()
    login(u, "nacht", "secret1")
    assert u.get("/api/doors").get_json()["blocked"]
    assert u.post(f"/api/doors/{b1}/open", headers=H).status_code == 403


def test_user_manager_cannot_touch_higher_rank(env):
    app, _ = env
    c = app.test_client()
    login(c)
    roles = {r["name"]: r["id"] for r in c.get("/api/admin/roles").get_json()["roles"]}
    mid = c.post("/api/admin/roles", json={"name": "Manager", "rank": 5, "manage_users": True, "all_doors": True, "days": "1234567"}, headers=H).get_json()["id"]
    c.post("/api/admin/users", json={"username": "boss", "password": "secret1", "role_id": mid}, headers=H)
    m = app.test_client()
    login(m, "boss", "secret1")
    # can add a normal user
    assert m.post("/api/admin/users", json={"username": "piet", "password": "secret1", "role_id": roles["User"]}, headers=H).status_code == 200
    # cannot make an admin
    assert m.post("/api/admin/users", json={"username": "evil", "password": "secret1", "role_id": roles["Admin"]}, headers=H).status_code == 403
    admin_id = app.db.one("SELECT id FROM users WHERE username = 'admin'")["id"]
    assert m.delete(f"/api/admin/users/{admin_id}", headers=H).status_code == 403
    # cannot edit ranks
    assert m.post("/api/admin/roles", json={"name": "x", "rank": 3, "days": "1"}, headers=H).status_code == 403


def test_last_admin_protected(env):
    app, _ = env
    c = app.test_client()
    login(c)
    me = c.get("/api/me").get_json()["user"]
    assert c.delete(f"/api/admin/users/{me['id']}", headers=H).status_code == 400
    roles = {r["name"]: r["id"] for r in c.get("/api/admin/roles").get_json()["roles"]}
    a2 = c.post("/api/admin/users", json={"username": "admin2", "password": "secret1", "role_id": roles["Admin"]}, headers=H).get_json()["id"]
    c2 = app.test_client()
    login(c2, "admin2", "secret1")
    # admin2 demotes admin, fine since admin2 stays
    r = c2.put(f"/api/admin/users/{me['id']}", json={"username": "admin", "role_id": roles["User"], "active": True}, headers=H)
    assert r.status_code == 200
    # now admin2 is the last one and cannot be removed by itself
    assert c2.delete(f"/api/admin/users/{a2}", headers=H).status_code == 400
    # the demoted admin got logged out (token changed)
    assert c.get("/api/me").status_code == 401


def test_disabled_user_is_logged_out(env):
    app, _ = env
    c = app.test_client()
    login(c)
    roles = {r["name"]: r["id"] for r in c.get("/api/admin/roles").get_json()["roles"]}
    uid = c.post("/api/admin/users", json={"username": "kim", "password": "secret1", "role_id": roles["User"]}, headers=H).get_json()["id"]
    u = app.test_client()
    login(u, "kim", "secret1")
    assert u.get("/api/me").status_code == 200
    c.put(f"/api/admin/users/{uid}", json={"username": "kim", "role_id": roles["User"], "active": False}, headers=H)
    assert u.get("/api/me").status_code == 401
    assert login(u, "kim", "secret1").status_code == 401


def test_branding_and_logo(env):
    app, _ = env
    c = app.test_client()
    login(c)
    r = c.put("/api/admin/settings", json={"app_name": "Haven Gent", "subtitle": "", "accent": "#0f766e", "language": "nl",
                                           "timezone": "Europe/Brussels", "session_days": "30", "log_days": "90"}, headers=H)
    assert r.status_code == 200
    assert c.get("/manifest.webmanifest").get_json()["name"] == "Haven Gent"
    img = Image.new("RGBA", (400, 120), (255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    r = c.post("/api/admin/logo", data={"logo": (io.BytesIO(buf.getvalue()), "logo.png")}, headers=H, content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["branding"]["logo"]
    assert c.get("/logo.png").status_code == 200
    icon = Image.open(io.BytesIO(c.get("/icon-512.png").data))
    assert icon.size == (512, 512)
    bad = c.post("/api/admin/logo", data={"logo": (io.BytesIO(b"<svg onload=alert(1)>"), "x.svg")}, headers=H, content_type="multipart/form-data")
    assert bad.status_code == 400
    assert c.put("/api/admin/settings", json={"app_name": "x", "accent": "red"}, headers=H).status_code == 400


def test_password_change_logs_out_other_sessions(env):
    app, _ = env
    a = app.test_client()
    b = app.test_client()
    login(a)
    login(b)
    assert a.post("/api/me/password", json={"current": "adminpass", "new": "newpass1"}, headers=H).status_code == 200
    assert a.get("/api/me").status_code == 200
    assert b.get("/api/me").status_code == 401


def test_backup_and_restore(env, tmp_path):
    app, _ = env
    c = app.test_client()
    login(c)
    c.post("/api/admin/devices", json={"name": "Gate", "host": "192.168.1.50"}, headers=H)
    r = c.get("/api/admin/backup")
    assert r.status_code == 200
    f = tmp_path / "b.db"
    f.write_bytes(r.data)
    data2 = tmp_path / "other"
    data2.mkdir()
    restore(str(data2), str(f))
    app2 = create_app(str(data2))
    assert app2.db.one("SELECT host FROM devices")["host"] == "192.168.1.50"


def test_button_validation(env, fake2n):
    app, _ = env
    c = app.test_client()
    login(c)
    did, _ = setup_door(c, fake2n)
    bad = [
        {"device_id": did, "kind": "switch", "value": "0"},
        {"device_id": did, "kind": "trigger", "value": "../../x"},
        {"device_id": 999, "kind": "switch", "value": "1"},
        {"device_id": did, "kind": "switch", "value": "1", "action": "explode"},
    ]
    for s in bad:
        assert c.post("/api/admin/buttons", json={"name": "x", "steps": [s]}, headers=H).status_code == 400, s


def test_pages_served(env):
    app, _ = env
    c = app.test_client()
    assert b"<div id=\"app\">" in c.get("/").data
    assert c.get("/static/app.js").status_code == 200
    assert c.get("/sw.js").status_code == 200
    assert c.get("/apple-touch-icon.png").status_code == 200


def test_https_redirect_is_not_success(env, fake2n):
    app, _ = env
    c = app.test_client()
    login(c)
    _, bid = setup_door(c, fake2n, kind="trigger", value="redirect")
    r = c.post(f"/api/doors/{bid}/open", headers=H)
    assert r.status_code == 502
    assert "https" in r.get_json()["error"]
