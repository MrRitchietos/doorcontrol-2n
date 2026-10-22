# Door Control for 2N

Web app to open doors, gates and locks on 2N IP intercoms and access units from a phone.
Works on Android and iPhone (add it to the home screen and it opens like a normal app).

One install per client. Each install has its own logo, name, color, users and doors.

## Install

On the 2N Access Commander VM (or any Debian/Ubuntu x86_64 machine), as root:

```
curl -fsSL https://raw.githubusercontent.com/MrRitchietos/doorcontrol-2n/main/install.sh | bash
```

At the end it prints the address and the first admin password. Open it on a phone and log in.

Run the same line again to update. Users, doors and settings stay.

The app runs on port **8080** so Access Commander keeps 80 and 443. It brings its own Python in
`/opt/doorcontrol`, data lives in `/var/lib/doorcontrol`. Nothing is installed with apt. When ufw
is on, the port is opened in ufw.

### Options

Add them after `bash -s --`, for example:

```
curl -fsSL https://raw.githubusercontent.com/MrRitchietos/doorcontrol-2n/main/install.sh | bash -s -- --port 8443 --self-signed
```

| Option | |
|---|---|
| `--port 8080` | port for the app |
| `--cert FILE --key FILE` | https with a real certificate (for example the client wildcard) |
| `--self-signed` | https with a self-signed certificate |
| `--http` | back to plain http |
| `--version v1.0.0` | install a tagged version |
| `--uninstall` | remove, keep the data |
| `--purge` | remove everything, data too |

### Https and phones

* **iPhone**: Safari, Share, Add to Home Screen. Works over plain http on the local network.
* **Android**: Chrome only installs it as a real app over https with a trusted certificate.
  Over http it still works, it just opens in a Chrome tab from the home screen icon.

For access from outside, put it behind the firewall's reverse proxy with a real certificate
(pfSense HAProxy + ACME works fine) and point it to `http://<vm-ip>:8080`.

## Setting up a client

1. Log in as admin, tap the gear.
2. **Look**: upload the client logo, set the name and color. The phone icon is made from the logo.
3. **Intercoms**: add every 2N with its IP. Tap Test to see the model, firmware and switches.
4. **Doors**: add a button per door and pick what it does on which intercom.
5. **Ranks**: make groups like "Staff" or "Night shift" with the doors they may open and when.
6. **Users**: add people and give them a rank.

### How a door is opened

A door button can do one or more actions, all at the same time:

* **Switch (HTTP API)**: `/api/switch/ctrl?switch=1&action=trigger`. Needs an HTTP API user on the
  2N (Services > HTTP API > Account) with switch control rights. Use https, that is the 2N default.
* **Automation HTTP trigger**: `/enu/trigger/<name>`, like the old setup (`open`, `hold`, `unhold`).
  Needs an Event.HttpTrigger in the 2N Automation.
* **Other URL path**: any other GET on the device.

When a 2N only answers on https, the app says so instead of reporting a false success.

### Ranks

* Rank 1 is the admin and can do everything. It cannot be removed.
* Other ranks get any mix of: manage users, manage doors and intercoms, change look and settings,
  see the log. Plus which doors, which days and which hours.
* Someone who manages users can only add or change users with a lower rank than their own.
* Disabling a user, changing their rank or password logs them out everywhere.

## Handy commands on the VM

```
doorcontrol reset-admin          # new admin password when locked out
doorcontrol restore backup.db    # put back a backup from Look > Backup, then: systemctl restart doorcontrol
journalctl -u doorcontrol -f     # logs
```

## Development

```
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m doorcontrol --data data reset-admin --password admin123
.venv/bin/python -m doorcontrol --data data serve --port 8080
.venv/bin/python -m pytest
```

Tests use a fake 2N on localhost, no real devices are touched.
