import queue
import threading
import uuid
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from playwright.sync_api import sync_playwright

import credentials


SONOS_EMAIL = credentials.SONOS_EMAIL
SONOS_PASSWORD = credentials.SONOS_PASSWORD
SONOS_API_TOKEN = getattr(credentials, "SONOS_API_TOKEN", "")

PROFILE_DIR = "/home/jamie/sonos/profile"
WEB_APP_URL = "https://play.sonos.com/en-us/web-app"
WS_LOG = "/home/jamie/sonos/websocket.log"

STATIONS = {
    "Qmusic": {
        "serviceId": "303",
        "objectId": "tunein:19004",
        "accountId": "sn_1",
    },
    "Radio 10": {
        "serviceId": "303",
        "objectId": "tunein:7073",
        "accountId": "sn_1",
    },
}

WEBSOCKET_HOOK = r"""
(() => {
    const NativeWebSocket = window.WebSocket;

    if (NativeWebSocket.__sonosHookInstalled) {
        return;
    }

    const WrappedWebSocket = new Proxy(NativeWebSocket, {
        construct(target, args) {
            const ws = new target(...args);

            try {
                const url = String(args[0] || "");
                if (url.includes("api.ws.sonos.com/websocket")) {
                    window.__sonosWebSocket = ws;
                    window.__sonosPlaybackStatus =
                        window.__sonosPlaybackStatus || {};

                    ws.addEventListener("message", (event) => {
                        let data;

                        try {
                            data = JSON.parse(event.data);
                        } catch (_) {
                            return;
                        }

                        if (
                            Array.isArray(data) &&
                            data[0] &&
                            data[0].namespace === "playbackExtended" &&
                            data[0].name === "extendedPlaybackStatus" &&
                            data[0].groupId
                        ) {
                            window.__sonosPlaybackStatus[
                                data[0].groupId
                            ] = data[1] || {};
                        }
                    });
                }
            } catch (_) {
            }

            return ws;
        }
    });

    WrappedWebSocket.__sonosHookInstalled = true;
    window.WebSocket = WrappedWebSocket;
})();
"""

app = FastAPI(title="Sonos Controller")


def _log_ws(direction, url, payload):
    if isinstance(payload, bytes):
        payload = payload.hex()

    line = (
        f"{datetime.now().isoformat(timespec='milliseconds')} "
        f"{direction} {url} {payload}"
    )

    with open(WS_LOG, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


@dataclass
class Job:
    action: str
    room: str = ""
    station: Optional[str] = None
    done: threading.Event = None
    result: Optional[dict] = None
    error: Optional[Exception] = None

    def __post_init__(self):
        self.done = threading.Event()


class SonosController:
    def __init__(self):
        self.jobs = queue.Queue()
        self.ready = threading.Event()
        self.startup_error = None
        self.household_id = None
        self.groups = {}

        self.thread = threading.Thread(
            target=self._worker,
            name="sonos-playwright",
            daemon=True,
        )
        self.thread.start()

    def _worker(self):
        try:
            with sync_playwright() as p:
                self.context = p.chromium.launch_persistent_context(
                    user_data_dir=PROFILE_DIR,
                    headless=True,
                )

                self.context.add_init_script(WEBSOCKET_HOOK)

                self.page = (
                    self.context.pages[0]
                    if self.context.pages
                    else self.context.new_page()
                )

                self.page.on("websocket", self._on_websocket)

                self._ensure_web_app()
                self._wait_for_websocket()
                self._refresh_household()
                self._refresh_groups()

                print("Sonos service ready")
                self.ready.set()

                while True:
                    job = self.jobs.get()

                    try:
                        if job.action == "play":
                            job.result = self._play(
                                job.room,
                                job.station,
                            )
                        elif job.action == "rooms":
                            self._refresh_groups()
                            job.result = {
                                "ok": True,
                                "rooms": sorted(self.groups),
                            }
                        elif job.action == "debug_favorites":
                            job.result = self._debug_favorites()
                        else:
                            raise RuntimeError(
                                f"Unsupported action: {job.action}"
                            )
                    except Exception as exc:
                        job.error = exc
                    finally:
                        job.done.set()
                        self.jobs.task_done()

        except Exception as exc:
            self.startup_error = exc
            self.ready.set()

    def execute(
        self,
        action: str,
        room: str = "",
        station: Optional[str] = None,
        timeout: int = 30,
    ):
        if not self.ready.wait(timeout=45):
            raise RuntimeError(
                "Sonos controller did not become ready"
            )

        if self.startup_error:
            raise RuntimeError(
                f"Sonos controller startup failed: "
                f"{self.startup_error}"
            )

        job = Job(
            action=action,
            room=room,
            station=station,
        )

        self.jobs.put(job)

        if not job.done.wait(timeout=timeout):
            raise TimeoutError("Sonos command timed out")

        if job.error:
            raise job.error

        return job.result

    def _on_websocket(self, websocket):
        websocket.on(
            "framesent",
            lambda payload: _log_ws(
                ">>",
                websocket.url,
                payload,
            ),
        )

        websocket.on(
            "framereceived",
            lambda payload: _log_ws(
                "<<",
                websocket.url,
                payload,
            ),
        )

    def _ensure_web_app(self):
        print("Opening Sonos Web App...")

        # Always navigate once after installing the WebSocket hook so the
        # Sonos socket is created through our wrapper.
        self.page.goto(
            WEB_APP_URL,
            wait_until="domcontentloaded",
        )

        if self._has_any_room(timeout=10000):
            print("Existing Sonos session is valid")
            return

        print("Session expired or not available, logging in...")

        self.page.goto(
            "https://login.sonos.com/",
            wait_until="domcontentloaded",
        )

        self.page.get_by_label("Email").fill(SONOS_EMAIL)
        self.page.get_by_label("Password").fill(SONOS_PASSWORD)

        self.page.get_by_role(
            "button",
            name="Sign in",
        ).click()

        self.page.wait_for_function(
            "() => window.location.hostname !== 'login.sonos.com'",
            timeout=30000,
        )

        if "idassets.sonos.com/welcome" in self.page.url:
            self.page.get_by_role(
                "button",
                name="Continue",
            ).click()

        self.page.goto(
            WEB_APP_URL,
            wait_until="domcontentloaded",
        )

        if not self._has_any_room(timeout=30000):
            raise RuntimeError(
                "Logged in, but Sonos rooms did not appear"
            )

        print("Logged in successfully")

    def _has_any_room(self, timeout=5000):
        buttons = self.page.locator(
            'button[aria-label^="Set "][aria-label$=" as active"]'
        )

        try:
            buttons.first.wait_for(
                state="visible",
                timeout=timeout,
            )
            return True
        except Exception:
            return False

    def _wait_for_websocket(self, timeout=15000):
        self.page.wait_for_function(
            """() => {
                const ws = window.__sonosWebSocket;
                return ws && ws.readyState === WebSocket.OPEN;
            }""",
            timeout=timeout,
        )

    def _ws_request(
        self,
        namespace,
        command,
        target=None,
        body=None,
        timeout=10000,
    ):
        self._wait_for_websocket()

        header = {
            "namespace": namespace,
            "command": command,
            "corrId": str(uuid.uuid4()),
        }

        if target:
            header.update(target)

        payload = {
            "header": header,
            "body": body or {},
            "timeout": timeout,
        }

        result = self.page.evaluate(
            """async ({header, body, timeout}) => {
                const ws = window.__sonosWebSocket;

                if (!ws || ws.readyState !== WebSocket.OPEN) {
                    throw new Error("Sonos WebSocket is not open");
                }

                return await new Promise((resolve, reject) => {
                    const timer = setTimeout(() => {
                        ws.removeEventListener("message", onMessage);
                        reject(
                            new Error(
                                "Timed out waiting for Sonos response"
                            )
                        );
                    }, timeout);

                    function cleanup() {
                        clearTimeout(timer);
                        ws.removeEventListener("message", onMessage);
                    }

                    function onMessage(event) {
                        let data;

                        try {
                            data = JSON.parse(event.data);
                        } catch (_) {
                            return;
                        }

                        if (
                            Array.isArray(data) &&
                            data[0] &&
                            data[0].corrId === header.corrId
                        ) {
                            cleanup();
                            resolve(data);
                        }
                    }

                    ws.addEventListener("message", onMessage);

                    ws.send(
                        JSON.stringify([
                            header,
                            body
                        ])
                    );
                });
            }""",
            payload,
        )

        response_header = result[0] if result else {}

        if response_header.get("success") is False:
            raise RuntimeError(
                f"Sonos {namespace}/{command} failed: "
                f"{response_header}"
            )

        return result

    def _refresh_household(self):
        result = self._ws_request(
            namespace="households",
            command="getHouseholds",
            body={
                "connectedOnly": "true",
            },
        )

        households = result[1].get("households", [])

        if not households:
            raise RuntimeError(
                "No connected Sonos household found"
            )

        self.household_id = households[0]["id"]

    def _refresh_groups(self):
        if not self.household_id:
            self._refresh_household()

        result = self._ws_request(
            namespace="groups",
            command="getGroups",
            target={
                "householdId": self.household_id,
            },
            body={
                "includeDeviceInfo": "true",
            },
        )

        groups = result[1].get("groups", [])

        self.groups = {
            group["name"]: group
            for group in groups
            if group.get("name") and group.get("id")
        }

        return self.groups

    def _debug_favorites(self):
        requests = []

        def capture_request(request):
            if request.resource_type in {
                "document",
                "xhr",
                "fetch",
            }:
                requests.append({
                    "type": request.resource_type,
                    "method": request.method,
                    "url": request.url,
                })

        self.page.on("request", capture_request)

        try:
            self.page.goto(
                WEB_APP_URL,
                wait_until="domcontentloaded",
            )

            sonos_radio = self.page.locator(
                'button[aria-label="Sonos Radio"]'
            )

            sonos_radio.first.wait_for(
                state="visible",
                timeout=15000,
            )

            sonos_radio.first.click(
                timeout=5000,
                force=True,
            )

            service_url = self.page.url

            favorites = self.page.get_by_text(
                "Favorites",
                exact=False,
            )

            clicked = False

            for i in range(favorites.count()):
                candidate = favorites.nth(i)

                try:
                    if not candidate.is_visible():
                        continue

                    clickable = candidate.locator(
                        "xpath=ancestor::*[self::button or @role='button' or self::a][1]"
                    )

                    if clickable.count() > 0:
                        clickable.first.click(
                            timeout=5000,
                            force=True,
                        )
                    else:
                        candidate.click(
                            timeout=5000,
                            force=True,
                        )

                    clicked = True
                    break
                except Exception:
                    pass

            if not clicked:
                raise RuntimeError(
                    "Visible Favorites entry not found in Sonos Radio"
                )

            self.page.get_by_text(
                "SLAM!",
                exact=True,
            ).first.wait_for(
                state="visible",
                timeout=15000,
            )

            favorites_url = self.page.url

            # Keep only useful network calls and remove duplicates.
            seen = set()
            useful = []

            for item in requests:
                key = (
                    item["type"],
                    item["method"],
                    item["url"],
                )

                if key in seen:
                    continue

                seen.add(key)
                useful.append(item)

            return {
                "ok": True,
                "service_url": service_url,
                "favorites_url": favorites_url,
                "requests": useful[-100:],
            }

        finally:
            self.page.remove_listener(
                "request",
                capture_request,
            )

    def _wait_for_station_playing(
        self,
        group_id,
        station_name,
        timeout=4000,
    ):
        try:
            self.page.wait_for_function(
                """({groupId, stationName}) => {
                    const status =
                        window.__sonosPlaybackStatus &&
                        window.__sonosPlaybackStatus[groupId];

                    if (!status) {
                        return false;
                    }

                    const playback = status.playback || {};
                    const metadata = status.metadata || {};
                    const container = metadata.container || {};

                    return (
                        playback.playbackState ===
                            "PLAYBACK_STATE_PLAYING" &&
                        container.name === stationName
                    );
                }""",
                arg={
                    "groupId": group_id,
                    "stationName": station_name,
                },
                timeout=timeout,
            )
            return True
        except Exception:
            return False

    def _play(self, room_name, station_name):
        if station_name not in STATIONS:
            raise RuntimeError(
                f"Unknown station '{station_name}'. "
                f"Available: {', '.join(STATIONS)}"
            )

        if room_name not in self.groups:
            self._refresh_groups()

        if room_name not in self.groups:
            raise RuntimeError(
                f"Unknown room '{room_name}'. "
                f"Available: {', '.join(sorted(self.groups))}"
            )

        station = STATIONS[station_name]

        def load_content(group_id):
            return self._ws_request(
                namespace="playback",
                command="loadContent",
                target={
                    "groupId": group_id,
                },
                body={
                    "type": "STREAM",
                    "id": {
                        "objectId": station["objectId"],
                        "accountId": station["accountId"],
                        "serviceId": station["serviceId"],
                    },
                    "playbackAction": "PLAY",
                    "playModes": {
                        "shuffle": False,
                    },
                    "queueAction": "REPLACE",
                },
            )

        group_id = self.groups[room_name]["id"]

        try:
            load_content(group_id)

            if not self._wait_for_station_playing(
                group_id,
                station_name,
            ):
                raise RuntimeError(
                    "Sonos accepted loadContent but playback "
                    "did not switch to the requested station"
                )

        except Exception as first_error:
            # A Sonos load can occasionally fail transiently ("Something went
            # wrong"). Refresh the current group id and retry once.
            print(
                f"Play failed for '{station_name}' on '{room_name}', "
                f"retrying once: {first_error}"
            )

            self._refresh_groups()

            if room_name not in self.groups:
                raise

            group_id = self.groups[room_name]["id"]
            load_content(group_id)

            if not self._wait_for_station_playing(
                group_id,
                station_name,
            ):
                raise RuntimeError(
                    "Sonos playback retry did not switch to "
                    "the requested station"
                )

        print(
            f"Playing '{station_name}' on "
            f"'{room_name}' ({group_id})"
        )

        return {
            "ok": True,
            "room": room_name,
            "station": station_name,
            "groupId": group_id,
        }


controller = SonosController()


def _check_token(token: str):
    if SONOS_API_TOKEN and token != SONOS_API_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Invalid token",
        )


def _run_controller(action, room="", station=None):
    try:
        return controller.execute(
            action,
            room=room,
            station=station,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=str(exc),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        )


@app.get("/sonos/play")
def sonos_play(
    room: str = Query(...),
    station: str = Query(...),
    token: str = Query(""),
):
    _check_token(token)

    return _run_controller(
        "play",
        room=room,
        station=station,
    )


@app.get("/sonos/rooms")
def sonos_rooms(
    token: str = Query(""),
):
    _check_token(token)
    return _run_controller("rooms")


@app.get("/sonos/debug/favorites")
def sonos_debug_favorites(
    token: str = Query(""),
):
    _check_token(token)
    return _run_controller("debug_favorites")


@app.get("/sonos/status")
def sonos_status(
    token: str = Query(""),
):
    _check_token(token)

    if not controller.ready.is_set():
        return {
            "ok": False,
            "status": "starting",
        }

    if controller.startup_error:
        return {
            "ok": False,
            "status": "error",
            "error": str(controller.startup_error),
        }

    return {
        "ok": True,
        "status": "ready",
        "stations": sorted(STATIONS),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8765,
        log_level="info",
    )
