#!/usr/bin/env python3
"""
ledcal - a pixel desk dashboard for a 64x32 RGB LED matrix.

Shows the clock and date, the current weather (Open-Meteo, no API key needed)
and today's agenda from your iCloud / Apple Calendar (CalDAV), rotating
between screens.

Run on the Pi:        sudo venv/bin/python ledcal.py
Preview on any computer (writes PNGs + an animated GIF, no matrix needed):
                      python ledcal.py --preview --demo
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import threading
import time
import tomllib
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageChops, ImageDraw

W, H = 64, 32
log = logging.getLogger("ledcal")

# --------------------------------------------------------------------------
# Colors
# --------------------------------------------------------------------------
WHITE = (240, 240, 240)
GRAY = (120, 120, 120)
DIM = (40, 40, 40)
AMBER = (255, 165, 36)
CYAN = (76, 201, 240)
GREEN = (61, 220, 132)
RED = (255, 70, 80)
SKY = (130, 160, 210)
CLOUD = (190, 198, 212)
SUN = (255, 200, 40)
MOON = (240, 230, 170)
RAIN = (60, 140, 255)
SNOWC = (255, 255, 255)

# Used for calendars when iCloud doesn't report a color.
PALETTE = [CYAN, GREEN, (255, 107, 74), (200, 120, 255), AMBER, (255, 110, 180)]

# --------------------------------------------------------------------------
# 3x5 pixel font. Each glyph = 5 rows, 3 bits per row (4 = left, 1 = right).
# --------------------------------------------------------------------------
GLYPHS = {
    "0": (7, 5, 5, 5, 7), "1": (2, 6, 2, 2, 7), "2": (7, 1, 7, 4, 7), "3": (7, 1, 7, 1, 7),
    "4": (5, 5, 7, 1, 1), "5": (7, 4, 7, 1, 7), "6": (7, 4, 7, 5, 7), "7": (7, 1, 1, 1, 1),
    "8": (7, 5, 7, 5, 7), "9": (7, 5, 7, 1, 7),
    "A": (2, 5, 7, 5, 5), "B": (6, 5, 6, 5, 6), "C": (3, 4, 4, 4, 3), "D": (6, 5, 5, 5, 6),
    "E": (7, 4, 6, 4, 7), "F": (7, 4, 6, 4, 4), "G": (3, 4, 5, 5, 3), "H": (5, 5, 7, 5, 5),
    "I": (7, 2, 2, 2, 7), "J": (1, 1, 1, 5, 2), "K": (5, 5, 6, 5, 5), "L": (4, 4, 4, 4, 7),
    "M": (5, 7, 7, 5, 5), "N": (6, 5, 5, 5, 5), "O": (2, 5, 5, 5, 2), "P": (6, 5, 6, 4, 4),
    "Q": (2, 5, 5, 7, 3), "R": (6, 5, 6, 5, 5), "S": (3, 4, 2, 1, 6), "T": (7, 2, 2, 2, 2),
    "U": (5, 5, 5, 5, 7), "V": (5, 5, 5, 5, 2), "W": (5, 5, 7, 7, 5), "X": (5, 5, 2, 5, 5),
    "Y": (5, 5, 2, 2, 2), "Z": (7, 1, 2, 4, 7),
    " ": (0, 0, 0, 0, 0), ":": (0, 2, 0, 2, 0), "-": (0, 0, 7, 0, 0), ".": (0, 0, 0, 0, 2),
    ",": (0, 0, 0, 2, 4), "'": (2, 2, 0, 0, 0), "!": (2, 2, 2, 0, 2), "?": (7, 1, 2, 0, 2),
    "/": (1, 1, 2, 4, 4), "+": (0, 2, 7, 2, 0), "&": (2, 5, 2, 5, 3), "(": (1, 2, 2, 2, 1),
    ")": (4, 2, 2, 2, 4), "#": (5, 7, 5, 7, 5), "%": (5, 1, 2, 4, 5), "=": (0, 7, 0, 7, 0),
    "°": (2, 5, 2, 0, 0), "_": (0, 0, 0, 0, 7), "@": (7, 5, 7, 4, 7), "*": (5, 2, 7, 2, 5),
}
# Wider glyphs (width, rows) for letters that are unreadable at 3px wide.
GLYPHS.update({
    "M": (5, (17, 27, 21, 17, 17)),
    "W": (5, (17, 17, 21, 27, 17)),
    "N": (4, (9, 13, 11, 9, 9)),
})


def glyph(ch):
    g = GLYPHS.get(ch, GLYPHS[" "])
    return g if isinstance(g[1], tuple) else (3, g)


# Letters with marks drawn in the free pixel row above the glyph (Finnish-friendly).
ACCENTS = {"Ä": ("A", "dots"), "Ö": ("O", "dots"), "Ü": ("U", "dots"), "Å": ("A", "ring")}


def clean(s: str) -> str:
    """Uppercase and map anything the font can't draw to something it can."""
    out = []
    for ch in s.upper():
        if ch in GLYPHS or ch in ACCENTS:
            out.append(ch)
            continue
        base = unicodedata.normalize("NFKD", ch)[:1]
        if base in GLYPHS:
            out.append(base)
        elif ch in "\t\n\r–—":
            out.append(" " if ch in "\t\n\r" else "-")
        else:
            out.append("?" if not ch.isspace() else " ")
    return "".join(out)


def text_width(s: str, scale: int = 1) -> int:
    w = sum(glyph(ACCENTS[c][0] if c in ACCENTS else c)[0] + 1 for c in s)
    return max(0, (w - 1) * scale)


# --------------------------------------------------------------------------
# Drawing surface (a 64x32 PIL image)
# --------------------------------------------------------------------------
class Canvas:
    def __init__(self):
        self.img = Image.new("RGB", (W, H))
        self.px = self.img.load()
        self.clip = (0, W)

    def clear(self):
        self.img.paste((0, 0, 0), (0, 0, W, H))

    def set(self, x, y, c):
        x, y = int(round(x)), int(round(y))
        if self.clip[0] <= x < self.clip[1] and 0 <= y < H:
            self.px[x, y] = c

    def block(self, x, y, s, c):
        for i in range(s):
            for j in range(s):
                self.set(x + i, y + j, c)

    def text(self, s, x, y, c, scale=1):
        """Draw text (already cleaned) and return the x after it."""
        for ch in s:
            mark = None
            if ch in ACCENTS:
                ch, mark = ACCENTS[ch]
            gw, g = glyph(ch)
            for r in range(5):
                for b in range(gw):
                    if g[r] & (1 << (gw - 1 - b)):
                        self.block(x + b * scale, y + r * scale, scale, c)
            if mark == "dots":
                self.block(x, y - scale, scale, c)
                self.block(x + (gw - 1) * scale, y - scale, scale, c)
            elif mark == "ring":
                self.block(x + (gw // 2) * scale, y - scale, scale, c)
            x += (gw + 1) * scale
        return x

    def center(self, s, y, c, scale=1, x0=0, x1=W):
        self.text(s, x0 + (x1 - x0 - text_width(s, scale)) // 2, y, c, scale)

    def hline(self, x0, x1, y, c):
        for x in range(x0, x1):
            self.set(x, y, c)


def marquee(cv: Canvas, s: str, x0: int, x1: int, y: int, c, t: float, speed=14.0, hold=1.5):
    """Draw text clipped to [x0, x1); scroll it back and forth if it doesn't fit."""
    box = x1 - x0
    tw = text_width(s)
    off = 0
    if tw > box:
        travel = tw - box
        period = hold * 2 + travel / speed
        p = t % period
        off = 0 if p < hold else min(travel, (p - hold) * speed)
    old = cv.clip
    cv.clip = (x0, x1)
    cv.text(s, x0 - int(off), y, c)
    cv.clip = old


# --------------------------------------------------------------------------
# Weather icons (drawn in a 16x13 box)
# --------------------------------------------------------------------------
CLOUD_BMP = [
    "....XXX.....",
    "..XXXXXXX...",
    ".XXXXXXXXXX.",
    "XXXXXXXXXXXX",
    "XXXXXXXXXXXX",
    ".XXXXXXXXXX.",
]


def _cloud(cv, x, y, c=CLOUD):
    for j, row in enumerate(CLOUD_BMP):
        for i, ch in enumerate(row):
            if ch == "X":
                cv.set(x + i, y + j, c)


def _disc(cv, cx, cy, r, c):
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r + r * 0.6:
                cv.set(cx + dx, cy + dy, c)


def _sun(cv, cx, cy, r, t, rays=True):
    _disc(cv, cx, cy, r, SUN)
    if rays:
        long_ = int(t) % 2 == 0
        for k in range(8):
            a = k * math.pi / 4
            if k % 2 == 0 or long_:
                d = r + 2
                cv.set(cx + math.cos(a) * d, cy + math.sin(a) * d, AMBER)


def _moon(cv, cx, cy, r):
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r + 1 and (dx - 2) ** 2 + (dy + 1) ** 2 > r * r - 2:
                cv.set(cx + dx, cy + dy, MOON)


def _falling(cv, x, y, t, color, speed, cols):
    for i, col in enumerate(cols):
        fy = (t * speed + i * 2.7) % 6
        cv.set(x + col, y + fy, color)


def draw_icon(cv: Canvas, kind: str, x: int, y: int, t: float, night: bool):
    if kind == "clear":
        if night:
            _moon(cv, x + 7, y + 6, 5)
        else:
            _sun(cv, x + 7, y + 6, 3, t)
    elif kind == "partly":
        if night:
            _moon(cv, x + 10, y + 4, 4)
        else:
            _sun(cv, x + 10, y + 4, 2, t)
        _cloud(cv, x + 1, y + 6)
    elif kind == "cloudy":
        _cloud(cv, x + 4, y + 2, (130, 136, 150))
        _cloud(cv, x + 1, y + 5)
    elif kind == "fog":
        for j, (a, b) in enumerate([(2, 13), (0, 11), (3, 15), (1, 12)]):
            cv.hline(x + a, x + b, y + 2 + j * 3, (150, 155, 165))
    elif kind in ("rain", "drizzle"):
        _cloud(cv, x + 2, y + 1)
        cols = [3, 6, 9, 12] if kind == "rain" else [4, 8, 11]
        _falling(cv, x, y + 8, t, RAIN, 6 if kind == "rain" else 4, cols)
    elif kind == "snow":
        _cloud(cv, x + 2, y + 1)
        _falling(cv, x, y + 8, t, SNOWC, 2.2, [3, 7, 10, 13])
    elif kind == "thunder":
        _cloud(cv, x + 2, y + 1, (140, 145, 160))
        if int(t * 2) % 3 != 0:
            for px, py in [(8, 7), (7, 8), (6, 9), (7, 9), (8, 9), (7, 10), (6, 11)]:
                cv.set(x + px, y + py, SUN)
    else:
        cv.center("?", y + 4, GRAY, 1, x, x + 16)


# WMO weather codes -> (icon, label)
WMO = {
    0: ("clear", "CLEAR"), 1: ("partly", "MOSTLY CLEAR"), 2: ("partly", "PARTLY CLOUDY"),
    3: ("cloudy", "OVERCAST"), 45: ("fog", "FOG"), 48: ("fog", "FREEZING FOG"),
    51: ("drizzle", "LIGHT DRIZZLE"), 53: ("drizzle", "DRIZZLE"), 55: ("drizzle", "HEAVY DRIZZLE"),
    56: ("drizzle", "FREEZ. DRIZZLE"), 57: ("drizzle", "FREEZ. DRIZZLE"),
    61: ("rain", "LIGHT RAIN"), 63: ("rain", "RAIN"), 65: ("rain", "HEAVY RAIN"),
    66: ("rain", "FREEZING RAIN"), 67: ("rain", "FREEZING RAIN"),
    71: ("snow", "LIGHT SNOW"), 73: ("snow", "SNOW"), 75: ("snow", "HEAVY SNOW"),
    77: ("snow", "SNOW GRAINS"), 80: ("rain", "SHOWERS"), 81: ("rain", "SHOWERS"),
    82: ("rain", "HEAVY SHOWERS"), 85: ("snow", "SNOW SHOWERS"), 86: ("snow", "SNOW SHOWERS"),
    95: ("thunder", "THUNDER"), 96: ("thunder", "THUNDER"), 99: ("thunder", "THUNDER"),
}


def temp_color(v: float):
    if v <= -10:
        return (150, 120, 255)
    if v <= 0:
        return CYAN
    if v <= 15:
        return GREEN
    if v <= 25:
        return AMBER
    return RED


def fmt_temp(v: float) -> str:
    n = round(v)
    return f"{0 if n == 0 else n}°"  # avoids "-0°"


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
@dataclass
class Event:
    start: datetime
    end: datetime
    title: str
    all_day: bool
    color: tuple


@dataclass
class Weather:
    temp: float
    code: int
    is_day: bool
    high: float
    low: float


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.weather: Weather | None = None
        self.events: list[Event] = []
        self.games: list = []
        self.weather_ok = 0.0  # time.time() of last success
        self.cal_ok = 0.0
        self.fetching = False
        self.cal_configured = False
        self.started = time.time()

    def stale(self) -> bool:
        """True if live data hasn't refreshed for a while (shown as a red dot)."""
        now = time.time()
        if now - self.started < 180:
            return False
        if not self.fetching:
            return False
        bad_w = now - self.weather_ok > 3600
        bad_c = self.cal_configured and now - self.cal_ok > 1800
        return bad_w or bad_c

    def snapshot(self):
        with self.lock:
            return self.weather, list(self.events), self.weather_ok, self.cal_ok


def http_get(url: str, timeout=20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ledcal/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_weather(cfg) -> Weather:
    loc = cfg["location"]
    q = urllib.parse.urlencode({
        "latitude": loc["latitude"],
        "longitude": loc["longitude"],
        "current": "temperature_2m,weather_code,is_day",
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": loc.get("timezone", "auto"),
        "forecast_days": 1,
    })
    d = json.loads(http_get("https://api.open-meteo.com/v1/forecast?" + q))
    cur, day = d["current"], d["daily"]
    return Weather(
        temp=float(cur["temperature_2m"]),
        code=int(cur["weather_code"]),
        is_day=bool(cur.get("is_day", 1)),
        high=float(day["temperature_2m_max"][0]),
        low=float(day["temperature_2m_min"][0]),
    )


def _parse_color(value) -> tuple | None:
    """'#RRGGBB' or '#RRGGBBAA' -> (r, g, b), brightened a little for LEDs."""
    try:
        s = str(value).strip().lstrip("#")
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except (ValueError, IndexError):
        return None
    m = max(r, g, b, 1)
    k = 255 / m if m < 255 else 1.0  # LEDs look best at full channel intensity
    return (min(255, int(r * k)), min(255, int(g * k)), min(255, int(b * k)))


def expand_calendar(ical_cal, start: datetime, end: datetime, color, tz) -> list[Event]:
    """Expand one icalendar.Calendar (including recurring events) into Events."""
    import recurring_ical_events

    out = []
    for ev in recurring_ical_events.of(ical_cal).between(start, end):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        title = str(ev.get("SUMMARY", "")).strip() or "BUSY"
        ds = ev.get("DTSTART").dt
        de = ev.get("DTEND").dt if ev.get("DTEND") else None
        if de is None and ev.get("DURATION"):
            de = ds + ev.get("DURATION").dt
        if not isinstance(ds, datetime):  # all-day: DTSTART is a date
            s = datetime.combine(ds, dtime(0), tz)
            e = datetime.combine(de if de else ds + timedelta(days=1), dtime(0), tz)
            all_day = True
        else:
            s = (ds if ds.tzinfo else ds.replace(tzinfo=tz)).astimezone(tz)
            if isinstance(de, datetime):
                e = (de if de.tzinfo else de.replace(tzinfo=tz)).astimezone(tz)
            else:
                e = s
            all_day = False
        out.append(Event(s, e, title, all_day, color))
    return out


def fetch_events(cfg, tz) -> list[Event]:
    from icalendar import Calendar as ICal

    today = datetime.now(tz).date()
    start = datetime.combine(today, dtime(0), tz)
    end = start + timedelta(days=2)  # today + tomorrow
    events: list[Event] = []
    ic = cfg.get("icloud", {})

    if ic.get("apple_id") and ic.get("app_password"):
        import caldav
        from caldav.elements import ical as ical_el

        client = caldav.DAVClient(
            url=ic.get("server", "https://caldav.icloud.com/"),
            username=ic["apple_id"],
            password=ic["app_password"],
            timeout=30,
        )
        wanted = {n.strip().lower() for n in ic.get("calendars", []) if n.strip()}
        overrides = {k.lower(): v for k, v in ic.get("colors", {}).items()}
        for i, cal in enumerate(client.principal().calendars()):
            getter = getattr(cal, "get_display_name", None)  # newer caldav versions
            name = ((getter() if getter else cal.name) or "").strip()
            if wanted and name.lower() not in wanted:
                continue
            color = _parse_color(overrides[name.lower()]) if name.lower() in overrides else None
            if color is None:
                try:
                    color = _parse_color(cal.get_property(ical_el.CalendarColor()))
                except Exception:
                    color = None
            color = color or PALETTE[i % len(PALETTE)]
            try:
                results = cal.search(start=start, end=end, event=True)
            except Exception as e:  # e.g. a reminders-only list
                log.debug("skipping calendar %r: %s", name, e)
                continue
            for obj in results:
                events += expand_calendar(obj.icalendar_instance, start, end, color, tz)

    for i, url in enumerate(ic.get("ics_urls", [])):
        url = url.replace("webcal://", "https://", 1)
        cal = ICal.from_ical(http_get(url))
        events += expand_calendar(cal, start, end, PALETTE[(i + 3) % len(PALETTE)], tz)

    seen, unique = set(), []
    for e in sorted(events, key=lambda e: (not e.all_day, e.start, e.title)):
        key = (e.title, e.start, e.all_day)
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


def demo_data(tz) -> tuple[Weather, list[Event]]:
    now = datetime.now(tz).replace(second=0, microsecond=0)
    today = datetime.combine(now.date(), dtime(0), tz)
    m = now.replace(minute=(now.minute // 15) * 15)
    ev = [
        Event(today, today + timedelta(days=1), "Pikkujoulut", True, (200, 120, 255)),
        Event(m - timedelta(minutes=15), m + timedelta(minutes=30), "Team sync", False, CYAN),
        Event(m + timedelta(minutes=35), m + timedelta(minutes=60), "Hammaslääkäri", False, (255, 107, 74)),
        Event(m + timedelta(minutes=70), m + timedelta(minutes=80), "Sauna", False, GREEN),
        Event(today + timedelta(days=1, hours=8, minutes=30),
              today + timedelta(days=1, hours=9), "Standup", False, CYAN),
    ]
    return Weather(temp=-2.4, code=73, is_day=False, high=0.3, low=-4.1), ev


def demo_games(tz) -> list:
    now = datetime.now(tz)
    today = datetime.combine(now.date(), dtime(0), tz)

    def sd(abbr, color, score=None, mine=False):
        return Side(abbr, abbr, color, score, mine)
    blue, red, gold = (0, 85, 255), (228, 0, 43), (255, 190, 30)
    return [
        Game("d1", "NHL", now - timedelta(minutes=50), sd("BOS", gold, "1"), sd("NYR", blue, "2", True),
             "in", "2ND 12:34"),
        Game("d2", "NBA", now - timedelta(hours=3), sd("NY", (53, 121, 255), "110", True), sd("MIA", (200, 0, 40), "104"),
             "post", "FINAL"),
        Game("d3", "EPL", today + timedelta(hours=1), sd("EVE", (60, 120, 255), "1"), sd("LIV", red, "3", True),
             "post", "FT"),
        Game("d4", "LIIGA", today + timedelta(days=1, hours=18, minutes=30), sd("HIFK", red, None, True),
             sd("ÄSS", WHITE), "pre", ""),
        Game("d5", "NFL", today + timedelta(days=2, hours=3, minutes=15), sd("NYG", blue, None, True),
             sd("DAL", (130, 150, 170)), "pre", ""),
    ]


def start_fetchers(cfg, state: State, tz):
    ref = cfg.get("refresh", {})
    state.fetching = True

    def loop(name, fn, minutes, on_ok):
        while True:
            try:
                value = fn()
                with state.lock:
                    on_ok(value)
                log.info("%s updated", name)
                time.sleep((minutes() if callable(minutes) else minutes) * 60)
            except Exception as e:
                log.warning("%s update failed: %s", name, e)
                time.sleep(60)  # retry sooner after an error

    def set_weather(w):
        state.weather, state.weather_ok = w, time.time()

    def set_events(ev):
        state.events, state.cal_ok = ev, time.time()

    threading.Thread(target=loop, daemon=True, args=(
        "weather", lambda: fetch_weather(cfg), ref.get("weather_minutes", 10), set_weather)).start()
    ic = cfg.get("icloud", {})
    state.cal_configured = bool((ic.get("apple_id") and ic.get("app_password")) or ic.get("ics_urls"))
    if cfg.get("sports", {}).get("teams"):
        def set_games(g):
            state.games = g
        threading.Thread(target=loop, daemon=True, args=(
            "sports", lambda: fetch_games(cfg), lambda: sports_interval(state, cfg, tz), set_games)).start()
    if state.cal_configured:
        threading.Thread(target=loop, daemon=True, args=(
            "calendar", lambda: fetch_events(cfg, tz), ref.get("calendar_minutes", 5), set_events)).start()
    else:
        log.warning("No calendar configured - add your iCloud details to config.toml")


# --------------------------------------------------------------------------
# Sports (ESPN public JSON, optional TheSportsDB)
# --------------------------------------------------------------------------
@dataclass
class Side:
    abbr: str
    name: str
    color: tuple
    score: str | None
    mine: bool


@dataclass
class Game:
    id: str
    league: str
    start: datetime
    away: Side
    home: Side
    state: str        # "pre", "in" or "post"
    detail: str       # e.g. "FINAL", "FINAL/OT"


def _team_color(*hexes):
    """Pick the team's color that will look best on LEDs (dark navies/blacks look muddy)."""
    def raw_max(h):
        try:
            return max(int(h[i:i + 2], 16) for i in (0, 2, 4))
        except (TypeError, ValueError):
            return 0
    hexes = [str(h).lstrip("#") for h in hexes if h]
    for min_bright in (90, 30):
        for h in hexes:
            if raw_max(h) >= min_bright:
                return _parse_color(h)
    return WHITE


def _score(v):
    if v is None:
        return None
    if isinstance(v, dict):
        v = v.get("displayValue", v.get("value"))
    try:
        return str(int(float(v)))
    except (TypeError, ValueError):
        return None


def _utc(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    if len(s) == 16:                       # "2026-11-12T00:00" (no seconds, no tz)
        s += ":00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo("UTC"))


ESPN = "https://site.api.espn.com/apis/site/v2/sports/"


def espn_games(team_cfg) -> list[Game]:
    league = team_cfg["league"].strip("/")
    team = str(team_cfg["team"]).lower()
    urls = [f"{ESPN}{league}/teams/{team}/schedule"]
    if league.startswith("soccer/"):
        urls.append(urls[0] + "?fixture=true")   # soccer keeps upcoming fixtures separately
    games = []
    for url in urls:
        data = json.loads(http_get(url))
        my_ids = {str(data.get("team", {}).get("id", "")), team}
        my_abbr = str(data.get("team", {}).get("abbreviation", "")).lower()
        for ev in data.get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            st = (comp.get("status") or ev.get("status") or {}).get("type", {})
            sides = {}
            for c in comp.get("competitors", []):
                t = c.get("team", {})
                abbr = clean(t.get("abbreviation") or t.get("shortDisplayName", "?")[:3])
                sides[c.get("homeAway", "home")] = Side(
                    abbr=abbr,
                    name=t.get("shortDisplayName") or t.get("displayName", abbr),
                    color=_team_color(t.get("color"), t.get("alternateColor")),
                    score=_score(c.get("score")),
                    mine=str(t.get("id", "")) in my_ids or abbr.lower() in (team, my_abbr),
                )
            if "home" not in sides or "away" not in sides:
                continue
            games.append(Game(
                id=f"espn:{ev.get('id')}",
                league=clean(team_cfg.get("label") or league.split("/")[-1].split(".")[0]),
                start=_utc(comp.get("date") or ev["date"]),
                away=sides["away"], home=sides["home"],
                state=st.get("state", "pre"),
                detail=clean(st.get("shortDetail") or st.get("detail") or ""),
            ))
    return games


def tsdb_games(team_cfg, key="123") -> list[Game]:
    tid = str(team_cfg["team"])
    short = {k.lower(): v for k, v in team_cfg.get("short_names", {}).items()}
    base = f"https://www.thesportsdb.com/api/v1/json/{key}/"
    events = []
    for ep, field in (("eventsnext.php", "events"), ("eventslast.php", "results")):
        try:
            events += json.loads(http_get(f"{base}{ep}?id={tid}")).get(field) or []
        except Exception as e:
            log.debug("thesportsdb %s: %s", ep, e)

    def side(name, team_id, score):
        abbr = short.get((name or "").lower()) or clean(name or "?")
        if len(abbr) > 4:
            abbr = abbr[:3]
        mine = str(team_id) == tid
        color = (_parse_color(team_cfg["color"]) if mine and team_cfg.get("color") else None) or (AMBER if mine else WHITE)
        return Side(abbr, name or "?", color, _score(score), mine)

    games = []
    for e in events:
        ts = e.get("strTimestamp") or f"{e.get('dateEvent')}T{(e.get('strTime') or '00:00:00')[:8]}"
        try:
            start = _utc(ts)
        except ValueError:
            continue
        home = side(e.get("strHomeTeam"), e.get("idHomeTeam"), e.get("intHomeScore"))
        away = side(e.get("strAwayTeam"), e.get("idAwayTeam"), e.get("intAwayScore"))
        status = (e.get("strStatus") or "").upper()
        if home.score is not None and away.score is not None and status not in ("NS", "NOT STARTED"):
            state = "post" if status in ("", "FT", "AET", "PEN", "AP", "MATCH FINISHED", "FINISHED") else "in"
        else:
            state = "pre"
        games.append(Game(f"tsdb:{e.get('idEvent')}", clean(team_cfg.get("label") or e.get("strLeague") or ""),
                          start, away, home, state, "FINAL" if state == "post" else ""))
    return games


def fetch_games(cfg) -> list[Game]:
    sp = cfg.get("sports", {})
    games, errors = {}, []
    for tc in sp.get("teams", []):
        try:
            src = tc.get("source", "espn").lower()
            found = tsdb_games(tc, sp.get("thesportsdb_key", "123")) if src == "thesportsdb" else espn_games(tc)
        except Exception as e:
            errors.append(f"{tc.get('team')}: {e}")
            continue
        for g in found:
            if g.id in games:  # two favourite teams playing each other
                old = games[g.id]
                old.home.mine |= g.home.mine
                old.away.mine |= g.away.mine
            else:
                games[g.id] = g
    if errors and not games:
        raise RuntimeError("; ".join(errors))
    for err in errors:
        log.warning("sports: %s", err)
    return sorted(games.values(), key=lambda g: g.start)


def sports_interval(state, cfg, tz) -> float:
    """Minutes until the next sports refresh: every minute while a game is (probably) on."""
    ref = cfg.get("refresh", {})
    now = datetime.now(tz)
    with state.lock:
        games = list(state.games)
    for g in games:
        live = g.state == "in"
        starting = g.state == "pre" and now - timedelta(hours=4) <= g.start <= now + timedelta(minutes=10)
        if live or starting:
            return ref.get("sports_live_minutes", 1)
    return ref.get("sports_minutes", 15)


def reveal_at(start: datetime, reveal: dtime, tz) -> datetime:
    """First moment at `reveal` o'clock (local time) after the game started."""
    local = start.astimezone(tz)
    r = datetime.combine(local.date(), reveal, tz)
    return r if r > local else r + timedelta(days=1)


def games_to_show(games, now, cfg, tz):
    sp = cfg.get("sports", {})
    recent = [g for g in games
              if g.state in ("in", "post") and now - timedelta(hours=24) <= g.start <= now
              and not g.detail.startswith(("POSTP", "CANC"))]
    horizon = now + timedelta(days=int(sp.get("days_ahead", 7)))
    upcoming, seen = [], set()
    for g in games:  # the next game for each of your teams
        if g.state != "pre" or not (now - timedelta(hours=1) <= g.start <= horizon):
            continue
        mine = {s.abbr for s in (g.home, g.away) if s.mine} or {g.id}
        if mine - seen:
            upcoming.append(g)
            seen |= mine
    live = [g for g in recent if g.state == "in"]
    done = [g for g in recent if g.state != "in"]
    return live + done + upcoming[: int(sp.get("max_upcoming", 6))]


def scores_hidden(g: Game, now, cfg, tz) -> bool:
    hide = cfg.get("sports", {}).get("hide_scores_until", "12:00")
    if not hide or g.state in ("pre", "in"):  # live games always show the score
        return False
    return now < reveal_at(g.start, parse_hhmm(hide), tz)


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------
DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def date_label(d: date) -> str:
    return f"{DAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"


def todays_remaining(events, now):
    today = now.date()
    return [e for e in events if e.end > now and e.start.date() <= today]


def next_event_text(events, now):
    """Text + color for the clock screen's bottom line."""
    timed = [e for e in events if not e.all_day and e.end > now]
    for e in sorted(timed, key=lambda e: e.start):
        if e.start <= now:
            return f"NOW {clean(e.title)}", e.color
        prefix = "" if e.start.date() == now.date() else "TMRW "
        return f"{prefix}{e.start:%H:%M} {clean(e.title)}", e.color
    return "NO EVENTS", GRAY


class ClockScreen:
    name = "clock"

    def duration(self, cfg, state, now):
        return cfg["screens"].get("clock_seconds", 12)

    def draw(self, cv, now, t, weather, events, cfg):
        cv.center(date_label(now.date()), 2, AMBER)
        colon = ":" if (now.microsecond < 500_000 or not cfg["display"].get("blink_colon", True)) else " "
        cv.center(f"{now:%H}{colon}{now:%M}", 9, WHITE, 2)
        cv.hline(0, W, 21, DIM)
        txt, col = next_event_text(events, now) if events or cfg.get("_has_cal") else ("", GRAY)
        x0 = 1
        if weather:
            tt = fmt_temp(weather.temp)
            cv.text(tt, 1, 25, temp_color(weather.temp))
            x0 = 1 + text_width(tt) + 3
        if txt:
            if text_width(txt) <= W - x0 - 1:
                cv.center(txt, 25, col, 1, x0, W)
            else:
                marquee(cv, txt, x0, W, 25, col, t)


class WeatherScreen:
    name = "weather"

    def duration(self, cfg, state, now):
        return cfg["screens"].get("weather_seconds", 8)

    def draw(self, cv, now, t, weather, events, cfg):
        if not weather:
            cv.center("WEATHER", 9, GRAY)
            cv.center("LOADING...", 17, DIM)
            return
        icon, label = WMO.get(weather.code, ("?", f"CODE {weather.code}"))
        draw_icon(cv, icon, 1, 2, t, night=not weather.is_day)
        tt = str(0 if round(weather.temp) == 0 else round(weather.temp))
        tw = text_width(tt, 2) + 2 + 3
        x = 18 + (W - 18 - tw) // 2
        x = cv.text(tt, x, 3, temp_color(weather.temp), 2)
        cv.text("°", x, 3, temp_color(weather.temp))
        cv.center(label, 17, SKY)
        hi, lo = fmt_temp(weather.high), fmt_temp(weather.low)
        line = f"H {hi}  L {lo}"
        x = (W - text_width(line)) // 2
        x = cv.text("H ", x, 25, GRAY)
        x = cv.text(hi, x, 25, temp_color(weather.high))
        x = cv.text("  L ", x, 25, GRAY)
        cv.text(lo, x, 25, temp_color(weather.low))


class AgendaScreen:
    name = "agenda"
    ROWS = 3

    def _pages(self, events, now):
        rem = todays_remaining(events, now)
        return [rem[i:i + self.ROWS] for i in range(0, len(rem), self.ROWS)] or [[]]

    def duration(self, cfg, state, now):
        _, events, _, _ = state.snapshot()
        return cfg["screens"].get("agenda_page_seconds", 7) * len(self._pages(events, now))

    def draw(self, cv, now, t, weather, events, cfg):
        page_s = cfg["screens"].get("agenda_page_seconds", 7)
        pages = self._pages(events, now)
        p = min(int(t // page_s), len(pages) - 1)
        tp = t - p * page_s  # time since this page appeared (for scrolling)
        cv.text("TODAY", 1, 1, AMBER)
        if len(pages) > 1:
            ind = f"{p + 1}/{len(pages)}"
            cv.text(ind, W - text_width(ind) - 1, 1, GRAY)

        if not pages[0]:
            if not cfg.get("_has_cal"):
                cv.center("ADD CALENDAR", 12, GRAY)
                cv.center("IN CONFIG", 20, DIM)
                return
            cv.center("ALL CLEAR", 10, GREEN)
            tomorrow = [e for e in events if e.start.date() > now.date()]
            if tomorrow:
                e = tomorrow[0]
                when = "ALL DAY" if e.all_day else f"{e.start:%H:%M}"
                cv.center(f"TMRW {when}", 18, GRAY)
                title = clean(e.title)
                if text_width(title) > W - 4:
                    marquee(cv, title, 2, W - 2, 25, e.color, tp)
                else:
                    cv.center(title, 25, e.color)
            return

        for i, e in enumerate(pages[p]):
            y = 9 + i * 8
            ongoing = not e.all_day and e.start <= now < e.end
            for k in range(5):
                cv.set(0, y + k, e.color)
            if e.all_day:
                cv.text("ALL", 3, y, GRAY)
            elif ongoing:
                cv.text("NOW", 3, y, GREEN if int(t * 2) % 2 == 0 else (30, 110, 66))
            else:
                cv.text(f"{e.start:%H:%M}", 3, y, GRAY)
            marquee(cv, clean(e.title), 25, W, y, e.color, tp + i * 0.7)


class GamesScreen:
    """One compact page: last 24 h results (spoiler-safe) and the next game for each team."""
    name = "games"
    state = None  # set in main()
    ROWS = 4

    def _rows(self, now, cfg):
        if not self.state:
            return []
        with self.state.lock:
            games = list(self.state.games)
        shown = games_to_show(games, now, cfg, now.tzinfo)
        live = [g for g in shown if g.state == "in"]
        done = sorted((g for g in shown if g.state == "post"), key=lambda g: g.start, reverse=True)
        upcoming = [g for g in shown if g.state == "pre"]
        return (live + done[:2] + upcoming + done[2:])[: self.ROWS]

    def duration(self, cfg, state, now):
        return cfg["screens"].get("games_seconds", 8) if self._rows(now, cfg) else 0

    def draw(self, cv, now, t, weather, events, cfg):
        tz = now.tzinfo
        for i, g in enumerate(self._rows(now, cfg)):
            y = 1 + i * 8
            mine, other = (g.away, g.home) if g.away.mine and not g.home.mine else (g.home, g.away)
            if g.state == "pre":
                start = g.start.astimezone(tz)
                today = start.date() == now.date()
                cv.text(DAYS[start.weekday()], 0, y, GREEN if today else AMBER)
                cv.text(f"{start:%H:%M}", 14, y, GRAY)
                x = cv.text(mine.abbr, 36, y, mine.color)
                ox = W - text_width(other.abbr)
                if ox >= x + 3:
                    cv.text(other.abbr, ox, y, tuple(v // 3 for v in other.color))
                continue
            # result or live game: MINE  score  OTHER
            cv.text(mine.abbr, 0, y, mine.color)
            cv.text(other.abbr, W - text_width(other.abbr), y, tuple(v // 2 for v in other.color))
            if scores_hidden(g, now, cfg, tz) or mine.score is None or other.score is None:
                score, col = "?-?", GRAY
            else:
                score = f"{mine.score}-{other.score}"
                ms, os_ = int(mine.score), int(other.score)
                col = RED if g.state == "in" else GREEN if ms > os_ else (255, 80, 80) if ms < os_ else AMBER
            if g.state == "in" and int(t * 2) % 2 == 0:
                col = (140, 30, 30)  # live scores pulse
            cv.center(score, y, col, 1, 17, W - 17)


class LiveScreen:
    """Big scoreboard, shown only while one of your games is live."""
    name = "live"
    state = None  # set in main()

    def _games(self, now, cfg):
        if not self.state:
            return []
        with self.state.lock:
            games = list(self.state.games)
        return [g for g in games_to_show(games, now, cfg, now.tzinfo) if g.state == "in"]

    def duration(self, cfg, state, now):
        return cfg["screens"].get("live_seconds", 8) * len(self._games(now, cfg))

    def draw(self, cv, now, t, weather, events, cfg):
        games = self._games(now, cfg)
        if not games:
            cv.center("NO GAMES", 13, GRAY)
            return
        per = cfg["screens"].get("live_seconds", 8)
        i = min(int(t // per), len(games) - 1)
        g = games[i]
        tz = now.tzinfo
        start = g.start.astimezone(tz)
        hidden = scores_hidden(g, now, cfg, tz)

        # Top line: league + when / status
        if g.state == "pre":
            days = (start.date() - now.date()).days
            when = "TODAY" if days == 0 else "TMRW" if days == 1 else DAYS[start.weekday()]
            top = f"{g.league} {when} {start:%H:%M}"
        elif g.state == "in":
            top = f"{g.league} LIVE"
        else:
            top = f"{g.league} {g.detail or 'FINAL'}"
        if text_width(top) > W - 2:
            top = top[len(g.league) + 1:]
        cv.center(top, 1, AMBER if g.state != "in" else RED)

        # Middle: AWAY  score  HOME  (big team names when they fit)
        a, h = g.away, g.home
        big = text_width(a.abbr, 2) <= 24 and text_width(h.abbr, 2) <= 24
        sc, y = (2, 9) if big else (1, 12)
        ca = a.color if (a.mine or not (h.mine)) else tuple(v // 2 for v in a.color)
        ch = h.color if (h.mine or not (a.mine)) else tuple(v // 2 for v in h.color)
        cv.text(a.abbr, 1, y, ca, sc)
        cv.text(h.abbr, W - 1 - text_width(h.abbr, sc), y, ch, sc)
        mid_y = 12
        if g.state == "pre":
            cv.center("VS", mid_y, GRAY)
        elif hidden or a.score is None or h.score is None:
            cv.center("?-?", mid_y, GRAY)
        else:
            cv.center(f"{a.score}-{h.score}", mid_y, WHITE)

        # Bottom line
        if g.state == "pre":
            delta = start - now
            if delta < timedelta(hours=1):
                msg, col = f"IN {max(1, int(delta.total_seconds() // 60))} MIN", GREEN
            elif start.date() == now.date():
                msg, col = "TONIGHT" if start.hour >= 17 else "TODAY", GREEN
            elif h.mine != a.mine:
                msg, col = ("HOME GAME" if h.mine else "AWAY GAME"), GRAY
            else:
                msg, col = f"{start.day}.{start.month}.", GRAY
        elif g.state == "in":
            # period / clock from the feed, e.g. "2ND 12:34", "BOT 5TH", "45'+2"
            msg = g.detail if g.detail and text_width(g.detail) <= W - 6 else "LIVE"
            col = RED
            if int(t * 2) % 2 == 0:  # blinking "on air" dot
                for dx in range(2):
                    for dy in range(2):
                        cv.set(1 + dx, 26 + dy, RED)
        elif hidden:
            reveal = cfg.get("sports", {}).get("hide_scores_until", "12:00")
            msg, col = f"SCORE AT {reveal}", GRAY
        else:
            mine = a if a.mine else h if h.mine else None
            msg, col = "FINAL", GRAY
            if mine and a.score is not None and h.score is not None:
                other = h if mine is a else a
                ms, os_ = int(mine.score), int(other.score)
                msg, col = (("WIN", GREEN) if ms > os_ else ("LOSS", RED) if ms < os_ else ("DRAW", AMBER))
                msg = f"{mine.abbr} {msg}"
        cv.center(msg, 25, col)
        if len(games) > 1:  # page dots
            for k in range(len(games)):
                cv.set(W // 2 - len(games) + k * 2, 31, AMBER if k == i else DIM)


SCREENS = {"clock": ClockScreen(), "weather": WeatherScreen(), "agenda": AgendaScreen(),
           "games": GamesScreen(), "live": LiveScreen()}
SCREENS["sports"] = SCREENS["games"]  # older configs


# --------------------------------------------------------------------------
# Output: real matrix or PNG/GIF preview
# --------------------------------------------------------------------------
def parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def brightness_now(cfg, now: datetime) -> int:
    d = cfg["display"]
    day_b = int(d.get("brightness", 60))
    if "night_start" not in d:
        return day_b
    ns, ne, t = parse_hhmm(d["night_start"]), parse_hhmm(d.get("night_end", "07:00")), now.time()
    night = (t >= ns or t < ne) if ns > ne else (ns <= t < ne)
    return int(d.get("night_brightness", 15)) if night else day_b


class MatrixOut:
    def __init__(self, cfg):
        from rgbmatrix import RGBMatrix, RGBMatrixOptions

        d = cfg["display"]
        o = RGBMatrixOptions()
        o.rows, o.cols = 32, 64
        o.hardware_mapping = d.get("hardware_mapping", "adafruit-hat")
        o.gpio_slowdown = int(d.get("gpio_slowdown", 4))
        o.brightness = int(d.get("brightness", 60))
        o.disable_hardware_pulsing = bool(d.get("disable_hardware_pulsing", False))
        o.drop_privileges = False  # keep root so the venv in /home stays readable
        if d.get("pwm_bits"):
            o.pwm_bits = int(d["pwm_bits"])
        self.matrix = RGBMatrix(options=o)
        self.off = self.matrix.CreateFrameCanvas()
        self.rotate = int(d.get("rotate", 0))

    def show(self, img: Image.Image, brightness: int):
        if brightness <= 0:
            self.off.Clear()
        else:
            try:
                self.off.brightness = brightness
            except AttributeError:
                self.matrix.brightness = brightness
            self.off.SetImage(img.rotate(self.rotate) if self.rotate else img)
        self.off = self.matrix.SwapOnVSync(self.off)


def led_render(img: Image.Image, scale=10) -> Image.Image:
    """Upscale a 64x32 frame so it looks like a physical LED panel."""
    up = img.resize((W * scale, H * scale), Image.NEAREST)
    up = ImageChops.lighter(up, Image.new("RGB", up.size, (24, 24, 24)))  # unlit LEDs
    mask = Image.new("L", (scale, scale))
    ImageDraw.Draw(mask).ellipse((1, 1, scale - 2, scale - 2), fill=255)
    full = Image.new("L", up.size)
    for y in range(H):
        for x in range(W):
            full.paste(mask, (x * scale, y * scale))
    return Image.composite(up, Image.new("RGB", up.size, (8, 8, 8)), full)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
DEFAULTS = {
    "location": {"latitude": 60.1699, "longitude": 24.9384, "timezone": "Europe/Helsinki"},
    "icloud": {},
    "display": {"brightness": 60, "fps": 30},
    "screens": {"order": ["clock", "agenda", "weather", "games", "live"]},
    "refresh": {},
}


def load_config(path: Path) -> dict:
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    if path.exists():
        with open(path, "rb") as f:
            user = tomllib.load(f)
        for k, v in user.items():
            if isinstance(v, dict):
                cfg.setdefault(k, {}).update(v)
            else:
                cfg[k] = v
    else:
        log.warning("%s not found, using defaults", path)
    return cfg


def next_screen(order, idx, cfg, state, now):
    """Index of the next screen that has something to show (duration > 0)."""
    for step in range(1, len(order) + 1):
        j = (idx + step) % len(order)
        if order[j].duration(cfg, state, now) > 0:
            return j
    return 0


def render_frame(cv, screen, now, t, state, cfg):
    weather, events, _, _ = state.snapshot()
    cv.clear()
    screen.draw(cv, now, t, weather, events, cfg)
    if state.stale():
        cv.set(W - 1, H - 1, RED)
    return cv.img


def run_preview(cfg, state, tz, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    cv = Canvas()
    order = [SCREENS[n] for n in cfg["screens"]["order"] if n in SCREENS]
    frames, fps = [], 10
    now0 = datetime.now(tz)
    for sc in order:
        dur = sc.duration(cfg, state, now0)
        if dur <= 0:
            continue
        for i in range(int(dur * fps)):
            t = i / fps
            img = render_frame(cv, sc, now0 + timedelta(seconds=t), t, state, cfg)
            if i == int(min(2.0, dur / 2) * fps):
                led_render(img).save(out_dir / f"preview_{sc.name}.png")
            frames.append(led_render(img, 6))
    frames[0].save(out_dir / "preview.gif", save_all=True, append_images=frames[1:],
                   duration=int(1000 / fps), loop=0, optimize=True)
    log.info("wrote %d screens and preview.gif to %s", len(order), out_dir)


def find_team(query: str, league: str):
    data = json.loads(http_get(f"{ESPN}{league.strip('/')}/teams?limit=500"))
    teams = [t["team"] for s in data.get("sports", []) for lg in s.get("leagues", []) for t in lg.get("teams", [])]
    q = query.lower()
    hits = [t for t in teams if q in json.dumps([t.get("displayName"), t.get("abbreviation"),
                                                 t.get("location"), t.get("name")]).lower()]
    if not hits:
        print(f"No team matching {query!r} in {league}. All teams:")
        hits = teams
    for t in hits:
        print(f'  team = "{t.get("id")}"   # {t.get("abbreviation")}  {t.get("displayName")}')
    print(f'\nUse with:  league = "{league}"')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.toml")))
    ap.add_argument("--demo", action="store_true", help="use made-up weather and events")
    ap.add_argument("--preview", action="store_true", help="render PNGs + GIF instead of driving the matrix")
    ap.add_argument("--out", default="preview", help="folder for --preview output")
    ap.add_argument("--find-team", metavar="NAME", help="look up ESPN team ids, e.g. --find-team panthers")
    ap.add_argument("--league", default="hockey/nhl", help="league for --find-team (e.g. soccer/eng.1)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if a.find_team:
        find_team(a.find_team, a.league)
        return

    cfg = load_config(Path(a.config))
    tz = ZoneInfo(cfg["location"].get("timezone", "Europe/Helsinki"))
    state = State()
    SCREENS["games"].state = SCREENS["live"].state = state
    ic = cfg.get("icloud", {})
    cfg["_has_cal"] = a.demo or bool((ic.get("apple_id") and ic.get("app_password")) or ic.get("ics_urls"))

    if a.demo:
        state.weather, state.events = demo_data(tz)
        state.games = demo_games(tz)
    elif a.preview:  # fetch once, synchronously
        try:
            state.weather = fetch_weather(cfg)
        except Exception as e:
            log.warning("weather: %s", e)
        if cfg["_has_cal"]:
            try:
                state.events = fetch_events(cfg, tz)
                for e in state.events:
                    log.info("event: %s %s %s", e.start, "all-day" if e.all_day else "", e.title)
            except Exception as e:
                log.warning("calendar: %s", e)
        if cfg.get("sports", {}).get("teams"):
            try:
                state.games = fetch_games(cfg)
                for g in state.games:
                    log.debug("game: %s %s @ %s %s", g.start, g.away.abbr, g.home.abbr, g.state)
            except Exception as e:
                log.warning("sports: %s", e)

    if a.preview:
        run_preview(cfg, state, tz, Path(a.out))
        return

    out = MatrixOut(cfg)
    if not a.demo:
        start_fetchers(cfg, state, tz)

    cv = Canvas()
    order = [SCREENS[n] for n in cfg["screens"]["order"] if n in SCREENS]
    frame_dt = 1.0 / int(cfg["display"].get("fps", 30))
    idx, screen_start = 0, time.monotonic()
    log.info("running - Ctrl+C to stop")
    try:
        while True:
            tick = time.monotonic()
            now = datetime.now(tz)
            sc = order[idx]
            t = tick - screen_start
            if t >= sc.duration(cfg, state, now):
                idx = next_screen(order, idx, cfg, state, now)
                sc, screen_start, t = order[idx], tick, 0.0
            out.show(render_frame(cv, sc, now, t, state, cfg), brightness_now(cfg, now))
            time.sleep(max(0.0, frame_dt - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        out.matrix.Clear()


if __name__ == "__main__":
    main()
