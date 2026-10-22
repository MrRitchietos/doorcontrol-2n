"""Talks to 2N IP intercoms and access units.

Three ways to fire a button step:
  switch   HTTP API  /api/switch/ctrl?switch=N&action=trigger  (needs an API user on the 2N)
  trigger  Automation HTTP trigger  /enu/trigger/<name>  (what the old setup used)
  path     any other path on the device, for odd cases
"""

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests
import urllib3
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIMEOUT = 6
SWITCH_ACTIONS = ("trigger", "on", "off", "lock", "unlock", "hold", "release")


class IntercomError(Exception):
    pass


def base_url(device):
    host = device["host"].strip().rstrip("/")
    host = re.sub(r"^https?://", "", host)
    return f"{device['scheme']}://{host}"


def _get(device, path):
    url = base_url(device) + path
    verify = bool(device.get("verify_tls"))
    try:
        # never follow redirects: a 2N set to https-only answers http with a 302 to its home page,
        # and following that would look like success while nothing was opened
        r = requests.get(url, timeout=TIMEOUT, verify=verify, allow_redirects=False)
        if r.status_code == 401 and device.get("username"):
            challenge = r.headers.get("WWW-Authenticate", "").lower()
            if challenge.startswith("digest"):
                auth = HTTPDigestAuth(device["username"], device["password"])
            else:
                auth = HTTPBasicAuth(device["username"], device["password"])
            r = requests.get(url, timeout=TIMEOUT, verify=verify, auth=auth, allow_redirects=False)
    except requests.exceptions.SSLError:
        raise IntercomError("TLS certificate rejected")
    except requests.exceptions.ConnectTimeout:
        raise IntercomError("no answer (timeout)")
    except requests.exceptions.ConnectionError:
        raise IntercomError("cannot connect")
    except requests.exceptions.Timeout:
        raise IntercomError("timeout")
    if 300 <= r.status_code < 400:
        loc = r.headers.get("Location", "")
        if loc.startswith("https://") and device["scheme"] == "http":
            raise IntercomError("device only answers on https, set the connection to https")
        raise IntercomError(f"device redirected to {loc or 'another page'}, nothing was done")
    if r.status_code == 401:
        raise IntercomError("login refused (check API user)")
    if r.status_code == 403:
        raise IntercomError("no permission for this API user")
    if r.status_code == 404:
        raise IntercomError("not found on device (404)")
    if r.status_code >= 400:
        raise IntercomError(f"device answered HTTP {r.status_code}")
    return r


def _json(r):
    try:
        data = r.json()
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("success") is False:
        err = data.get("error") or {}
        raise IntercomError(err.get("description") or f"device error {err.get('code', '')}".strip())
    return data


def step_path(step):
    kind = step["kind"]
    if kind == "switch":
        action = step["action"] if step["action"] in SWITCH_ACTIONS else "trigger"
        return f"/api/switch/ctrl?switch={int(step['value'])}&action={action}"
    if kind == "trigger":
        return "/enu/trigger/" + quote(step["value"].strip(), safe="")
    path = step["value"].strip()
    return path if path.startswith("/") else "/" + path


def run_step(device, step):
    r = _get(device, step_path(step))
    _json(r)
    return True


def run_steps(pairs):
    """pairs = [(device, step), ...]. Runs them at the same time, like the old double-gate hold.
    Returns a list of error strings (empty = all good)."""
    def one(pair):
        device, step = pair
        try:
            run_step(device, step)
            return None
        except IntercomError as e:
            # only say which intercom when the button talks to more than one
            return f"{device['name']}: {e}" if len(pairs) > 1 else str(e)

    if not pairs:
        return ["no intercom linked to this button"]
    if len(pairs) == 1:
        res = [one(pairs[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(8, len(pairs))) as ex:
            res = list(ex.map(one, pairs))
    return [e for e in res if e]


def device_info(device):
    """Used by the Test button in settings."""
    info = {"model": "", "firmware": "", "serial": "", "switches": []}
    r = _get(device, "/api/system/info")
    data = _json(r) or {}
    res = data.get("result", {}) if isinstance(data, dict) else {}
    info["model"] = res.get("variant") or res.get("deviceName") or ""
    info["firmware"] = res.get("swVersion", "")
    info["serial"] = res.get("serialNumber", "")
    try:
        caps = _json(_get(device, "/api/switch/caps")) or {}
        for s in caps.get("result", {}).get("switches", []):
            info["switches"].append({"switch": s.get("switch"), "enabled": s.get("enabled", False)})
    except IntercomError:
        pass
    return info


def reachable(device):
    """Quick check without credentials, for the HTTP trigger only setups."""
    try:
        requests.get(base_url(device) + "/", timeout=TIMEOUT, verify=False, allow_redirects=False)
        return True
    except requests.exceptions.RequestException:
        return False
