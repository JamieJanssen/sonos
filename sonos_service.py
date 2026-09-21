import queue
import threading
import uuid
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
        self.favorites = {}

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

                self._ensure_web_app()
                self._wait_for_websocket()
                self._refresh_household()
                self._refresh_groups()
                self._refresh_favorites()

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
                        elif job.action == "favorites":
                            self._refresh_favorites()
                            job.result = {
                                "ok": True,
                                "stations": list(self.favorites.values()),
                            }
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

    def _refresh_favorites(self):
        if not self.household_id:
            self._refresh_household()

        favorites_url = (
            "https://play.sonos.com/api/content/v1/"
            f"households/{self.household_id}/"
            "services/77575/accounts/1/"
            "catalog/containers/%2Fstations%2FSONOS_FAVORITES/"
            "resources?count=100"
        )

        data = self.page.evaluate(
            """async (url) => {
                const response = await fetch(url);

                if (!response.ok) {
                    throw new Error(
                        "Favorites request failed: " + response.status
                    );
                }

                return await response.json();
            }""",
            favorites_url,
        )

        resources = []

        if isinstance(data, dict):
            resources.extend(data.get("resources", []))

            for section in data.values():
                if isinstance(section, dict):
                    resources.extend(section.get("resources", []))

        favorites = {}

        for resource in resources:
            if not isinstance(resource, dict):
                continue

            if isinstance(resource.get("resource"), dict):
                resource = resource["resource"]

            if not resource.get("playable"):
                continue

            station_id = resource.get("id", {})

            if not isinstance(station_id, dict):
                continue

            name = resource.get("name")

            if not name:
                continue

            favorites[name] = {
                "name": name,
                "serviceId": station_id.get("serviceId"),
                "accountId": station_id.get("accountId"),
                "objectId": station_id.get("objectId"),
                "type": resource.get("type"),
            }

        if not favorites:
            raise RuntimeError("No Sonos Radio favorites found")

        self.favorites = favorites
        return self.favorites

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
        if station_name not in self.favorites:
            self._refresh_favorites()

        if station_name not in self.favorites:
            raise RuntimeError(
                f"Unknown station '{station_name}'. "
                f"Available: {', '.join(sorted(self.favorites))}"
            )

        if room_name not in self.groups:
            self._refresh_groups()

        if room_name not in self.groups:
            raise RuntimeError(
                f"Unknown room '{room_name}'. "
                f"Available: {', '.join(sorted(self.groups))}"
            )

        station = self.favorites[station_name]

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
                        "accountId": (
                            "sn_1"
                            if str(station.get("objectId", "")).startswith("tunein:")
                            else station["accountId"]
                        ),
                        "serviceId": (
                            "303"
                            if str(station.get("objectId", "")).startswith("tunein:")
                            else station["serviceId"]
                        ),
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


@app.get("/sonos/favorites")
def sonos_favorites(
    token: str = Query(""),
):
    _check_token(token)
    return _run_controller("favorites")


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
        "stations": sorted(controller.favorites),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8765,
        log_level="info",
    )
