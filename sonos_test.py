import sys
from playwright.sync_api import sync_playwright
from credentials import SONOS_EMAIL, SONOS_PASSWORD

ROOM = sys.argv[1] if len(sys.argv) > 1 else "Keuken"
STATION = sys.argv[2] if len(sys.argv) > 2 else "Radio 10"

SCREENSHOT = "/home/jamie/sonos-debug.png"
HTML = "/home/jamie/sonos-debug.html"


def select_room(page, room_name):
    print(f"\nWaiting for room '{room_name}'...")

    room = page.get_by_text(room_name, exact=True)

    try:
        room.first.wait_for(
            state="visible",
            timeout=30000
        )
    except Exception:
        print(f"\nERROR: Room '{room_name}' did not appear.")

        page.screenshot(
            path="/home/jamie/sonos-room-not-found.png",
            full_page=True
        )

        raise RuntimeError(f"Room '{room_name}' not found")

    print(f"Room '{room_name}' found.")
    print(f"Selecting '{room_name}'...")

    active_button = page.locator(
        f'button[aria-label="Set {room_name} as active"]'
    )

    try:
        active_button.first.wait_for(
            state="visible",
            timeout=5000
        )
    except Exception:
        raise RuntimeError(
            f"Could not find activation button for room '{room_name}'"
        )

    active_button.first.click(
        timeout=5000,
        force=True
    )

    print(f"Room '{room_name}' selected")



def inspect_favorites(page):
    print("\nLooking for Favorites...")

    favorites = page.get_by_text("Sonos Favorites", exact=True)

    try:
        favorites.first.wait_for(
            state="visible",
            timeout=10000
        )
    except Exception:
        print("Sonos Favorites section not found")
        return None

    print("Favorites matches:", favorites.count())
    favorites.first.scroll_into_view_if_needed()

    for level in range(1, 7):
        try:
            section = favorites.first.locator(
                f"xpath=ancestor::*[{level}]"
            )

            text = section.inner_text().strip()

            print(f"\n===== FAVORITES ANCESTOR LEVEL {level} =====\n")
            print(text[:2000])

        except Exception as e:
            print(f"Could not inspect ancestor level {level}:", e)

    return favorites.first


def play_favorite(page, station_name):
    print(f"\nLooking for favorite '{station_name}'...")

    favorites = page.get_by_text("Sonos Favorites", exact=True)

    try:
        favorites.first.wait_for(
            state="visible",
            timeout=10000
        )
    except Exception:
        raise RuntimeError("Sonos Favorites section not found")

    favorites.first.scroll_into_view_if_needed()

    favorites_section = None

    for level in range(1, 8):
        candidate = favorites.first.locator(
            f"xpath=ancestor::*[{level}]"
        )

        try:
            text = candidate.inner_text()

            if station_name in text:
                favorites_section = candidate
                print(
                    f"Found Favorites container at ancestor level {level}"
                )
                break
        except Exception:
            pass

    if favorites_section is None:
        raise RuntimeError(
            f"Could not find Favorites container containing '{station_name}'"
        )

    station_text = favorites_section.get_by_text(
        station_name,
        exact=True
    )

    if station_text.count() == 0:
        raise RuntimeError(
            f"Favorite '{station_name}' not found inside Favorites"
        )

    station_text.first.scroll_into_view_if_needed()

    # Click the actual clickable card/button containing the station,
    # not only the inner text node.
    station_card = station_text.first.locator(
        "xpath=ancestor::*[self::button or @role='button'][1]"
    )

    if station_card.count() == 0:
        station_card = station_text.first.locator(
            "xpath=ancestor::*[.//button][1]"
        )

    if station_card.count() == 0:
        raise RuntimeError(
            f"Could not find clickable card for favorite '{station_name}'"
        )

    print(f"Selecting favorite '{station_name}'...")
    station_card.first.click(timeout=5000, force=True)

    print(f"Favorite '{station_name}' selected")

    # Wait until the station detail view is actually present.
    page.get_by_text(station_name, exact=True).last.wait_for(
        state="visible",
        timeout=10000
    )


def press_station_play(page, station_name):
    print(f"\nLooking for station controls on '{station_name}'...")

    title = page.get_by_text(station_name, exact=True)

    try:
        title.first.wait_for(
            state="visible",
            timeout=10000
        )
    except Exception:
        raise RuntimeError(
            f"Station detail page for '{station_name}' not found"
        )

    detail = None

    for level in range(1, 8):
        candidate = title.first.locator(
            f"xpath=ancestor::*[{level}]"
        )

        try:
            buttons = candidate.locator("button")

            if buttons.count() >= 2:
                detail = candidate
                break
        except Exception:
            pass

    if detail is None:
        raise RuntimeError(
            f"Could not find station controls for '{station_name}'"
        )

    buttons = detail.locator("button")

    print("Station buttons found:", buttons.count())

    for i in range(buttons.count()):
        button = buttons.nth(i)

        try:
            text = button.inner_text()
        except Exception:
            text = ""

        aria = button.get_attribute("aria-label")
        title_attr = button.get_attribute("title")

        print(
            f"Station button {i}: "
            f"text={text!r}, "
            f"aria-label={aria!r}, "
            f"title={title_attr!r}"
        )

    # On the Sonos station detail page the first control is Play
    # and the second control is the overflow ("...") menu.
    play_button = buttons.first

    play_button.click(
        timeout=5000,
        force=True
    )

    print(f"Play clicked for '{station_name}'")


def press_play_for_room(page, room_name):
    print(f"\nChecking playback state for '{room_name}'...")

    room = page.get_by_text(room_name, exact=True)

    if room.count() == 0:
        raise RuntimeError(f"Room '{room_name}' not found")

    room_card = room.first.locator(
        "xpath=ancestor::*[.//button][1]"
    )

    buttons = room_card.locator("button")

    print("\n===== ROOM BUTTONS =====\n")
    print("Buttons found:", buttons.count())

    for i in range(buttons.count()):
        button = buttons.nth(i)

        try:
            text = button.inner_text()
        except Exception:
            text = ""

        aria = button.get_attribute("aria-label")
        title = button.get_attribute("title")

        print(
            f"Button {i}: "
            f"text={text!r}, "
            f"aria-label={aria!r}, "
            f"title={title!r}"
        )

    # If Sonos shows a Stop button, playback is already active.
    stop_button = room_card.locator(
        f'button[aria-label="Stop group {room_name}"]'
    )

    if stop_button.count() > 0:
        print(f"'{room_name}' is already playing")
        return

    # When stopped, Sonos labels the control as "Play group <room>".
    play_button = room_card.locator(
        f'button[aria-label="Play group {room_name}"]'
    )

    if play_button.count() > 0:
        play_button.first.click(timeout=5000)
        print(f"Play clicked for '{room_name}'")
        return

    # Fallback for future Sonos UI changes.
    for i in range(buttons.count()):
        button = buttons.nth(i)

        aria = (button.get_attribute("aria-label") or "").lower()
        title = (button.get_attribute("title") or "").lower()

        if "play" in aria or "play" in title:
            button.click(timeout=5000)
            print(
                f"Play clicked for '{room_name}' using fallback button {i}"
            )
            return

    raise RuntimeError(
        f"No Play/Stop control found for room '{room_name}'"
    )


with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir="/home/jamie/sonos/profile",
        headless=True
    )

    page = context.pages[0] if context.pages else context.new_page()

    print("Opening Sonos Web App...")

    page.goto(
        "https://play.sonos.com/en-us/web-app",
        wait_until="domcontentloaded"
    )

    room = page.get_by_text(ROOM, exact=True)

    try:
        room.first.wait_for(
            state="visible",
            timeout=10000
        )
        print("Existing Sonos session is valid")

    except Exception:
        print("Session expired or not available, logging in...")

        page.goto(
            "https://login.sonos.com/",
            wait_until="domcontentloaded"
        )

        page.get_by_label("Email").fill(SONOS_EMAIL)
        page.get_by_label("Password").fill(SONOS_PASSWORD)

        page.get_by_role(
            "button",
            name="Sign in"
        ).click()

        page.wait_for_function(
            "() => window.location.hostname !== 'login.sonos.com'",
            timeout=30000
        )

        if "idassets.sonos.com/welcome" in page.url:
            print("Clicking Continue...")
            page.get_by_role(
                "button",
                name="Continue"
            ).click()

        page.goto(
            "https://play.sonos.com/en-us/web-app",
            wait_until="domcontentloaded"
        )

        room = page.get_by_text(ROOM, exact=True)
        room.first.wait_for(
            state="visible",
            timeout=30000
        )

        print("Logged in successfully")

    print("Web App URL:", page.url)
    print("Web App title:", page.title())

    select_room(page, ROOM)

    print("\nLoading Sonos Favorites...")

    favorites = page.get_by_text("Sonos Favorites", exact=True)

    if favorites.count() == 0:
        page.keyboard.press("End")

    try:
        favorites.first.wait_for(
            state="visible",
            timeout=10000
        )
    except Exception:
        # Some Sonos layouts lazy-load while scrolling rather than on End.
        for _ in range(20):
            page.mouse.wheel(0, 1000)
            if favorites.count() > 0:
                break
            page.wait_for_timeout(100)

    inspect_favorites(page)
    play_favorite(page, STATION)
    press_station_play(page, STATION)

    page.screenshot(
        path=SCREENSHOT,
        full_page=True
    )

    with open(
        HTML,
        "w",
        encoding="utf-8"
    ) as f:
        f.write(page.content())

    print("\n===== BODY TEXT =====\n")

    try:
        print(page.locator("body").inner_text())
    except Exception as e:
        print("Could not read body text:", e)

    context.close()


print("\nDone")
print("Room:", ROOM)
print("Station:", STATION)
print("Screenshot:", SCREENSHOT)
print("HTML:", HTML)
