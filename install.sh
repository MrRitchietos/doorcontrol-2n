#!/usr/bin/env bash
# Door Control installer.
#
#   curl -fsSL https://raw.githubusercontent.com/MrRitchietos/doorcontrol-2n/main/install.sh | sudo bash
#
# Run the same line again to update. Settings, users, doors and the certificate are kept.
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

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
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
    --uninstall) ACTION="uninstall"; shift ;;
    --purge) ACTION="purge"; shift ;;
    *) die "unknown option: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (put sudo in front of bash)"
command -v systemctl >/dev/null || die "systemd is needed"

if [ "$ACTION" != "install" ]; then
  say "Removing Door Control"
  systemctl disable --now doorcontrol 2>/dev/null || true
  rm -f "$UNIT" /usr/local/bin/doorcontrol
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
fi
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
if [ "$VERSION" = "main" ]; then
  SRC="https://codeload.github.com/$REPO/tar.gz/refs/heads/main"
else
  SRC="https://codeload.github.com/$REPO/tar.gz/refs/tags/$VERSION"
fi
SRC="${DOORCONTROL_SRC:-$SRC}"
curl -fsSL --retry 3 -o "$TMP/app.tgz" "$SRC" || die "download failed: $SRC"
mkdir -p "$TMP/app"
tar -xzf "$TMP/app.tgz" -C "$TMP/app" --strip-components=1
[ -f "$TMP/app/doorcontrol/app.py" ] || die "download looks wrong"
rm -rf "$OPT/app.new"
mv "$TMP/app" "$OPT/app.new"

say "Installing packages"
[ -x "$OPT/venv/bin/python" ] || "$OPT/python/bin/python3" -m venv "$OPT/venv"
"$OPT/venv/bin/pip" install --quiet --disable-pip-version-check -r "$OPT/app.new/requirements.txt"
rm -rf "$OPT/app.old"
[ -d "$OPT/app" ] && mv "$OPT/app" "$OPT/app.old"
mv "$OPT/app.new" "$OPT/app"
rm -rf "$OPT/app.old"
ok "version $("$OPT/venv/bin/python" -c 'import sys; sys.path.insert(0, "/opt/doorcontrol/app"); import doorcontrol; print(doorcontrol.__version__)')"

cat > "$ENVF" <<EOF
DOORCONTROL_PORT=$PORT
DOORCONTROL_HTTPS_PORT=$HTTPS_PORT
DOORCONTROL_DATA=$DATA
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

FIRST_RUN=""
/usr/local/bin/doorcontrol has-users || FIRST_RUN=1

say "Starting service"
systemctl daemon-reload
systemctl enable --quiet doorcontrol
systemctl restart doorcontrol
sleep 2
systemctl is-active --quiet doorcontrol || { journalctl -u doorcontrol -n 30 --no-pager; die "service did not start"; }

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow "$PORT/tcp" >/dev/null
  ufw allow "$HTTPS_PORT/tcp" >/dev/null
  ok "firewall (ufw) opened for ports $PORT and $HTTPS_PORT"
fi

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
