#!/usr/bin/env bash
# Door Control installer.
#
#   curl -fsSL https://raw.githubusercontent.com/MrRitchietos/doorcontrol-2n/main/install.sh | sudo bash
#
# Run the same line again to update. Settings, users and doors are kept.
#
# Options (put them after "bash -s --"):
#   --port 8080              port for the web app (default 8080, 80/443 stay free for Access Commander)
#   --cert FILE --key FILE   serve https with this certificate
#   --self-signed            make a self-signed certificate and serve https
#   --http                   go back to plain http
#   --version v1.0.0         install a tagged version instead of the latest
#   --uninstall              remove the app, keep the data in /var/lib/doorcontrol
#   --purge                  remove the app and the data
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
CERT=""
KEY=""
MODE=""
VERSION="main"
ACTION="install"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:-}"; shift 2 ;;
    --cert) CERT="${2:-}"; MODE="cert"; shift 2 ;;
    --key) KEY="${2:-}"; shift 2 ;;
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
  if [ -z "$MODE" ]; then
    CERT="${DOORCONTROL_CERT:-}"
    KEY="${DOORCONTROL_KEY:-}"
  fi
fi
PORT="${PORT:-8080}"
case "$PORT" in ''|*[!0-9]*) die "port must be a number" ;; esac

if [ "$MODE" = "cert" ]; then
  [ -f "$CERT" ] || die "certificate not found: $CERT"
  [ -f "$KEY" ] || die "key not found: $KEY"
fi
if [ "$MODE" = "http" ]; then CERT=""; KEY=""; fi

# is the port free (or already ours)?
if command -v ss >/dev/null && ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q .; then
  if ! { systemctl is-active --quiet doorcontrol && grep -q "^DOORCONTROL_PORT=$PORT\$" "$ENVF" 2>/dev/null; }; then
    die "port $PORT is already used by something else, pick another one with --port"
  fi
fi

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

if [ "$MODE" = "self" ]; then
  command -v openssl >/dev/null || die "openssl is missing, cannot make a certificate"
  say "Making a self-signed certificate"
  IPS="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9.]+$' | sed 's/^/IP:/' | paste -sd, -)"
  SAN="DNS:$(hostname),DNS:localhost${IPS:+,$IPS}"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$(hostname)" \
    -addext "subjectAltName=$SAN" -keyout "$DATA/tls.key" -out "$DATA/tls.crt" 2>/dev/null
  chown "$SVC_USER:$SVC_USER" "$DATA/tls.key" "$DATA/tls.crt"
  chmod 600 "$DATA/tls.key"
  CERT="$DATA/tls.crt"
  KEY="$DATA/tls.key"
fi

cat > "$ENVF" <<EOF
DOORCONTROL_PORT=$PORT
DOORCONTROL_CERT=$CERT
DOORCONTROL_KEY=$KEY
DOORCONTROL_DATA=$DATA
EOF

if [ -n "$CERT" ] && [ "$MODE" = "cert" ]; then
  # the service user must be able to read them
  install -o "$SVC_USER" -g "$SVC_USER" -m 600 "$CERT" "$DATA/tls.crt"
  install -o "$SVC_USER" -g "$SVC_USER" -m 600 "$KEY" "$DATA/tls.key"
  sed -i "s#^DOORCONTROL_CERT=.*#DOORCONTROL_CERT=$DATA/tls.crt#; s#^DOORCONTROL_KEY=.*#DOORCONTROL_KEY=$DATA/tls.key#" "$ENVF"
  CERT="$DATA/tls.crt"
fi

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
# doorcontrol reset-admin | restore FILE | version
set -a; . $ENVF; set +a
cd $OPT/app && exec runuser -u $SVC_USER -- $OPT/venv/bin/python -m doorcontrol "\$@"
EOF
chmod 755 /usr/local/bin/doorcontrol

FIRST_RUN=""
/usr/local/bin/doorcontrol has-users || FIRST_RUN=1

say "Starting service"
systemctl daemon-reload
systemctl enable --quiet doorcontrol
systemctl restart doorcontrol
sleep 2
systemctl is-active --quiet doorcontrol || { journalctl -u doorcontrol -n 30 --no-pager; die "service did not start"; }

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow "$PORT/tcp" >/dev/null && ok "firewall (ufw) opened for port $PORT"
fi

SCHEME=http
[ -n "$CERT" ] && SCHEME=https
echo
ok "Door Control is running"
for ip in $(hostname -I 2>/dev/null); do
  case "$ip" in *:*|172.17.*) continue ;; esac
  echo "    $SCHEME://$ip:$PORT"
done
if [ -n "$FIRST_RUN" ]; then
  echo
  echo "    Open the address now and create the admin account."
  echo "    This screen only shows until the first admin is made."
fi
echo
echo "    Update:        run the same install line again"
echo "    Admin locked out:  sudo doorcontrol reset-admin"
echo "    Logs:          journalctl -u doorcontrol -f"
