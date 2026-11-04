
import os
import re
import sys
import json
import ssl
import logging
import smtplib
import http.cookiejar

from pathlib import Path
from datetime import datetime, timedelta
from email.message import EmailMessage
from html.parser import HTMLParser
from urllib.request import (
    Request, build_opener, HTTPCookieProcessor
)
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError
from zoneinfo import ZoneInfo


# Configuration
BASE_URL = "https://www.tnstc.in/OTRSOnline/"
SEARCH_URL = (
    "https://www.tnstc.in/OTRSOnline/"
    "jqreq.do?hiddenAction=SearchService"
)

STATE_FILE = Path("state.json")
TIMEOUT = 30
TIMEZONE = ZoneInfo("Asia/Kolkata")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)


def config(name, default):
    return os.environ.get(name, default)


def journey_dates():
    """Comma-separated DD/MM/YYYY dates to watch."""
    raw = config("JOURNEY_DATES", "09/11/2026,10/11/2026")
    dates = [d.strip() for d in raw.split(",") if d.strip()]

    # Validate the configured journey dates.
    for date in dates:
        datetime.strptime(date, "%d/%m/%Y")

    return dates


def route_description():
    return (
        f"{config('START_PLACE_NAME', 'KARAIKUDI')} -> "
        f"{config('END_PLACE_NAME', 'CHENNAI-KILAMBAKKAM-KCBT')}"
    )


def current_dates():
    today = datetime.now(TIMEZONE).date()

    return (
        today.strftime("%d/%m/%Y"),
        (today + timedelta(days=30)).strftime("%d/%m/%Y")
    )


def load_state():
    if not STATE_FILE.exists():
        return None

    content = STATE_FILE.read_text(encoding="utf-8").strip()

    # Treat an empty or corrupt file as no state.
    if not content:
        return None

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logging.warning("State file is not valid JSON; ignoring it.")
        return None


def save_state(state):
    """Write only when changed, using an atomic replacement."""
    content = json.dumps(
        state, indent=2, sort_keys=True
    ) + "\n"

    if STATE_FILE.exists():
        if STATE_FILE.read_text(encoding="utf-8") == content:
            return

    temp_file = STATE_FILE.with_suffix(".tmp")
    temp_file.write_text(content, encoding="utf-8")
    temp_file.replace(STATE_FILE)

    logging.info("State updated.")


# Parse individual bus blocks
class BusParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.anchors = []
        self.spans = []

        self.in_anchor = False
        self.in_seat_span = False

        self.anchor_text = ""
        self.span_text = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if (
            tag == "a"
            and attrs.get("href", "").startswith("javascript:void(0)")
        ):
            self.in_anchor = True
            self.anchor_text = ""

        if tag == "span":
            classes = attrs.get("class", "").split()

            if "text-1" in classes:
                self.in_seat_span = True
                self.span_text = ""

    def handle_data(self, data):
        if self.in_anchor:
            self.anchor_text += data

        if self.in_seat_span:
            self.span_text += data

    def handle_endtag(self, tag):
        if tag == "a" and self.in_anchor:
            self.anchors.append(self.anchor_text.strip())
            self.in_anchor = False

        if tag == "span" and self.in_seat_span:
            self.spans.append(self.span_text.strip())
            self.in_seat_span = False


def parse_buses(html):
    # Each bus starts with a bus-list div.
    chunks = re.split(
        r'(?=<div\s+class=["\']bus-list["\'])',
        html,
        flags=re.IGNORECASE
    )[1:]

    buses = {}

    for chunk in chunks:
        time_match = re.search(
            r'data-time=["\']([^"\']+)["\']',
            chunk,
            re.IGNORECASE
        )

        type_match = re.search(
            r'data-bus-type=["\']([^"\']+)["\']',
            chunk,
            re.IGNORECASE
        )

        if not time_match or not type_match:
            continue

        parser = BusParser()
        parser.feed(chunk)

        if not parser.anchors:
            continue

        trip_code = parser.anchors[0]

        if not trip_code:
            continue

        # No available-seat text means the bus is full.
        seats = "Full"

        for span in parser.spans:
            match = re.search(
                r"(\d+)\s*Seats?\s+Available",
                span,
                re.IGNORECASE
            )

            if match:
                seats = int(match.group(1))
                break

        buses[trip_code] = {
            "time": time_match.group(1),
            "type": type_match.group(1),
            "seats": seats
        }

    return buses


def fetch_buses(journey_date):
    today, max_date = current_dates()

    # Fresh cookie jar for every run.
    cookie_jar = http.cookiejar.CookieJar()

    opener = build_opener(
        HTTPCookieProcessor(cookie_jar)
    )

    # GET homepage to obtain a fresh JSESSIONID.
    get_request = Request(
        BASE_URL,
        headers={"User-Agent": USER_AGENT}
    )

    with opener.open(get_request, timeout=TIMEOUT) as response:
        response.read()

    logging.info("Homepage loaded; session established.")

    # Configurable journey and locations.
    start_id = config("START_PLACE_ID", "494")
    end_id = config("END_PLACE_ID", "1392")

    start_code = config("START_PLACE_CODE", "KDI")
    end_code = config("END_PLACE_CODE", "KCB")

    start_name = config("START_PLACE_NAME", "KARAIKUDI")
    end_name = config(
        "END_PLACE_NAME",
        "CHENNAI-KILAMBAKKAM-KCBT"
    )

    form = {
        "hiddenStartPlaceID": start_id,
        "hiddenEndPlaceID": end_id,

        "txtStartPlaceCode": start_code,
        "txtEndPlaceCode": end_code,

        "hiddenStartPlaceName": start_name,
        "hiddenEndPlaceName": end_name,

        "matchStartPlace": config(
            "MATCH_START_PLACE", start_name
        ),
        "matchEndPlace": config(
            "MATCH_END_PLACE", end_name
        ),

        "txtJourneyDate": "DD/MM/YYYY",
        "txtReturnDate": "DD/MM/YYYY",

        "hiddenCurrentDate": today,
        "hiddenMaxNoOfPassengers": "16",
        "hiddenMaxValidReservDate": max_date,

        "hiddenOnwardJourneyDate": journey_date,
        "hiddenReturnJourneyDate": "DD/MM/YYYY",

        "selectStartPlace": start_code,
        "selectEndPlace": end_code,

        "languageType": "E",
        "hiddenAction": "SearchService",

        "checkSingleLady": "N",
        "hiddenUserType": "G"
    }

    post_request = Request(
        SEARCH_URL,
        data=urlencode(form).encode("utf-8"),
        headers={
            "User-Agent": USER_AGENT,
            "Origin": "https://www.tnstc.in",
            "Referer": BASE_URL,
            "Content-Type": "application/x-www-form-urlencoded"
        },
        method="POST"
    )

    with opener.open(post_request, timeout=TIMEOUT) as response:
        html = response.read().decode(
            "utf-8", errors="replace"
        )

    buses = parse_buses(html)

    if not buses:
        raise ValueError(
            "No buses parsed. TNSTC page format may have changed."
        )

    return buses


def send_email(subject, body):
    username = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    recipient = os.environ["MAIL_TO"]

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = username
    message["To"] = recipient
    message.set_content(body)

    # Gmail SMTP over implicit TLS.
    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        timeout=TIMEOUT,
        context=ssl.create_default_context()
    ) as server:
        server.login(username, password)
        server.send_message(message)

    logging.info("Email sent: %s", subject)


def format_buses(buses):
    lines = []

    for code, bus in sorted(
        buses.items(),
        key=lambda item: (item[1]["time"], item[0])
    ):
        lines.append(
            f"{bus['time']} {bus['type']} "
            f"(Trip: {code}): {bus['seats']}"
        )

    return lines


def compare_buses(previous, current):
    changes = []

    old_buses = previous["buses"]

    for code in sorted(set(old_buses) | set(current)):
        old = old_buses.get(code)
        new = current.get(code)

        bus = new or old
        description = f"{bus['time']} {bus['type']}"

        if old is None:
            changes.append(
                f"{description}: appeared ({new['seats']} seats)"
                f" [Trip: {code}]"
            )

        elif new is None:
            changes.append(
                f"{description}: disappeared "
                f"(was {old['seats']} seats)"
                f" [Trip: {code}]"
            )

        elif old["seats"] != new["seats"]:
            changes.append(
                f"{description}: {old['seats']} -> {new['seats']}"
                f" [Trip: {code}]"
            )

    return changes


def load_journeys():
    """Per-date state; migrates the old single-date format."""
    state = load_state() or {}

    if "journeys" in state:
        return state["journeys"]

    # Old format held one date's snapshot at the top level.
    if state.get("baseline_recorded"):
        return {config("JOURNEY_DATE", "09/11/2026"): state}

    return {}


def main():
    logging.info("Starting TNSTC watcher.")

    journeys = load_journeys()

    baseline_sections = []
    change_sections = []
    error_dates = []
    network_failed = False

    for journey_date in journey_dates():
        entry = journeys.get(journey_date, {})

        try:
            buses = fetch_buses(journey_date)

        except (URLError, HTTPError, TimeoutError, OSError) as error:
            logging.error(
                "Network/request error for %s: %s", journey_date, error
            )
            network_failed = True
            continue

        except Exception as error:
            logging.exception(
                "Fetching/parsing failed for %s: %s", journey_date, error
            )

            # Notify once; keep the previous snapshot.
            if not entry.get("format_error_notified", False):
                error_dates.append(journey_date)
                entry["format_error_notified"] = True
                journeys[journey_date] = entry

            continue

        # First successful parse establishes the baseline.
        if not entry.get("baseline_recorded", False):
            baseline_sections += (
                [f"Journey date: {journey_date}", ""]
                + format_buses(buses) + [""]
            )
            logging.info("Baseline recorded for %s.", journey_date)

        else:
            changes = compare_buses(entry, buses)

            if changes:
                change_sections += (
                    [f"Journey date: {journey_date}", ""]
                    + changes + [""]
                )
            else:
                logging.info("No availability changes for %s.", journey_date)

        journeys[journey_date] = {
            "buses": buses,
            "baseline_recorded": True,
            "format_error_notified": False
        }

    if baseline_sections:
        send_email(
            "TNSTC Watcher Started",
            "\n".join([
                "TNSTC seat watcher has started.",
                "",
                f"Route: {route_description()}",
                "",
                "Current bus availability:",
                ""
            ] + baseline_sections)
        )

    if change_sections:
        send_email(
            "TNSTC Seat Availability Changed",
            "\n".join([
                "TNSTC seat availability has changed.",
                "",
                f"Route: {route_description()}",
                ""
            ] + change_sections)
        )

    if error_dates:
        send_email(
            "TNSTC Watcher: Parsing Error",
            "The TNSTC page returned zero parseable buses for: "
            + ", ".join(error_dates) + "\n\n"
            "The page format may have changed, or the response "
            "may be unexpected.\n\n"
            "The previous bus snapshot has NOT been replaced.\n"
            "Please inspect the TNSTC website and watcher logs."
        )

    save_state({"journeys": journeys})

    if network_failed:
        # Fail the job so blocked or unreachable runs are visible.
        sys.exit(1)


if __name__ == "__main__":
    main()
