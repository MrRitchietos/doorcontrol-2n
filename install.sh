#!/usr/bin/env bash
# Door Control installer.
#
#   curl -fsSL https://raw.githubusercontent.com/MrRitchietos/doorcontrol-2n/main/install.sh | sudo bash
#
# Run the same line again to update. Settings, users, doors and the certificate are kept.
# The server also updates itself: every hour it checks GitHub and installs what is new.
# When the new version does not start, the previous one is put back.
#
# Options (put them after "bash -s --"):
#   --port 8080              http port (default 8080, 80/443 stay free for Access Commander)
#   --https-port 8443        https port, used once there is a certificate (default 8443)
#   --cert FILE              certificate for https: a .pfx/.p12, or a .crt together with --key
#   --key FILE               private key that goes with --cert
#   --password PW            password of the .pfx or the key
#   --domain NAME            name the phones use, for example door.company.be
#   --self-signed            make a self-signed certificate (Android will not install the app with it)
#   --http                   remove the certificate, plain http only
#   --version v1.0.0         install a tagged version instead of the latest
#   --no-auto-update         do not check GitHub for updates (default: check every hour)
#   --auto-update            switch the hourly update check back on
#   --uninstall              remove the app, keep the data in /var/lib/doorcontrol
#   --purge                  remove the app and the data
#
# The certificate can also be imported later in the app: Settings > HTTPS.
#
# Everything lives in /opt/doorcontrol and /var/lib/doorcontrol. It brings its own Python,
# so nothing is installed or changed with apt and Access Commander is left alone.

set -euo pipefail

REPO="MrRitchietos/doorcontrol-2n"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20260924/cpython-3.12.14%2B20260924-x86_64-unknown-linux-gnu-install_only.tar.gz"
PY_SHA="5eae8cf79dd47fc2496a4fccc892936be831ce7a84d984b2299dfb1cdb592682"
PY_TAG="3.12.14-20260924"

OPT=/opt/doorcontrol
DATA=/var/lib/doorcontrol
ENVF=/etc/doorcontrol.env
UNIT=/etc/systemd/system/doorcontrol.service
SVC_USER=doorcontrol

PORT=""
HTTPS_PORT=""
CERT=""
KEY=""
CERT_PW=""
DOMAIN=""
MODE=""
VERSION="main"
ACTION="install"
AUTO_UPDATE=""
QUIET=""

say()  { [ -n "$QUIET" ] || printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { [ -n "$QUIET" ] || printf '\033[1;32m ok\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:-}"; shift 2 ;;
    --https-port) HTTPS_PORT="${2:-}"; shift 2 ;;
    --cert) CERT="${2:-}"; MODE="cert"; shift 2 ;;
    --key) KEY="${2:-}"; shift 2 ;;
    --password) CERT_PW="${2:-}"; shift 2 ;;
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --self-signed) MODE="self"; shift ;;
    --http) MODE="http"; shift ;;
    --version) VERSION="${2:-}"; shift 2 ;;
    --no-auto-update) AUTO_UPDATE=0; shift ;;
    --auto-update) AUTO_UPDATE=1; shift ;;
    --from-updater) QUIET=1; shift ;;
    --uninstall) ACTION="uninstall"; shift ;;
    --purge) ACTION="purge"; shift ;;
    *) die "unknown option: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (put sudo in front of bash)"
command -v systemctl >/dev/null || die "systemd is needed"

if [ "$ACTION" != "install" ]; then
  say "Removing Door Control"
  systemctl disable --now doorcontrol doorcontrol-update.timer doorcontrol-update.path 2>/dev/null || true
  rm -f "$UNIT" /usr/local/bin/doorcontrol /usr/local/sbin/doorcontrol-update \
    /etc/systemd/system/doorcontrol-update.service /etc/systemd/system/doorcontrol-update.timer \
    /etc/systemd/system/doorcontrol-update.path
  systemctl daemon-reload
  rm -rf "$OPT"
  if [ "$ACTION" = "purge" ]; then
    rm -rf "$DATA" "$ENVF"
    userdel "$SVC_USER" 2>/dev/null || true
    ok "removed, data deleted too"
  else
    ok "removed, data kept in $DATA (use --purge to delete it)"
  fi
  exit 0
fi

[ "$(uname -m)" = "x86_64" ] || die "only x86_64 is supported (this is $(uname -m))"
for c in curl tar sha256sum; do command -v "$c" >/dev/null || die "$c is missing"; done

# keep earlier choices on update
if [ -f "$ENVF" ]; then
  # shellcheck disable=SC1090
  . "$ENVF"
  [ -n "$PORT" ] || PORT="${DOORCONTROL_PORT:-8080}"
  [ -n "$HTTPS_PORT" ] || HTTPS_PORT="${DOORCONTROL_HTTPS_PORT:-8443}"
  [ -n "$AUTO_UPDATE" ] || AUTO_UPDATE="${DOORCONTROL_AUTO_UPDATE:-1}"
fi
AUTO_UPDATE="${AUTO_UPDATE:-1}"
PORT="${PORT:-8080}"
HTTPS_PORT="${HTTPS_PORT:-8443}"
for p in "$PORT" "$HTTPS_PORT"; do
  case "$p" in ''|*[!0-9]*) die "port must be a number: $p" ;; esac
done
[ "$PORT" != "$HTTPS_PORT" ] || die "http and https need different ports"

if [ "$MODE" = "cert" ]; then
  [ -f "$CERT" ] || die "certificate not found: $CERT"
  [ -z "$KEY" ] || [ -f "$KEY" ] || die "key not found: $KEY"
fi

# are the ports free (or already ours)?
port_busy() {
  command -v ss >/dev/null && ss -ltnH "sport = :$1" 2>/dev/null | grep -q .
}
OURS=""
systemctl is-active --quiet doorcontrol 2>/dev/null && OURS=1
for p in "$PORT" "$HTTPS_PORT"; do
  if port_busy "$p" && [ -z "$OURS" ]; then
    die "port $p is already used by something else, pick another one with --port / --https-port"
  fi
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

say "Service user and folders"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$SVC_USER"
mkdir -p "$OPT" "$DATA"
chown "$SVC_USER:$SVC_USER" "$DATA"
chmod 700 "$DATA"
ok "$SVC_USER, $OPT, $DATA"

if [ "$(cat "$OPT/python/.tag" 2>/dev/null)" != "$PY_TAG" ]; then
  say "Downloading Python (only the first time)"
  curl -fsSL --retry 3 -o "$TMP/py.tgz" "$PY_URL"
  echo "$PY_SHA  $TMP/py.tgz" | sha256sum -c --quiet - || die "Python download checksum is wrong"
  rm -rf "$OPT/python" "$OPT/venv"
  mkdir -p "$OPT/python"
  tar -xzf "$TMP/py.tgz" -C "$OPT/python" --strip-components=1
  echo "$PY_TAG" > "$OPT/python/.tag"
  ok "Python $PY_TAG"
fi

say "Downloading Door Control ($VERSION)"
COMMIT=""
if [ -n "${DOORCONTROL_SRC:-}" ]; then
  SRC="$DOORCONTROL_SRC"
  COMMIT="local"
else
  # pin the exact commit, so the update check knows what is installed
  COMMIT="$(curl -fsSL -m 20 -H 'Accept: application/vnd.github.sha' "https://api.github.com/repos/$REPO/commits/$VERSION" || true)"
  case "$COMMIT" in
    [0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) SRC="https://codeload.github.com/$REPO/tar.gz/$COMMIT" ;;
    *) COMMIT=""
       if [ "$VERSION" = "main" ]; then SRC="https://codeload.github.com/$REPO/tar.gz/refs/heads/main"
       else SRC="https://codeload.github.com/$REPO/tar.gz/refs/tags/$VERSION"; fi ;;
  esac
fi
curl -fsSL --retry 3 -o "$TMP/app.tgz" "$SRC" || die "download failed: $SRC"
mkdir -p "$TMP/app"
tar -xzf "$TMP/app.tgz" -C "$TMP/app" --strip-components=1
[ -f "$TMP/app/doorcontrol/app.py" ] || die "download looks wrong"
echo "$COMMIT" > "$TMP/app/.commit"
rm -rf "$OPT/app.new"
mv "$TMP/app" "$OPT/app.new"

say "Installing packages"
[ -x "$OPT/venv/bin/python" ] || "$OPT/python/bin/python3" -m venv "$OPT/venv"
"$OPT/venv/bin/pip" install --quiet --disable-pip-version-check -r "$OPT/app.new/requirements.txt"
# the previous version stays as app.prev, so a broken update can be undone
rm -rf "$OPT/app.prev"
[ -d "$OPT/app" ] && mv "$OPT/app" "$OPT/app.prev"
mv "$OPT/app.new" "$OPT/app"
ok "version $("$OPT/venv/bin/python" -c 'import sys; sys.path.insert(0, "/opt/doorcontrol/app"); import doorcontrol; print(doorcontrol.__version__)')"

cat > "$ENVF" <<EOF
DOORCONTROL_PORT=$PORT
DOORCONTROL_HTTPS_PORT=$HTTPS_PORT
DOORCONTROL_DATA=$DATA
DOORCONTROL_AUTO_UPDATE=$AUTO_UPDATE
EOF

cat > "$UNIT" <<EOF
[Unit]
Description=Door Control for 2N intercoms
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
Group=$SVC_USER
EnvironmentFile=$ENVF
WorkingDirectory=$OPT/app
ExecStart=$OPT/venv/bin/python -m doorcontrol serve
Restart=always
RestartSec=3
AmbientCapabilities=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/bin/doorcontrol <<EOF
#!/bin/sh
# doorcontrol reset-admin | import-cert FILE | https-off | restore FILE | version
set -a; . $ENVF; set +a
cd $OPT/app && exec runuser -u $SVC_USER -- $OPT/venv/bin/python -m doorcontrol "\$@"
EOF
chmod 755 /usr/local/bin/doorcontrol

case "$MODE" in
  cert)
    say "Importing certificate"
    # the service user reads the files from a copy, so they can sit anywhere (also in /root)
    install -o "$SVC_USER" -m 600 "$CERT" "$TMP/cert.in"
    KEYARG=""
    if [ -n "$KEY" ]; then install -o "$SVC_USER" -m 600 "$KEY" "$TMP/key.in"; KEYARG="--key $TMP/key.in"; fi
    chmod 711 "$TMP"
    # shellcheck disable=SC2086
    /usr/local/bin/doorcontrol import-cert "$TMP/cert.in" $KEYARG --password "$CERT_PW" --domain "$DOMAIN" | head -1
    ;;
  self)
    command -v openssl >/dev/null || die "openssl is missing, cannot make a certificate"
    say "Making a self-signed certificate"
    IPS="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9.]+$' | sed 's/^/IP:/' | paste -sd, -)"
    SAN="DNS:$(hostname),DNS:localhost${IPS:+,$IPS}"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$(hostname)" \
      -addext "subjectAltName=$SAN" -keyout "$DATA/tls.key" -out "$DATA/tls.crt" 2>/dev/null
    chown "$SVC_USER:$SVC_USER" "$DATA/tls.key" "$DATA/tls.crt"
    chmod 600 "$DATA/tls.key"
    ;;
  http)
    /usr/local/bin/doorcontrol https-off >/dev/null
    ok "certificate removed"
    ;;
esac

cat > /usr/local/sbin/doorcontrol-update <<'UPD'
#!/bin/bash
# Checks GitHub for a newer Door Control and installs it. Run by doorcontrol-update.timer (hourly)
# and by doorcontrol-update.path when "Update now" is pressed in the app.
set -uo pipefail
REPO="MrRitchietos/doorcontrol-2n"
. /etc/doorcontrol.env
DATA="${DOORCONTROL_DATA:-/var/lib/doorcontrol}"
STATUS="$DATA/update.json"
NOW=""
[ "${1:-}" = "--now" ] && NOW=1
if [ -f "$DATA/update-request" ]; then rm -f "$DATA/update-request"; NOW=1; fi
if [ -z "$NOW" ] && { [ "${DOORCONTROL_AUTO_UPDATE:-1}" != 1 ] || [ -f "$DATA/auto-update-off" ]; }; then
  exit 0
fi

status() {
  printf '{"checked": "%s", "installed": "%s", "latest": "%s", "result": "%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$3" > "$STATUS.tmp"
  chown doorcontrol: "$STATUS.tmp" 2>/dev/null
  mv -f "$STATUS.tmp" "$STATUS"
}

installed="$(cat /opt/doorcontrol/app/.commit 2>/dev/null)"
latest="$(curl -fsSL -m 20 -H 'Accept: application/vnd.github.sha' "https://api.github.com/repos/$REPO/commits/main" 2>/dev/null)"
case "$latest" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
  *) status "$installed" "" "could not reach GitHub"; exit 0 ;;
esac
if [ "$installed" = "$latest" ]; then
  status "$installed" "$latest" "up to date"
  exit 0
fi
status "$installed" "$latest" "updating"
if curl -fsSL -m 60 "https://raw.githubusercontent.com/$REPO/$latest/install.sh" -o /tmp/doorcontrol-install.sh &&
   bash /tmp/doorcontrol-install.sh --from-updater; then
  status "$(cat /opt/doorcontrol/app/.commit 2>/dev/null)" "$latest" "updated"
else
  status "$(cat /opt/doorcontrol/app/.commit 2>/dev/null)" "$latest" "update failed, previous version kept"
  rm -f /tmp/doorcontrol-install.sh
  exit 1
fi
rm -f /tmp/doorcontrol-install.sh
UPD
chmod 755 /usr/local/sbin/doorcontrol-update

cat > /etc/systemd/system/doorcontrol-update.service <<'UPD'
[Unit]
Description=Door Control update check
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/doorcontrol-update
UPD

cat > /etc/systemd/system/doorcontrol-update.timer <<'UPD'
[Unit]
Description=Door Control update check every hour

[Timer]
OnBootSec=10min
OnUnitActiveSec=1h
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
UPD

cat > /etc/systemd/system/doorcontrol-update.path <<UPD
[Unit]
Description=Door Control update requested from the app

[Path]
PathExists=$DATA/update-request

[Install]
WantedBy=multi-user.target
UPD

FIRST_RUN=""
/usr/local/bin/doorcontrol has-users || FIRST_RUN=1

healthy() {
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    systemctl is-active --quiet doorcontrol &&
      curl -fsS -m 3 -o /dev/null "http://127.0.0.1:$PORT/api/branding" && return 0
  done
  return 1
}

say "Starting service"
systemctl daemon-reload
systemctl enable --quiet doorcontrol
systemctl restart doorcontrol
if ! healthy; then
  journalctl -u doorcontrol -n 30 --no-pager
  if [ -d "$OPT/app.prev" ]; then
    rm -rf "$OPT/app.broken"
    mv "$OPT/app" "$OPT/app.broken"
    mv "$OPT/app.prev" "$OPT/app"
    systemctl restart doorcontrol
    healthy && die "new version did not start, the previous version is back"
  fi
  die "service did not start"
fi
systemctl enable --quiet doorcontrol-update.path
systemctl restart doorcontrol-update.path
if [ "$AUTO_UPDATE" = 1 ]; then
  systemctl enable --quiet doorcontrol-update.timer
  systemctl start doorcontrol-update.timer
  ok "auto update on (checks GitHub every hour)"
else
  systemctl disable --quiet --now doorcontrol-update.timer 2>/dev/null || true
  ok "auto update off"
fi

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow "$PORT/tcp" >/dev/null
  ufw allow "$HTTPS_PORT/tcp" >/dev/null
  ok "firewall (ufw) opened for ports $PORT and $HTTPS_PORT"
fi

[ -z "$QUIET" ] || exit 0
echo
ok "Door Control is running"
for ip in $(hostname -I 2>/dev/null); do
  case "$ip" in *:*|172.17.*) continue ;; esac
  echo "    http://$ip:$PORT"
done
if [ -f "$DATA/tls.crt" ]; then
  echo "    https on port $HTTPS_PORT${DOMAIN:+: https://$DOMAIN:$HTTPS_PORT}"
else
  echo "    https: import a certificate in Settings > HTTPS (needed for a real Android app)"
fi
if [ -n "$FIRST_RUN" ]; then
  echo
  echo "    Open the address now and create the admin account."
  echo "    This screen only shows until the first admin is made."
fi
echo
echo "    Update:        run the same install line again"
echo "    Admin locked out:  sudo doorcontrol reset-admin"
echo "    Logs:          journalctl -u doorcontrol -f"
