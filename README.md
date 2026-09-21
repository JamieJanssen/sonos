# Sonos HTTP Controller

A lightweight HTTP service for controlling Sonos playback from scripts, Domoticz and Google Home.

The service runs on **Rooster** (`192.168.1.15`) and uses a persistent headless Chromium session to connect to the Sonos Web App. It reuses the Sonos web application's authenticated WebSocket connection instead of talking directly to the speakers.

Current chain:

```text
Google Assistant / Google Home
        |
        v
Google Cloud-to-cloud
        |
        v
https://dzga.jamiejets.com
        |
        v
Jester nginx reverse proxy
        |
        v
topgun.theworkpc.com:16794
        |
        v
Rooster:8181  DZGA-Flask
        |
        v
Domoticz:8080
        |
        v
Virtual switch
        |
        v
Rooster:8765  Sonos HTTP Controller
        |
        v
Sonos Web App / Sonos cloud
        |
        v
Sonos speaker
```

## Sonos HTTP API

The service listens on:

```text
http://192.168.1.15:8765
```

Endpoints:

| Endpoint | Description |
|---|---|
| `GET /sonos/status` | Service status and cached favorite names |
| `GET /sonos/rooms` | Refresh and list available Sonos rooms/groups |
| `GET /sonos/favorites` | Refresh and list Sonos Radio favorites |
| `GET /sonos/play?room=<room>&station=<station>` | Play a favorite |
| `GET /sonos/stop?room=<room>` | Pause playback |

Examples:

```bash
curl "http://192.168.1.15:8765/sonos/status"

curl "http://192.168.1.15:8765/sonos/play?room=Keuken&station=Qmusic"

curl "http://192.168.1.15:8765/sonos/play?room=Keuken&station=Radio+10"

curl "http://192.168.1.15:8765/sonos/play?room=Keuken&station=SLAM%21+Mixmarathon"

curl "http://192.168.1.15:8765/sonos/stop?room=Keuken"
```

Station names currently match the names returned by Sonos and should be treated as case-sensitive. URL-encode special characters where necessary; for example `!` becomes `%21` and `100% NL` becomes `100%25+NL`.

If `SONOS_API_TOKEN` is configured, add `?token=...` or `&token=...` to requests.

## How it works

`sonos_service.py` launches a persistent Playwright Chromium profile and opens:

```text
https://play.sonos.com/en-us/web-app
```

An init script wraps `window.WebSocket` and captures the Sonos cloud WebSocket created by the web app.

At startup the service:

1. opens the Sonos Web App;
2. reuses the existing browser session or logs in;
3. waits for the Sonos WebSocket;
4. retrieves the household;
5. retrieves current room/group IDs;
6. retrieves Sonos Radio favorites.

Room/group IDs are refreshed dynamically; no Sonos group ID is hardcoded.

Favorites are fetched from the Sonos Radio favorites container. TuneIn-based favorites are translated to the playback identity Sonos expects when `loadContent` is sent.

Playback retries up to three times when Sonos returns a transient error. Between attempts the service waits briefly and refreshes group information.

`/sonos/stop` sends a Sonos playback `pause` command.

---

# Rooster setup

Rooster is the Ubuntu server at:

```text
192.168.1.15
```

The repository lives at:

```text
/home/jamie/sonos
```

## 1. Clone repository

```bash
cd ~
git clone https://github.com/JamieJanssen/sonos.git
cd ~/sonos
```

For an existing installation:

```bash
cd ~/sonos
git pull
```

## 2. Python virtual environment

Create a virtual environment:

```bash
cd ~/sonos
python3 -m venv .venv
source .venv/bin/activate
```

Install the Python dependencies:

```bash
pip install --upgrade pip
pip install fastapi uvicorn playwright
```

Install Chromium for Playwright:

```bash
playwright install chromium
```

If Playwright reports missing Ubuntu system libraries:

```bash
sudo .venv/bin/playwright install-deps chromium
```

## 3. Credentials

Create `/home/jamie/sonos/credentials.py`.

**Never commit this file.** It is excluded by `.gitignore`.

Example:

```python
SONOS_EMAIL = "your-sonos-login@example.com"
SONOS_PASSWORD = "your-sonos-password"

# Optional. Leave empty to disable HTTP token checking.
SONOS_API_TOKEN = ""
```

The Playwright browser profile is stored in:

```text
/home/jamie/sonos/profile
```

This directory is also excluded from Git.

## 4. First manual start

Activate the virtual environment and start the service:

```bash
cd ~/sonos
source .venv/bin/activate
python sonos_service.py
```

A healthy startup ends with:

```text
Sonos service ready
```

Test:

```bash
curl "http://127.0.0.1:8765/sonos/status"
```

and from another LAN machine:

```bash
curl "http://192.168.1.15:8765/sonos/status"
```

The Uvicorn server intentionally binds to `0.0.0.0:8765`.

## 5. Start Sonos automatically with systemd

Create:

```text
/etc/systemd/system/sonos.service
```

with:

```ini
[Unit]
Description=Sonos HTTP Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=jamie
WorkingDirectory=/home/jamie/sonos
ExecStart=/home/jamie/sonos/.venv/bin/python /home/jamie/sonos/sonos_service.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sonos.service
```

Check status:

```bash
systemctl status sonos.service --no-pager
```

Follow logs:

```bash
journalctl -u sonos.service -f
```

Restart after an update:

```bash
cd ~/sonos
git pull
sudo systemctl restart sonos.service
```

---

# Domoticz integration

Domoticz runs on Rooster on:

```text
http://192.168.1.15:8080
```

A Dummy hardware device can be used to create virtual switches for stations.

Example virtual switch:

```text
Qmusic Keuken
```

For the switch action, Domoticz can call:

```text
ON:
http://127.0.0.1:8765/sonos/play?room=Keuken&station=Qmusic

OFF:
http://127.0.0.1:8765/sonos/stop?room=Keuken
```

Using `127.0.0.1` here is intentional because Domoticz and the Sonos service run on the same host.

A dzVents alternative is:

```lua
return {
    on = {
        devices = {
            'Qmusic Keuken'
        }
    },

    execute = function(domoticz, device)
        if device.state == 'On' then
            domoticz.openURL({
                url = 'http://127.0.0.1:8765/sonos/play?room=Keuken&station=Qmusic',
                method = 'GET'
            })
        elseif device.state == 'Off' then
            domoticz.openURL({
                url = 'http://127.0.0.1:8765/sonos/stop?room=Keuken',
                method = 'GET'
            })
        end
    end
}
```

---

# DZGA-Flask on Rooster

DZGA-Flask provides the bridge between Google Home and Domoticz.

The Docker Compose directory is:

```text
/home/jamie/dzga
```

DZGA listens locally on:

```text
http://192.168.1.15:8181
```

## Docker

Docker must start automatically:

```bash
sudo systemctl enable --now docker
systemctl is-enabled docker
```

The DZGA Compose configuration should contain:

```yaml
restart: unless-stopped
```

Start or update DZGA:

```bash
cd ~/dzga
docker compose up -d
```

Check status:

```bash
docker compose ps
```

Logs:

```bash
docker compose logs -f dzga-flask
```

Because `restart: unless-stopped` is set, the container is restarted automatically when Docker starts after a reboot.

## DZGA user configuration

The Google-linked DZGA user is:

```text
jamie
```

Its Domoticz URL is:

```text
http://192.168.1.15:8080
```

Configure a valid Domoticz username and password for this user. A missing or incorrect Domoticz login results in a `401 Unauthorized` during Google's `action.devices.SYNC`, and Google receives an empty device list.

Google Assistant must be enabled for the linked DZGA user.

Do not put passwords, OAuth secrets or access tokens in this repository.

## Inspecting DZGA users

DZGA stores its user data in SQLite. To inspect non-secret user information:

```bash
docker exec dzga-flask python3 -c "
import sqlite3
db = sqlite3.connect('/instance/db.sqlite')
for row in db.execute('SELECT id, username, email, domo_url, googleassistant FROM User'):
    print(row)
"
```

Keep only the intended Google-linked account once setup is complete.

---

# Public Google endpoint

The actual DZGA web UI is intended to remain a local management interface on Rooster.

Google reaches only the required endpoints through the public reverse proxy:

```text
https://dzga.jamiejets.com/oauth
https://dzga.jamiejets.com/token
https://dzga.jamiejets.com/smarthome
```

Jester terminates HTTPS and proxies these endpoints to the home connection.

The home-side external forwarding path is:

```text
topgun.theworkpc.com:16794
        ->
192.168.1.15:8181
```

## Jester nginx

The HTTPS server should expose only the three Google/DZGA endpoints:

```nginx
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;

    server_name dzga.jamiejets.com;

    ssl_certificate     /etc/letsencrypt/live/dzga.jamiejets.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/dzga.jamiejets.com/privkey.pem;

    access_log /home/jamie/www/dzga/logs/access.log;
    error_log  /home/jamie/www/dzga/logs/error.log;

    location = /oauth {
        proxy_pass http://topgun.theworkpc.com:16794;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location = /token {
        proxy_pass http://topgun.theworkpc.com:16794;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location = /smarthome {
        proxy_pass http://topgun.theworkpc.com:16794;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location / {
        return 404;
    }
}
```

Keep an HTTP ACME challenge location available for Let's Encrypt renewal:

```nginx
server {
    listen 80;
    listen [::]:80;

    server_name dzga.jamiejets.com;

    root /home/jamie/www/dzga/public;

    location ^~ /.well-known/acme-challenge/ {
        try_files $uri =404;
    }

    location / {
        return 301 https://dzga.jamiejets.com$request_uri;
    }
}
```

After changes:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

The root URL should intentionally return 404:

```bash
curl -I https://dzga.jamiejets.com/
```

The OAuth endpoint should still reach DZGA:

```bash
curl -I https://dzga.jamiejets.com/oauth
```

---

# Google Home Cloud-to-cloud setup

The Google Home Developer project uses a Cloud-to-cloud integration.

Device type for the Domoticz virtual station switch:

```text
Switch
```

OAuth endpoints:

```text
Authorization URL:
https://dzga.jamiejets.com/oauth

Token URL:
https://dzga.jamiejets.com/token
```

Fulfillment URL:

```text
https://dzga.jamiejets.com/smarthome
```

The OAuth Client ID and Client Secret configured in Google must exactly match the values configured in DZGA. Do not store those values in Git.

DZGA usernames are case-sensitive. The same DZGA username used during OAuth linking must have the correct Domoticz URL and credentials.

For a Google Workspace account, **Google Home must be enabled for the user/organizational unit in the Google Workspace Admin Console**. Without this permission, the integration may link incompletely or fail in Google Home.

Once linked, Google sends an `action.devices.SYNC` request to DZGA. A healthy DZGA log should show:

```text
intent: action.devices.SYNC
...
GET /json.htm?type=command&param=getdevices... 200
...
"devices": [...]
```

If the Domoticz request returns `401`, fix the Domoticz credentials stored for the linked DZGA user.

---

# Useful diagnostics

## Sonos

```bash
curl "http://127.0.0.1:8765/sonos/status"
curl "http://127.0.0.1:8765/sonos/rooms"
curl "http://127.0.0.1:8765/sonos/favorites"
```

```bash
journalctl -u sonos.service -f
```

## DZGA

```bash
cd ~/dzga
docker compose ps
docker compose logs -f dzga-flask
```

## Domoticz API

List used devices:

```bash
curl "http://192.168.1.15:8080/json.htm?type=command&param=getdevices&plan=0&filter=all&used=true"
```

When Domoticz authentication is enabled, use valid credentials.

## Jester reverse proxy

```bash
sudo tail -f /home/jamie/www/dzga/logs/access.log
sudo tail -f /home/jamie/www/dzga/logs/error.log
```

## Docker cleanup

List containers:

```bash
docker ps -a
```

Remove stopped test containers:

```bash
docker container prune
```

---

# Security notes

- Never commit `credentials.py`.
- Never commit Sonos passwords, Domoticz passwords, Google OAuth secrets or access tokens.
- Keep the persistent Playwright `profile/` directory out of Git.
- Prefer local `127.0.0.1` access between services on Rooster.
- The public DZGA reverse proxy should expose only `/oauth`, `/token` and `/smarthome`.
- Keep the DZGA management UI accessible only on the trusted LAN.
