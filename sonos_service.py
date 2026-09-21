import queue
import threading
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

app = FastAPI(title="Sonos Controller")


@dataclass
class Job:
    action: str
    room: str
    station: Optional[str] = None
    done: threading.Event = threading.Event()
    result: Optional[dict] = None
    error: Optional[Exception] = None

    def __post_init__(self):
        self.done = threading.Event()


class SonosController:
    def __init__(self):
        self.jobs = queue.Queue()
        self.ready = threading.Event()
        self.startup_error = None
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
                self.page = (
                    self.context.pages[0]
                    if self.context.pages
                    else self.context.new_page()
                )

                self._ensure_web_app()
                self.ready.set()

                while True:
                    job = self.jobs.get()

                    try:
                        if job.action == "play":
                            self._play(job.room, job.station)
                            job.result = {
                                "ok": True,
                                "room": job.room,
                                "station": job.station,
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
        room: str,
        station: Optional[str] = None,
        timeout: int = 45,
    ):
        if not self.ready.wait(timeout=45):
            raise RuntimeError("Sonos controller did not become ready")

        if self.startup_error:
            raise RuntimeError(
                f"Sonos controller startup failed: {self.startup_error}"
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

        if not self.page.url.startswith(WEB_APP_URL):
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

    def _ensure_room_available(self, room_name):
        active_button = self.page.locator(
            f'button[aria-label="Set {room_name} as active"]'
        )

        try:
            active_button.first.wait_for(
                state="visible",
                timeout=10000,
            )
        except Exception:
            print("Room not visible; refreshing Sonos session...")
            self._ensure_web_app()

            active_button = self.page.locator(
                f'button[aria-label="Set {room_name} as active"]'
            )

            active_button.first.wait_for(
                state="visible",
                timeout=30000,
            )

        return active_button

    def _select_room(self, room_name):
        print(f"Selecting room '{room_name}'...")

        active_button = self._ensure_room_available(room_name)

        active_button.first.click(
            timeout=5000,
            force=True,
        )

        print(f"Room '{room_name}' selected")

    def _close_station_detail_if_open(self):
        about = self.page.get_by_text("About", exact=True)

        if about.count() == 0:
            return

        try:
            if about.first.is_visible():
                self.page.keyboard.press("Escape")
                try:
                    about.first.wait_for(
                        state="hidden",
                        timeout=3000,
                    )
                except Exception:
                    pass
        except Exception:
            pass

    def _favorites(self):
        favorites = self.page.get_by_text(
            "Sonos Favorites",
            exact=True,
        )

        if favorites.count() == 0:
            self.page.keyboard.press("End")

        try:
            favorites.first.wait_for(
                state="visible",
                timeout=10000,
            )
        except Exception:
            for _ in range(20):
                self.page.mouse.wheel(0, 1000)
                favorites = self.page.get_by_text(
                    "Sonos Favorites",
                    exact=True,
                )
                if favorites.count() > 0:
                    break
                self.page.wait_for_timeout(100)

            favorites.first.wait_for(
                state="visible",
                timeout=10000,
            )

        favorites = self.page.get_by_text(
            "Sonos Favorites",
            exact=True,
        )

        try:
            favorites.first.scroll_into_view_if_needed(
                timeout=5000,
            )
        except Exception:
            favorites = self.page.get_by_text(
                "Sonos Favorites",
                exact=True,
            )
            favorites.first.wait_for(
                state="visible",
                timeout=10000,
            )
            favorites.first.scroll_into_view_if_needed(
                timeout=5000,
            )

        return favorites

    def _open_favorite(self, station_name):
        print(f"Opening favorite '{station_name}'...")

        favorites = self._favorites()
        favorites_section = None

        for level in range(1, 8):
            candidate = favorites.first.locator(
                f"xpath=ancestor::*[{level}]"
            )

            try:
                text = candidate.inner_text()

                if station_name in text:
                    favorites_section = candidate
                    break
            except Exception:
                pass

        if favorites_section is None:
            raise RuntimeError(
                f"Favorite '{station_name}' not found"
            )

        station_text = favorites_section.get_by_text(
            station_name,
            exact=True,
        )

        if station_text.count() == 0:
            raise RuntimeError(
                f"Favorite '{station_name}' not found"
            )

        station_text.first.scroll_into_view_if_needed()

        station_card = station_text.first.locator(
            "xpath=ancestor::*[self::button or @role='button'][1]"
        )

        if station_card.count() == 0:
            station_card = station_text.first.locator(
                "xpath=ancestor::*[.//button][1]"
            )

        if station_card.count() == 0:
            raise RuntimeError(
                f"Clickable card for '{station_name}' not found"
            )

        station_card.first.click(
            timeout=5000,
            force=True,
        )

        about = self.page.get_by_text(
            "About",
            exact=True,
        )

        about.wait_for(
            state="visible",
            timeout=10000,
        )

        print(f"Station detail for '{station_name}' opened")

    def _press_station_play(self, station_name):
        about = self.page.get_by_text(
            "About",
            exact=True,
        )

        about.wait_for(
            state="visible",
            timeout=10000,
        )

        detail = None

        for level in range(1, 9):
            candidate = about.locator(
                f"xpath=ancestor::*[{level}]"
            )

            try:
                text = candidate.inner_text()
                buttons = candidate.locator("button")

                if station_name in text and buttons.count() >= 1:
                    detail = candidate
                    break
            except Exception:
                pass

        if detail is None:
            raise RuntimeError(
                f"Station controls for '{station_name}' not found"
            )

        buttons = detail.locator("button")
        about_box = about.bounding_box()
        candidates = []

        for i in range(buttons.count()):
            button = buttons.nth(i)
            aria = button.get_attribute("aria-label") or ""
            title = button.get_attribute("title") or ""
            label = f"{aria} {title}".lower()
            box = button.bounding_box()

            if "play" in label:
                button.click(
                    timeout=5000,
                    force=True,
                )
                print(f"Play clicked for '{station_name}'")
                return

            if box and about_box and box["y"] < about_box["y"]:
                candidates.append((box["x"], button))

        if not candidates:
            raise RuntimeError(
                f"Play control for '{station_name}' not found"
            )

        candidates.sort(key=lambda item: item[0])

        candidates[0][1].click(
            timeout=5000,
            force=True,
        )

        print(f"Play clicked for '{station_name}'")

    def _play(self, room_name, station_name):
        if not station_name:
            raise RuntimeError("Station is required")

        self._close_station_detail_if_open()
        self._select_room(room_name)
        self._open_favorite(station_name)
        self._press_station_play(station_name)
        self._close_station_detail_if_open()


controller = SonosController()


def _check_token(token: str):
    if SONOS_API_TOKEN and token != SONOS_API_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Invalid token",
        )


@app.get("/sonos/play")
def sonos_play(
    room: str = Query(...),
    station: str = Query(...),
    token: str = Query(""),
):
    _check_token(token)

    try:
        return controller.execute(
            "play",
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
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8765,
        log_level="info",
    )
