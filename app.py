#!/usr/bin/env python3
"""
Pi Sprinkler - Flask web application
Replaces all CGI scripts with a single modern web app.
Run with: sudo python3 app.py
"""

import configparser
import datetime
import hmac
import ipaddress
import json
import os
import re
import subprocess
import threading
import time
import urllib.request

from flask import Flask, request, Response

try:
    import pigpio
    _pi = pigpio.pi()
    GPIO_AVAILABLE = _pi.connected
except Exception:
    _pi = None
    GPIO_AVAILABLE = False

from apscheduler.schedulers.background import BackgroundScheduler

# ── App setup ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = b'sprinkler-secret-key-change-me'

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "sprinkler.config")
STATE_FILE  = os.path.join(BASE_DIR, "sprinkler_state.json")
LOG_FILE    = os.path.join(BASE_DIR, "sprinkler.log")


def _load_gpio_config():
    """Read station pins and master pin from [GPIO] section of config."""
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    stations = []
    master   = None
    if cfg.has_section('GPIO'):
        try:
            n = int(cfg.get('GPIO', 'NumberOfStations'))
        except (configparser.Error, ValueError):
            n = 8
        for i in range(1, n + 1):
            try:
                stations.append(int(cfg.get('GPIO', str(i))))
            except (configparser.Error, ValueError):
                pass
        try:
            master = int(cfg.get('GPIO', 'Master'))
        except (configparser.Error, ValueError):
            master = None
    if not stations:                       # fallback if config missing
        stations = [5, 6, 12, 13, 16]
    return stations, master


# GPIO pin numbers for each station (station 1 = index 0); reloaded at startup
STATIONS, MASTER_PIN = _load_gpio_config()

def reload_stations():
    """Re-read GPIO config from file and update module globals + re-init GPIO."""
    global STATIONS, MASTER_PIN
    STATIONS, MASTER_PIN = _load_gpio_config()
    gpio_setup()

# ── Global state ───────────────────────────────────────────────────────────────
_lock  = threading.Lock()
_state = {
    "running":         False,   # never persisted — always False on startup
    "enabled":         True,
    "delay":           False,
    "resume_at":       0.0,     # epoch time when delay expires
    "manual_on_since": {},      # {str(idx): epoch float} tracked for manual-on display
}

def _load_state():
    """Load persisted enabled/delay/resume_at from JSON file."""
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
        with _lock:
            _state["enabled"]   = bool(saved.get("enabled", True))
            _state["delay"]     = bool(saved.get("delay", False))
            _state["resume_at"] = float(saved.get("resume_at", 0.0))
        # If delay has already expired, clear it
        with _lock:
            if _state["delay"] and time.time() >= _state["resume_at"]:
                _state["delay"]     = False
                _state["enabled"]   = True
                _state["resume_at"] = 0.0
    except (FileNotFoundError, ValueError, KeyError):
        pass   # first run or corrupt file — use defaults

def _save_state():
    """Persist enabled/delay/resume_at to JSON file."""
    with _lock:
        snapshot = {
            "enabled":   _state["enabled"],
            "delay":     _state["delay"],
            "resume_at": _state["resume_at"],
        }
    with open(STATE_FILE, "w") as f:
        json.dump(snapshot, f)

# ── GPIO ───────────────────────────────────────────────────────────────────────
def gpio_setup():
    if not GPIO_AVAILABLE:
        return
    for pin in STATIONS:
        _pi.set_mode(pin, pigpio.OUTPUT)
        _pi.write(pin, 1)       # stations: HIGH = relay off (active-low)
    if MASTER_PIN is not None:
        _pi.set_mode(MASTER_PIN, pigpio.OUTPUT)
        _pi.write(MASTER_PIN, 1)  # master NO wiring: HIGH = de-energized = NO open = valve CLOSED (safe default)

def gpio_write(pin, on: bool):
    if GPIO_AVAILABLE:
        _pi.write(pin, 0 if on else 1)
    

def gpio_read(pin) -> bool:
    return GPIO_AVAILABLE and (_pi.read(pin) == 0)

def any_station_on() -> bool:
    return any(gpio_read(p) for p in STATIONS)

def master_on():
    """Open master valve. Relay wired NO: energize (LOW) = NO closed = valve open."""
    if MASTER_PIN is not None:
        gpio_write(MASTER_PIN, True)    # write 0 = energize = NO closed = valve open

def master_off():
    """Close master valve. Relay wired NO: de-energize (HIGH) = NO open = valve closed."""
    if MASTER_PIN is not None:
        gpio_write(MASTER_PIN, False)   # write 1 = de-energize = NO open = valve closed

def all_off():
    for pin in STATIONS:
        gpio_write(pin, False)
    master_off()

# ── Config ─────────────────────────────────────────────────────────────────────
def read_cfg() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    return cfg

def write_cfg(cfg: configparser.ConfigParser):
    with open(CONFIG_FILE, 'w') as f:
        cfg.write(f)

def ensure_default_cfg():
    cfg = read_cfg()
    changed = False
    if not cfg.has_section('forecastio'):
        cfg.add_section('forecastio')
        cfg.set('forecastio', 'lat', '37.774929')
        cfg.set('forecastio', 'lng', '-122.419416')
        changed = True
    if not cfg.has_section('WateringMinutes'):
        cfg.add_section('WateringMinutes')
        for i in range(1, len(STATIONS) + 1):
            cfg.set('WateringMinutes', str(i), '20')
        changed = True
    if not cfg.has_section('auth'):
        cfg.add_section('auth')
        cfg.set('auth', 'username', 'admin')
        cfg.set('auth', 'password', 'sprinkler')
        changed = True
    if not cfg.has_section('RuntimeParams'):
        cfg.add_section('RuntimeParams')
    _rtp_defaults = {
        'schedule_mode':       'fixed',
        'schedule_time':       '03:00',
        'schedule_offset':     '-30',
        'forecast_rain_skip':  'false',
        'temp_adjustment':     'false',
        'temp_cool_threshold': '20',
        'temp_hot_threshold':  '30',
        'temp_cool_factor':    '0.75',
        'temp_hot_factor':     '1.30',
    }
    for _k, _v in _rtp_defaults.items():
        if not cfg.has_option('RuntimeParams', _k):
            cfg.set('RuntimeParams', _k, _v)
            changed = True
    if not cfg.has_section('NoRainSkip'):
        cfg.add_section('NoRainSkip')
        for i in range(1, len(STATIONS) + 1):
            cfg.set('NoRainSkip', str(i), 'false')
        changed = True
    if not cfg.has_section('WateringInterval'):
        cfg.add_section('WateringInterval')
        for i in range(1, len(STATIONS) + 1):
            cfg.set('WateringInterval', str(i), '0')
        changed = True
    if changed:
        write_cfg(cfg)

# ── Weather ────────────────────────────────────────────────────────────────────
WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain",  63: "Moderate rain",    65: "Heavy rain",
    71: "Slight snow",  73: "Moderate snow",    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}

def precip_label(mm) -> str:
    if mm is None: return "Unknown"
    if mm == 0:    return "None"
    if mm < 2.5:   return "Very Light"
    if mm < 10:    return "Light"
    if mm < 25:    return "Moderate"
    return "Heavy"

def fetch_weather() -> dict:
    cfg = read_cfg()
    try:
        lat = cfg.get('forecastio', 'lat')
        lng = cfg.get('forecastio', 'lng')
    except configparser.Error:
        return {"error": "Latitude/longitude not configured in Settings."}

    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lng}"
        f"&daily=temperature_2m_max,precipitation_sum,sunrise"
        f"&current_weather=true"
        f"&temperature_unit=celsius&precipitation_unit=mm"
        f"&past_days=1&forecast_days=2&timezone=auto"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        cw     = data["current_weather"]
        daily  = data["daily"]
        precip = daily["precipitation_sum"]
        # Indices: 0 = yesterday, 1 = today, 2 = tomorrow
        return {
            "temperature":         cw["temperature"],
            "conditions":          WMO_CODES.get(cw.get("weathercode", -1), "Unknown"),
            "today_high":          daily["temperature_2m_max"][1],
            "yesterday_high":      daily["temperature_2m_max"][0],
            "today_precip":           precip_label(precip[1]),
            "yesterday_precip":       precip_label(precip[0]),
            "yesterday_precip_mm":    precip[0],
            "today_precip_mm":        precip[1],
            "tomorrow_precip_mm":     precip[2] if len(precip) > 2 else None,
            "sunrise_today":          daily["sunrise"][1] if len(daily["sunrise"]) > 1 else None,
            "sunrise_tomorrow":       daily["sunrise"][2] if len(daily["sunrise"]) > 2 else None,
            "error": None,
        }
    except Exception as exc:
        return {"error": str(exc)}

# ── Logging ────────────────────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] {msg}\n")
    # Trim to last 1000 lines when file exceeds 512 KB
    try:
        if os.path.getsize(LOG_FILE) > 512 * 1024:
            with open(LOG_FILE) as f:
                lines = f.readlines()
            with open(LOG_FILE, "w") as f:
                f.writelines(lines[-1000:])
    except OSError:
        pass

# ── Status ─────────────────────────────────────────────────────────────────────
def get_status() -> tuple[str, str]:
    """Returns (css_class, human_text)."""
    with _lock:
        s = dict(_state)
    if not s["enabled"]:
        if s["delay"] and s["resume_at"] > time.time():
            exp = datetime.datetime.fromtimestamp(s["resume_at"]).strftime("%d/%m %H:%M")
            return "delayed", f"Delayed — resumes {exp}"
        return "paused", "Paused (indefinite hold)"
    if s["running"]:
        return "running", "Running"
    return "idle", "Idle"

def station_names() -> dict[int, str]:
    """Return {0-based-index: name} from [stations] config section."""
    cfg = read_cfg()
    names = {}
    if cfg.has_section('stations'):
        for k, v in cfg.items('stations'):
            try:
                names[int(k) - 1] = v.strip()
            except ValueError:
                pass
    return names


# ── Scheduler jobs ─────────────────────────────────────────────────────────────
def job1():
    """Main watering job driven by [WateringMinutes] in config."""
    with _lock:
        if not _state["enabled"]:
            return
        _state["running"] = True

    ran = False
    temp_factor = 1.0
    try:
        cfg = read_cfg()

        # ── Per-station flags ─────────────────────────────────────────────────
        no_rain_skip: dict[int, bool] = {}   # {0-based idx: True = ignore rain skip}
        if cfg.has_section('NoRainSkip'):
            for _k, _v in cfg.items('NoRainSkip'):
                try:
                    no_rain_skip[int(_k) - 1] = _v.strip().lower() == 'true'
                except ValueError:
                    pass

        min_interval: dict[int, int] = {}    # {0-based idx: min days between runs}
        if cfg.has_section('WateringInterval'):
            for _k, _v in cfg.items('WateringInterval'):
                try:
                    min_interval[int(_k) - 1] = max(0, int(_v))
                except ValueError:
                    pass

        # ── Weather-based checks ──────────────────────────────────────────────
        global_rain_skip     = False
        global_rain_skip_msg = ""
        w = fetch_weather()
        if not w.get("error"):
            try:
                threshold = float(cfg.get('RuntimeParams', 'precipitation'))
            except (configparser.Error, ValueError):
                threshold = 0.0

            yesterday_mm = float(w.get("yesterday_precip_mm") or 0)
            today_mm     = float(w.get("today_precip_mm")    or 0)
            tomorrow_mm  = float(w.get("tomorrow_precip_mm") or 0)

            # 1. Backward rain skip
            if threshold > 0 and yesterday_mm >= threshold:
                excess = yesterday_mm - threshold
                global_rain_skip     = True
                global_rain_skip_msg = (f"yesterday's rain {yesterday_mm:.1f}mm "
                                        f"\u2265 threshold {threshold:.1f}mm (+{excess:.1f}mm over)")

            # 2. Forward rain skip
            if not global_rain_skip:
                forecast_skip = (cfg.get('RuntimeParams', 'forecast_rain_skip', fallback='false')
                                 .strip().lower() == 'true')
                if forecast_skip and threshold > 0:
                    if today_mm >= threshold:
                        excess = today_mm - threshold
                        global_rain_skip     = True
                        global_rain_skip_msg = (f"today's forecast {today_mm:.1f}mm "
                                                f"\u2265 threshold {threshold:.1f}mm (+{excess:.1f}mm over)")
                    elif tomorrow_mm >= threshold:
                        excess = tomorrow_mm - threshold
                        global_rain_skip     = True
                        global_rain_skip_msg = (f"tomorrow's forecast {tomorrow_mm:.1f}mm "
                                                f"\u2265 threshold {threshold:.1f}mm (+{excess:.1f}mm over)")

            # Rain check context (when not skipping)
            if not global_rain_skip and threshold > 0:
                max_obs = max(yesterday_mm, today_mm, tomorrow_mm)
                spare   = threshold - max_obs
                log(f"<Rain check: yest {yesterday_mm:.1f}mm, today {today_mm:.1f}mm, "
                    f"tmrw {tomorrow_mm:.1f}mm \u2014 threshold {threshold:.1f}mm, {spare:.1f}mm to spare>")

            # 3. Temperature-based duration adjustment
            temp_adj = (cfg.get('RuntimeParams', 'temp_adjustment', fallback='false')
                        .strip().lower() == 'true')
            if temp_adj:
                today_high = w.get("today_high")
                if today_high is not None:
                    try:
                        cool_t = float(cfg.get('RuntimeParams', 'temp_cool_threshold', fallback='20'))
                        hot_t  = float(cfg.get('RuntimeParams', 'temp_hot_threshold',  fallback='30'))
                        cool_f = float(cfg.get('RuntimeParams', 'temp_cool_factor',    fallback='0.75'))
                        hot_f  = float(cfg.get('RuntimeParams', 'temp_hot_factor',     fallback='1.30'))
                    except (configparser.Error, ValueError):
                        cool_t, hot_t, cool_f, hot_f = 20.0, 30.0, 0.75, 1.30
                    if today_high < cool_t:
                        temp_factor = cool_f
                    elif today_high > hot_t:
                        temp_factor = hot_f
                    if temp_factor != 1.0:
                        log(f"<Temperature adjustment: {today_high}\u00b0C \u2192 {temp_factor:.0%} of configured durations>")

        # ── Pre-determine which stations will actually run ─────────────────────
        minutes: dict[int, float] = {}
        if cfg.has_section("WateringMinutes"):
            for _k, _v in cfg.items("WateringMinutes"):
                try:
                    minutes[int(_k) - 1] = float(_v)
                except ValueError:
                    pass
        names      = station_names()
        last_runs  = _parse_last_station_runs()
        now_dt     = datetime.datetime.now()

        will_run = []
        for i in range(len(STATIONS)):
            dur = minutes.get(i, 0) * temp_factor
            if dur <= 0:
                continue
            if global_rain_skip and not no_rain_skip.get(i, False):
                continue
            interval = min_interval.get(i, 0)
            if interval > 0:
                last_ts = last_runs.get(names.get(i, f"Station {i + 1}"))
                if last_ts:
                    try:
                        days_ago = (now_dt - datetime.datetime.strptime(
                            last_ts, "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400
                        if days_ago < interval:
                            continue
                    except ValueError:
                        pass
            will_run.append(i)

        if global_rain_skip:
            if not will_run:
                log(f"<Skipped \u2014 {global_rain_skip_msg}>")
                return
            log(f"<Rain skip active ({global_rain_skip_msg}) \u2014 running no-rain-skip stations only>")

        if not will_run:
            log("<No stations to run \u2014 all skipped by interval or zero duration>")
            return

        ran = True
        log("<Master ON>")
        master_on()

        for i, pin in enumerate(STATIONS):
            with _lock:
                if not _state["enabled"] or not _state["running"]:
                    break
            dur = minutes.get(i, 0) * temp_factor
            if dur <= 0:
                continue
            name        = names.get(i, f"Station {i + 1}")
            stn_no_rain = no_rain_skip.get(i, False)

            # Rain skip
            if global_rain_skip and not stn_no_rain:
                log(f"<{name} skipped \u2014 rain skip active>")
                continue

            # Minimum interval check
            interval = min_interval.get(i, 0)
            if interval > 0:
                last_ts = last_runs.get(name)
                if last_ts:
                    try:
                        days_ago = (now_dt - datetime.datetime.strptime(
                            last_ts, "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400
                        if days_ago < interval:
                            days_left = interval - days_ago
                            log(f"<{name} skipped \u2014 min interval {interval}d, "
                                f"last watered {days_ago:.1f}d ago, {days_left:.1f}d remaining>")
                            continue
                    except ValueError:
                        pass

            gpio_write(pin, True)
            log(f"<{name} ON \u2014 {dur:.1f} min scheduled>")
            t_start = time.time()
            time.sleep(dur * 60)
            actual_min = (time.time() - t_start) / 60
            gpio_write(pin, False)
            log(f"<{name} OFF \u2014 ran {actual_min:.1f} min>")
            time.sleep(1)
    finally:
        all_off()
        with _lock:
            _state["running"] = False
        if ran:
            log("<Master OFF>")


def test_run(duration_secs: int):
    """Cycle all stations for `duration_secs` each as a test."""
    with _lock:
        if _state["running"]:
            return   # already running, ignore
        _state["running"] = True
    master_on()
    try:
        names = station_names()
        for i, pin in enumerate(STATIONS):
            with _lock:
                if not _state["running"]:
                    break
            name = names.get(i, f"Station {i + 1}")
            gpio_write(pin, True)
            log(f"<Test {name} ON>")
            time.sleep(duration_secs)
            gpio_write(pin, False)
            log(f"<Test {name} OFF>")
            time.sleep(1)
    finally:
        all_off()
        with _lock:
            _state["running"] = False


def _delay_watcher():
    """Background thread that auto-resumes the scheduler after a timed delay."""
    while True:
        with _lock:
            expired = _state["delay"] and time.time() >= _state["resume_at"]
        if expired:
            scheduler.resume()
            with _lock:
                _state["enabled"] = True
                _state["delay"]   = False
            _save_state()
            _schedule_next_run()   # re-add job in case the date trigger has expired
        time.sleep(30)


# ── Sunrise fetch & dynamic scheduling ────────────────────────────────────────

def _fetch_sunrise(date: datetime.date) -> "datetime.datetime | None":
    """Fetch sunrise time for a given date from Open-Meteo. Returns a local datetime or None."""
    cfg = read_cfg()
    try:
        lat = cfg.get('forecastio', 'lat')
        lng = cfg.get('forecastio', 'lng')
    except configparser.Error:
        return None
    today      = datetime.date.today()
    days_ahead = (date - today).days
    if days_ahead < 0:
        return None
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lng}"
        f"&daily=sunrise"
        f"&timezone=auto"
        f"&forecast_days={days_ahead + 1}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return datetime.datetime.fromisoformat(data["daily"]["sunrise"][days_ahead])
    except Exception:
        return None


def _schedule_next_run() -> "datetime.datetime | None":
    """Compute the next watering run time and (re-)schedule the watering job.
    Uses [RuntimeParams] schedule_mode / schedule_time / schedule_offset.
    Returns the scheduled datetime, or None on failure."""
    cfg        = read_cfg()
    mode       = 'fixed'
    sched_time = '03:00'
    offset_min = -30

    if cfg.has_section('RuntimeParams'):
        mode       = cfg.get('RuntimeParams', 'schedule_mode',   fallback='fixed').strip()
        sched_time = cfg.get('RuntimeParams', 'schedule_time',   fallback='03:00').strip()
        try:
            offset_min = int(cfg.get('RuntimeParams', 'schedule_offset', fallback='-30'))
        except (configparser.Error, ValueError):
            offset_min = -30

    now      = datetime.datetime.now()
    run_time = None

    if mode == 'sunrise':
        tomorrow   = now.date() + datetime.timedelta(days=1)
        sunrise_dt = _fetch_sunrise(tomorrow)
        if sunrise_dt is not None:
            run_time = sunrise_dt + datetime.timedelta(minutes=offset_min)

    if run_time is None or run_time <= now:
        # Fixed mode or fallback when sunrise fetch fails / results in a past time
        try:
            h, m = map(int, sched_time.split(':'))
        except (ValueError, AttributeError):
            h, m = 3, 0
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += datetime.timedelta(days=1)
        run_time = candidate

    # Remove the previous one-shot job (if any) then add the new one
    try:
        scheduler.remove_job('watering_job')
    except Exception:
        pass
    scheduler.add_job(_job1_and_reschedule, 'date', run_date=run_time, id='watering_job')
    return run_time


def _job1_and_reschedule():
    """Run the main watering job, then schedule the next day's run."""
    job1()
    _schedule_next_run()


def get_next_run_time() -> "str | None":
    """Return a human-readable string of the next scheduled run time, or None."""
    try:
        job = scheduler.get_job('watering_job')
        if job and job.next_run_time:
            return job.next_run_time.strftime("%d/%m/%Y %H:%M")
    except Exception:
        pass
    return None


scheduler = BackgroundScheduler()

# ── HTML page wrapper ──────────────────────────────────────────────────────────
_CSS = """
* { box-sizing: border-box; }
body { font-family: Arial, sans-serif; max-width: 900px; margin: 0 auto; padding: 1rem; color: #333; }
nav { background: #2a6496; padding: .6rem 1.2rem; border-radius: 6px; margin-bottom: 1.4rem; }
nav a { color: #fff; text-decoration: none; margin-right: 1.4rem; font-weight: bold; }
nav a:hover { text-decoration: underline; }
h2 { border-bottom: 2px solid #2a6496; padding-bottom: .3rem; color: #2a6496; }
.status-bar { padding: .5rem 1rem; border-radius: 4px; margin-bottom: 1.2rem; font-weight: bold; }
.idle    { background: #dff0d8; color: #3c763d; }
.running { background: #fcf8e3; color: #8a6d3b; }
.paused, .delayed { background: #f2dede; color: #a94442; }
table { border-collapse: collapse; width: 100%; margin-bottom: 1rem; }
th, td { border: 1px solid #ddd; padding: .5rem .8rem; text-align: left; }
th { background: #f5f5f5; }
tr.on td { background: #dff0d8; }
.btn { padding: .4rem .9rem; cursor: pointer; border: 1px solid #aaa; border-radius: 3px;
       background: #f5f5f5; font-size: .95rem; }
.btn:hover { background: #e8e8e8; }
.btn-green { background: #5cb85c; color: #fff; border-color: #4cae4c; }
.btn-red   { background: #d9534f; color: #fff; border-color: #d43f3a; }
.btn-green:hover { background: #449d44; }
.btn-red:hover   { background: #c9302c; }
.alert     { padding: .6rem 1rem; border-radius: 4px; margin-bottom: 1rem;
             background: #dff0d8; border: 1px solid #d6e9c6; }
.alert.err { background: #f2dede; border-color: #ebccd1; }
.row-form  { display: inline; }
input[type=number], input[type=text] { padding: .3rem .5rem; border: 1px solid #ccc; border-radius: 3px; }
"""

_NAV = """
<nav>
  <a href="/">Home</a>
  <a href="/manual">Manual</a>
  <a href="/delay">Delay</a>
  <a href="/program">Program</a>
  <a href="/settings">Settings</a>
  <a href="/log">Log</a>
</nav>
"""

def page(title: str, body: str) -> str:
    cls, txt = get_status()
    return f"""<!DOCTYPE html>
<html lang="en"><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Pi Sprinkler \u2013 {title}</title>
  <style>{_CSS}</style>
</head>
<body>
{_NAV}
<div class="status-bar {cls}">&#x25CF; {txt}</div>
{body}
</body></html>"""


# ── Authentication ─────────────────────────────────────────────────────────────
_LAN_NETS = [
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
]

@app.before_request
def require_login():
    # Skip auth for direct LAN connections
    try:
        remote = ipaddress.ip_address(request.remote_addr)
        if any(remote in net for net in _LAN_NETS):
            return
    except ValueError:
        pass

    auth = request.authorization
    cfg  = read_cfg()
    try:
        exp_u = cfg.get('auth', 'username')
        exp_p = cfg.get('auth', 'password')
    except configparser.Error:
        return  # no [auth] section configured — allow through
    if (not auth
            or not hmac.compare_digest(auth.username, exp_u)
            or not hmac.compare_digest(auth.password, exp_p)):
        return Response(
            'Please log in.',
            401,
            {'WWW-Authenticate': 'Basic realm="Pi Sprinkler"'},
        )


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    w   = fetch_weather()

    if w.get("error"):
        weather_html = f'<div class="alert err">Weather unavailable: {w["error"]}</div>'
    else:
        weather_html = f"""
        <table>
          <tr><th>Current Temperature</th><td>{w['temperature']}&deg;C</td></tr>
          <tr><th>Conditions</th>         <td>{w['conditions']}</td></tr>
          <tr><th>Today&apos;s High</th>  <td>{w['today_high']}&deg;C</td></tr>
          <tr><th>Yesterday&apos;s High</th><td>{w['yesterday_high']}&deg;C</td></tr>
          <tr><th>Today&apos;s Precipitation</th>    <td>{w['today_precip']}</td></tr>
          <tr><th>Yesterday&apos;s Precipitation</th><td>{w['yesterday_precip']}</td></tr>
        </table>"""

    nxt     = get_next_run_time()
    nxt_str = f'<p>Next scheduled run: <strong>{nxt}</strong></p>' if nxt else ''
    body = f"""
    <h2>Home</h2>
    <p>Current time: <strong>{now}</strong></p>
    {nxt_str}
    <h3>Weather Report</h3>
    {weather_html}"""
    return page("Home", body)


@app.route("/manual", methods=["GET", "POST"])
def manual():
    alert = ""
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "toggle":
            idx = int(request.form.get("station", 0))
            pin = STATIONS[idx]
            turning_on = not gpio_read(pin)
            names = station_names()
            name  = names.get(idx, f"Station {idx + 1}")
            if turning_on:
                master_on()
                with _lock:
                    _state["manual_on_since"][str(idx)] = time.time()
            else:
                with _lock:
                    _state["manual_on_since"].pop(str(idx), None)
            gpio_write(pin, turning_on)
            if not turning_on and not any_station_on():
                master_off()
            log(f"<Manual: {name} {'ON' if turning_on else 'OFF'}>")
        elif action == "test_start":
            dur = request.form.get("duration", "")
            if not dur.isdigit() or int(dur) < 1:
                alert = '<div class="alert err">Duration must be a positive number of seconds.</div>'
            else:
                t = threading.Thread(target=test_run, args=(int(dur),), daemon=True)
                t.start()
        elif action == "test_cancel":
            with _lock:
                _state["running"] = False
                _state["manual_on_since"].clear()
            all_off()
        elif action == "home_run":
            with _lock:
                already = _state["running"]
            if not already:
                with _lock:
                    _state["running"] = True   # set early so UI shows Cancel
                log("<Manual: Home Run triggered>")
                threading.Thread(target=job1, daemon=True).start()
        elif action == "home_run_cancel":
            with _lock:
                _state["running"] = False
                _state["manual_on_since"].clear()
            all_off()
            log("<Manual: Home Run cancelled>")

    cfg        = read_cfg()
    stn_names  = station_names()

    rows = ""
    with _lock:
        on_since_snap = dict(_state["manual_on_since"])
    for i, pin in enumerate(STATIONS):
        on    = gpio_read(pin)
        name  = stn_names.get(i, f"Station {i + 1}")
        since = on_since_snap.get(str(i))
        if on and since:
            since_str = datetime.datetime.fromtimestamp(since).strftime("%H:%M:%S")
            elapsed_m = int((time.time() - since) / 60)
            warn      = elapsed_m >= 10
            state_td  = (f'<td style="color:{'#a94442' if warn else '#3c763d'};font-weight:bold">'
                         f'&#x25CF; ON since {since_str}'
                         f'{" ("+str(elapsed_m)+" min)" if elapsed_m > 0 else ""}'
                         f'</td>')
        elif on:
            state_td = '<td style="color:#3c763d">&#x25CF; ON</td>'
        else:
            state_td = '<td>&#x25CB; OFF</td>'
        rows += f"""
        <tr class="{'on' if on else ''}">
          <td>{i + 1}</td>
          <td>{name}</td>
          {state_td}
          <td>
            <form class="row-form" method="post">
              <input type="hidden" name="action"  value="toggle">
              <input type="hidden" name="station" value="{i}">
              <button class="btn {'btn-red' if on else 'btn-green'}">
                {'Turn OFF' if on else 'Turn ON'}
              </button>
            </form>
          </td>
        </tr>"""

    with _lock:
        running = _state["running"]

    if running:
        run_controls = """
        <form method="post" style="margin-bottom:.5rem">
          <input type="hidden" name="action" value="test_cancel">
          <button class="btn btn-red">&#x25A0; Cancel Current Run</button>
        </form>"""
    else:
        run_controls = f"""
        <h4 style="margin:.8rem 0 .4rem">Test Run</h4>
        <form method="post" style="display:flex;gap:.6rem;align-items:center;margin-bottom:.8rem">
          <input type="hidden" name="action" value="test_start">
          <label>Duration per station (sec):</label>
          <input type="number" name="duration" value="10" min="1" style="width:80px">
          <button class="btn btn-green">&#x25B6; Start Test Run</button>
        </form>
        <h4 style="margin:.8rem 0 .4rem">Home Run</h4>
        <form method="post">
          <input type="hidden" name="action" value="home_run">
          <button class="btn btn-green"
                  onclick="return confirm('Run the full watering sequence now?')">
            &#x25B6; Home Run
          </button>
        </form>
        <p style="color:#777;margin:.35rem 0;font-size:.9rem">
          Runs the full scheduled sequence once. Rain&nbsp;skip and minimum
          intervals still apply.
        </p>"""

    body = f"""
    {alert}
    <h2>Manual Station Control</h2>
    <table>
      <thead><tr><th>#</th><th>Name</th><th>State</th><th>Action</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <h3>Run Controls</h3>
    {run_controls}
    <h3>System Control</h3>
    <a href="/reboot"><button class="btn">Reboot / Shutdown &rsaquo;</button></a>"""
    return page("Manual Control", body)


@app.route("/delay", methods=["GET", "POST"])
def delay():
    alert = ""
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "start":
            raw = request.form.get("duration", "")
            try:
                hours = float(raw)
                if hours <= 0:
                    raise ValueError
                resume_at = time.time() + hours * 3600
                with _lock:
                    _state["enabled"]   = False
                    _state["delay"]     = True
                    _state["resume_at"] = resume_at
                scheduler.pause()
                _save_state()
                exp = datetime.datetime.fromtimestamp(resume_at).strftime("%d/%m/%Y %H:%M")
                alert = f'<div class="alert">Rain delay set &mdash; scheduler will resume at {exp}.</div>'
            except ValueError:
                alert = '<div class="alert err">Please enter a valid number of hours.</div>'
        elif action == "pause":
            with _lock:
                _state["enabled"] = False
                _state["delay"]   = False
            scheduler.pause()
            _save_state()
            alert = '<div class="alert">Scheduler paused indefinitely.</div>'
        elif action == "resume":
            with _lock:
                _state["enabled"] = True
                _state["delay"]   = False
            scheduler.resume()
            _save_state()
            _schedule_next_run()
            alert = '<div class="alert">Scheduler resumed.</div>'

    body = f"""
    {alert}
    <h2>Rain Delay</h2>
    <form method="post" style="display:flex;gap:.6rem;align-items:center;margin-bottom:1.5rem">
      <input type="hidden" name="action" value="start">
      <label>Delay duration (hours):</label>
      <input type="number" name="duration" value="72" min="1" style="width:90px">
      <button class="btn">Set Delay</button>
    </form>
    <h3>Indefinite Hold</h3>
    <form class="row-form" method="post">
      <input type="hidden" name="action" value="pause">
      <button class="btn btn-red">&#x23F8; Pause Scheduler</button>
    </form>
    &nbsp;
    <form class="row-form" method="post">
      <input type="hidden" name="action" value="resume">
      <button class="btn btn-green">&#x25B6; Resume Scheduler</button>
    </form>"""
    return page("Rain Delay", body)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    cfg   = read_cfg()
    alert = ""

    if request.method == "POST":
        action = request.form.get("action", "update")
        if action == "update":
            for key, val in request.form.items():
                if key == "action":
                    continue
                if ":" in key:
                    section, option = key.split(":", 1)
                    if cfg.has_section(section) and cfg.has_option(section, option):
                        cfg.set(section, option, val.strip())
            write_cfg(cfg)
            reload_stations()
            _schedule_next_run()
            alert = '<div class="alert">Settings saved successfully.</div>'
        elif action == "reset":
            if os.path.exists(CONFIG_FILE):
                backup = CONFIG_FILE + "." + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                os.rename(CONFIG_FILE, backup)
            ensure_default_cfg()
            cfg   = read_cfg()
            alert = '<div class="alert">Settings reset to defaults.</div>'

    # Sections managed by the Program page — hide them here to avoid duplication
    _HIDE_IN_SETTINGS = {'wateringminutes', 'norainskip', 'wateringinterval', 'runtimeparams'}
    rows = ""
    for section in cfg.sections():
        if section.lower() in _HIDE_IN_SETTINGS:
            continue
        rows += f'<tr><td colspan="2" style="background:#e8e8e8;font-weight:bold;padding:.5rem .8rem">[{section}]</td></tr>'
        for name, value in cfg.items(section):
            rows += f"""
            <tr>
              <td style="width:40%">{name}</td>
              <td><input type="text" name="{section}:{name}" value="{value}" style="width:100%"></td>
            </tr>"""

    body = f"""
    {alert}
    <h2>Settings</h2>
    <form method="post">
      <input type="hidden" name="action" value="update">
      <table>{rows}</table>
      <p style="margin-top:1rem">
        <button class="btn btn-green" type="submit">Save Settings</button>
        &nbsp;
        <button class="btn btn-red" name="action" value="reset"
                onclick="return confirm('Reset ALL settings to defaults?')">
          Reset to Defaults
        </button>
      </p>
    </form>"""
    return page("Settings", body)


@app.route("/program", methods=["GET", "POST"])
def program():
    cfg   = read_cfg()
    alert = ""
    if request.method == "POST":
        action = request.form.get("action", "durations")
        if action == "durations":
            if cfg.has_section("WateringMinutes"):
                if not cfg.has_section('NoRainSkip'):
                    cfg.add_section('NoRainSkip')
                if not cfg.has_section('WateringInterval'):
                    cfg.add_section('WateringInterval')
                for k in cfg.options("WateringMinutes"):
                    val = request.form.get(f"min_{k}", "").strip()
                    try:
                        cfg.set("WateringMinutes", k, str(max(0.0, float(val))))
                    except ValueError:
                        pass
                    cfg.set('NoRainSkip', k,
                            'true' if request.form.get(f"norain_{k}") else 'false')
                    try:
                        iv = max(0, int(request.form.get(f"interval_{k}", "0").strip()))
                        cfg.set('WateringInterval', k, str(iv))
                    except ValueError:
                        pass
                write_cfg(cfg)
                alert = '<div class="alert">Durations saved.</div>'
        elif action == "schedule":
            if not cfg.has_section('RuntimeParams'):
                cfg.add_section('RuntimeParams')
            for key in ('schedule_mode', 'schedule_time', 'schedule_offset',
                        'temp_cool_threshold', 'temp_hot_threshold',
                        'temp_cool_factor', 'temp_hot_factor'):
                val = request.form.get(key, '').strip()
                if val:
                    cfg.set('RuntimeParams', key, val)
            cfg.set('RuntimeParams', 'forecast_rain_skip',
                    'true' if request.form.get('forecast_rain_skip') else 'false')
            cfg.set('RuntimeParams', 'temp_adjustment',
                    'true' if request.form.get('temp_adjustment') else 'false')
            write_cfg(cfg)
            cfg = read_cfg()
            _schedule_next_run()
            alert = '<div class="alert">Schedule saved.</div>'

    # ── Station durations table ──────────────────────────────────────────────
    names       = station_names()
    norain_cfg  = {k: v.strip().lower() == 'true'
                   for k, v in cfg.items('NoRainSkip')}\
                  if cfg.has_section('NoRainSkip') else {}
    interval_cfg = {k: v.strip()
                    for k, v in cfg.items('WateringInterval')}\
                   if cfg.has_section('WateringInterval') else {}
    _iv_options  = [(0, 'Every day'), (2, 'Every 2 days'), (3, 'Every 3 days'),
                    (4, 'Every 4 days'), (7, 'Weekly'), (14, 'Every 2 weeks')]
    rows  = ""
    if cfg.has_section("WateringMinutes"):
        for k, v in cfg.items("WateringMinutes"):
            try:
                idx        = int(k) - 1
                mins       = float(v)
                name       = names.get(idx, f"Station {idx + 1}")
                nr_checked = 'checked' if norain_cfg.get(k, False) else ''
                iv_val     = int(interval_cfg.get(k, '0'))
                iv_opts    = ''.join(
                    f'<option value="{d}" {"selected" if iv_val == d else ""}>{lbl}</option>'
                    for d, lbl in _iv_options
                )
                rows += f"""<tr>
                  <td>{name}</td>
                  <td><input type="number" name="min_{k}" value="{mins:.0f}" min="0" step="1" style="width:70px"> min</td>
                  <td style="text-align:center" title="When checked, this station ignores rain skip (e.g. indoor plants)">
                    <input type="checkbox" name="norain_{k}" value="true" {nr_checked}>
                  </td>
                  <td><select name="interval_{k}" style="width:130px">{iv_opts}</select></td>
                </tr>"""
            except ValueError:
                pass

    # ── Schedule settings ────────────────────────────────────────────────────
    rtp           = dict(cfg.items('RuntimeParams')) if cfg.has_section('RuntimeParams') else {}
    sched_mode    = rtp.get('schedule_mode',      'fixed')
    sched_time    = rtp.get('schedule_time',      '03:00')
    sched_offset  = rtp.get('schedule_offset',    '-30')
    forecast_skip = rtp.get('forecast_rain_skip', 'false').lower() == 'true'
    temp_adj      = rtp.get('temp_adjustment',    'false').lower() == 'true'
    cool_thresh   = rtp.get('temp_cool_threshold', '20')
    hot_thresh    = rtp.get('temp_hot_threshold',  '30')
    cool_factor   = rtp.get('temp_cool_factor',    '0.75')
    hot_factor    = rtp.get('temp_hot_factor',     '1.30')

    fixed_sel    = 'selected' if sched_mode == 'fixed'   else ''
    sunrise_sel  = 'selected' if sched_mode == 'sunrise' else ''
    forecast_chk = 'checked'  if forecast_skip           else ''
    temp_adj_chk = 'checked'  if temp_adj                else ''

    nxt = get_next_run_time()
    if sched_mode == 'sunrise':
        sched_desc = f"Relative to sunrise (offset {sched_offset} min)"
    else:
        sched_desc = f"Daily at {sched_time}"
    if nxt:
        sched_desc += f" &mdash; next run <strong>{nxt}</strong>"

    body = f"""
    {alert}
    <h2>Program</h2>
    <p>Schedule: {sched_desc}. Set duration to 0 to skip a station.</p>
    <h3>Station Durations</h3>
    <form method="post">
      <input type="hidden" name="action" value="durations">
      <table>
        <thead><tr><th>Station</th><th>Duration</th><th title="Ignore rain skip for this station">No-Rain Skip &#x1F4A7;</th><th>Min Interval</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="margin-top:1rem">
        <button class="btn btn-green" type="submit">Save Durations</button>
      </p>
    </form>
    <h3>Schedule Configuration</h3>
    <form method="post">
      <input type="hidden" name="action" value="schedule">
      <table>
        <tr>
          <td style="width:55%"><strong>Schedule mode</strong></td>
          <td>
            <select name="schedule_mode">
              <option value="fixed" {fixed_sel}>Fixed daily time</option>
              <option value="sunrise" {sunrise_sel}>Relative to sunrise</option>
            </select>
          </td>
        </tr>
        <tr>
          <td>Daily run time <small>(HH:MM, for fixed mode)</small></td>
          <td><input type="text" name="schedule_time" value="{sched_time}" style="width:80px" placeholder="03:00"></td>
        </tr>
        <tr>
          <td>Sunrise offset (minutes) <small>(negative = before sunrise)</small></td>
          <td><input type="number" name="schedule_offset" value="{sched_offset}" style="width:80px"> min</td>
        </tr>
        <tr>
          <td><strong>Skip if forecast rain &ge; threshold</strong> <small>(today or tomorrow)</small></td>
          <td><input type="checkbox" name="forecast_rain_skip" value="true" {forecast_chk}></td>
        </tr>
        <tr>
          <td><strong>Adjust duration by temperature</strong></td>
          <td><input type="checkbox" name="temp_adjustment" value="true" {temp_adj_chk}></td>
        </tr>
        <tr>
          <td>Cool threshold &deg;C <small>(below this &rarr; cool duration factor)</small></td>
          <td><input type="number" name="temp_cool_threshold" value="{cool_thresh}" step="0.5" style="width:80px"></td>
        </tr>
        <tr>
          <td>Hot threshold &deg;C <small>(above this &rarr; hot duration factor)</small></td>
          <td><input type="number" name="temp_hot_threshold" value="{hot_thresh}" step="0.5" style="width:80px"></td>
        </tr>
        <tr>
          <td>Cool duration factor <small>(e.g. 0.75 = 75% of configured minutes)</small></td>
          <td><input type="number" name="temp_cool_factor" value="{cool_factor}" step="0.05" min="0.1" max="2.0" style="width:80px"></td>
        </tr>
        <tr>
          <td>Hot duration factor <small>(e.g. 1.30 = 130% of configured minutes)</small></td>
          <td><input type="number" name="temp_hot_factor" value="{hot_factor}" step="0.05" min="0.1" max="2.0" style="width:80px"></td>
        </tr>
      </table>
      <p style="margin-top:1rem">
        <button class="btn btn-green" type="submit">Save Schedule</button>
      </p>
    </form>"""
    return page("Program", body)


def _parse_last_station_runs() -> dict[str, str]:
    """Scan the log (bottom-up) for the last OFF event for each station.
    Returns {station_name: 'YYYY-MM-DD HH:MM:SS'} skipping Master and Test entries."""
    last_runs: dict[str, str] = {}
    pattern = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] <(.+?) OFF')
    try:
        with open(LOG_FILE) as f:
            lines = f.readlines()
        for line in reversed(lines):
            m = pattern.match(line)
            if m:
                ts, name = m.group(1), m.group(2)
                if name == 'Master' or name.startswith('Test '):
                    continue
                if name not in last_runs:
                    last_runs[name] = ts
    except FileNotFoundError:
        pass
    return last_runs


def _time_ago(ts_str: str) -> str:
    """Convert 'YYYY-MM-DD HH:MM:SS' to a compact human-readable delta."""
    try:
        dt    = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        total = int((datetime.datetime.now() - dt).total_seconds())
        if total < 60:    return "just now"
        if total < 3600:  return f"{total // 60}m ago"
        if total < 86400:
            return f"{total // 3600}h {(total % 3600) // 60}m ago"
        return f"{total // 86400}d {(total % 86400) // 3600}h ago"
    except (ValueError, TypeError):
        return ""


@app.route("/log")
def show_log():
    cfg = read_cfg()
    w   = fetch_weather()

    # ── Precipitation vs threshold panel ──────────────────────────────────────
    threshold = 0.0
    try:
        threshold = float(cfg.get('RuntimeParams', 'precipitation'))
    except (configparser.Error, ValueError):
        pass
    forecast_skip_on = (cfg.get('RuntimeParams', 'forecast_rain_skip', fallback='false')
                        .strip().lower() == 'true') if cfg.has_section('RuntimeParams') else False
    temp_adj_on = (cfg.get('RuntimeParams', 'temp_adjustment', fallback='false')
                   .strip().lower() == 'true') if cfg.has_section('RuntimeParams') else False

    def _precip_row(label: str, mm, thr: float) -> str:
        if mm is None:
            return f"<tr><td>{label}</td><td>—</td><td>—</td><td>—</td></tr>"
        mm = float(mm)
        if thr <= 0:
            return f"<tr><td>{label}</td><td>{mm:.1f}mm</td><td>—</td><td style='color:#777'>no threshold set</td></tr>"
        spare = thr - mm
        if spare <= 0:
            margin = f'<span style="color:#a94442;font-weight:bold">{-spare:.1f}mm over — would skip</span>'
            style  = ' style="background:#f2dede"'
        elif spare < thr * 0.4:
            margin = f'<span style="color:#8a6d3b">{spare:.1f}mm to skip ({spare / thr * 100:.0f}% headroom)</span>'
            style  = ' style="background:#fcf8e3"'
        else:
            margin = f'<span style="color:#3c763d">{spare:.1f}mm to skip ({spare / thr * 100:.0f}% headroom)</span>'
            style  = ''
        return f"<tr{style}><td>{label}</td><td>{mm:.1f}mm</td><td>{thr:.1f}mm</td><td>{margin}</td></tr>"

    if not w.get("error"):
        yesterday_mm = w.get("yesterday_precip_mm") or 0
        today_mm     = w.get("today_precip_mm")    or 0
        tomorrow_mm  = w.get("tomorrow_precip_mm")
        today_high   = w.get("today_high")
        try:
            cool_t = float(cfg.get('RuntimeParams', 'temp_cool_threshold', fallback='20'))
            hot_t  = float(cfg.get('RuntimeParams', 'temp_hot_threshold',  fallback='30'))
            cool_f = float(cfg.get('RuntimeParams', 'temp_cool_factor',    fallback='0.75'))
            hot_f  = float(cfg.get('RuntimeParams', 'temp_hot_factor',     fallback='1.30'))
        except (configparser.Error, ValueError):
            cool_t, hot_t, cool_f, hot_f = 20.0, 30.0, 0.75, 1.30

        if today_high is not None and temp_adj_on:
            if today_high < cool_t:
                temp_status = (f'<span style="color:#2a6496">Cool &mdash; {today_high}\u00b0C '
                               f'&lt; {cool_t}\u00b0C &rarr; &times;{cool_f} durations</span>')
            elif today_high > hot_t:
                temp_status = (f'<span style="color:#a94442">Hot &mdash; {today_high}\u00b0C '
                               f'&gt; {hot_t}\u00b0C &rarr; &times;{hot_f} durations</span>')
            else:
                temp_status = (f'<span style="color:#3c763d">Normal &mdash; {today_high}\u00b0C '
                               f'({cool_t}\u2013{hot_t}\u00b0C range), no adjustment</span>')
        elif not temp_adj_on:
            high_str    = f"{today_high}\u00b0C" if today_high is not None else "unknown"
            temp_status = f"Adjustment disabled (today high: {high_str})"
        else:
            temp_status = "Today's high unavailable"

        precip_section = f"""
        <h3 style="margin-top:1.2rem">Precipitation vs Skip Threshold
          <small style="font-weight:normal;color:#777">&nbsp;&mdash;&nbsp;forward skip: {'on' if forecast_skip_on else 'off'}</small>
        </h3>
        <table>
          <thead><tr><th>Period</th><th>Rain</th><th>Skip threshold</th><th>Margin</th></tr></thead>
          <tbody>
            {_precip_row('Yesterday (actual)',  yesterday_mm, threshold)}
            {_precip_row('Today (forecast)',    today_mm,     threshold)}
            {_precip_row('Tomorrow (forecast)', tomorrow_mm,  threshold)}
          </tbody>
        </table>
        <p style="margin:.4rem 0"><strong>Temperature:</strong> {temp_status}</p>"""
    else:
        precip_section = f'<div class="alert err" style="margin-top:.8rem">Weather unavailable: {w["error"]}</div>'

    # ── Station history panel ─────────────────────────────────────────────────
    last_runs    = _parse_last_station_runs()
    names        = station_names()
    nxt          = get_next_run_time()
    nxt_label    = nxt or "\u2014"
    now_dt       = datetime.datetime.now()
    norain_log   = ({k: v.strip().lower() == 'true' for k, v in cfg.items('NoRainSkip')}
                    if cfg.has_section('NoRainSkip') else {})
    interval_log = {}
    if cfg.has_section('WateringInterval'):
        for _k, _v in cfg.items('WateringInterval'):
            try:
                interval_log[int(_k) - 1] = max(0, int(_v))
            except ValueError:
                pass

    stn_rows = ""
    for idx in range(len(STATIONS)):
        name     = names.get(idx, f"Station {idx + 1}")
        last_ts  = last_runs.get(name)
        last_str = (f"{last_ts}&nbsp;<span style='color:#777'>({_time_ago(last_ts)})</span>"
                    if last_ts else "<span style='color:#777'>Never recorded</span>")
        interval = interval_log.get(idx, 0)
        iv_label = f"Every {interval}d" if interval > 0 else "Daily"
        no_rain  = norain_log.get(str(idx + 1), False)
        nr_badge = " \U0001f4a7" if no_rain else ""

        # Would this station run at the next opportunity?
        skip_reason = ""
        if interval > 0 and last_ts:
            try:
                days_ago = (now_dt - datetime.datetime.strptime(
                    last_ts, "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400
                if days_ago < interval:
                    days_left   = interval - days_ago
                    skip_reason = (f'<span style="color:#a94442">&#x26A0; interval: '
                                   f'{days_left:.1f}d remaining</span>')
            except ValueError:
                pass
        if not skip_reason:
            skip_reason = '<span style="color:#3c763d">&#x2713; ready</span>'

        stn_rows += (f"<tr><td>{name}</td>"
                     f"<td style='font-family:monospace;font-size:.9rem'>{last_str}</td>"
                     f"<td>{iv_label}{nr_badge}</td>"
                     f"<td>{skip_reason}</td>"
                     f"<td>{nxt_label}</td></tr>")

    station_section = f"""
    <h3 style="margin-top:1.2rem">Station History &amp; Next Run</h3>
    <table>
      <thead><tr><th>Station</th><th>Last run</th><th>Interval</th><th>Status</th><th>Next run</th></tr></thead>
      <tbody>{stn_rows}</tbody>
    </table>"""

    # ── Log entries ───────────────────────────────────────────────────────────
    entries = []
    try:
        with open(LOG_FILE) as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                escaped = (line.replace("&", "&amp;")
                               .replace("<", "&lt;")
                               .replace(">", "&gt;"))
                if "Master" in line:
                    color = "#2a6496"
                elif "Skipped" in line or "skipped" in line:
                    color = "#c07000"
                elif "Rain skip active" in line:
                    color = "#c07000"
                elif "Rain check:" in line or "Temperature adjustment:" in line:
                    color = "#999"
                elif "Test" in line:
                    color = "#8a6d3b"
                elif "ON" in line or "OFF" in line:
                    color = "#3c763d"
                else:
                    color = "#555"
                entries.append(
                    f'<p style="color:{color};margin:.15rem 0;font-family:monospace;font-size:.9rem">{escaped}</p>'
                )
    except FileNotFoundError:
        pass

    entries.reverse()   # newest first
    log_lines = "\n".join(entries)

    body = f"""
    <h2>Log</h2>
    {precip_section}
    {station_section}
    <h3 style="margin-top:1.5rem">Activity Log</h3>
    {log_lines or '<p>Log is empty.</p>'}"""
    return page("Log", body)


@app.route("/reboot", methods=["GET", "POST"])
def reboot():
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "reboot":
            subprocess.Popen(["sudo", "shutdown", "-r", "now"])
            return page("Rebooting", """
            <h2>Rebooting&hellip;</h2>
            <p>The system is rebooting. Please wait about 60 seconds, then
               <a href="/">refresh this page</a>.</p>""")
        elif action == "shutdown":
            subprocess.Popen(["sudo", "shutdown", "-h", "now"])
            return page("Shutting Down", """
            <h2>Shutting Down&hellip;</h2>
            <p>The system is shutting down.</p>""")

    body = """
    <h2>System Control</h2>
    <form class="row-form" method="post">
      <input type="hidden" name="action" value="reboot">
      <button class="btn" onclick="return confirm('Reboot the Pi?')">&#x21BA; Reboot</button>
    </form>
    &nbsp;
    <form class="row-form" method="post">
      <input type="hidden" name="action" value="shutdown">
      <button class="btn btn-red" onclick="return confirm('Shut down the Pi?')">&#x23FC; Shutdown</button>
    </form>"""
    return page("System Control", body)


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ensure_default_cfg()
    gpio_setup()

    # Restore persisted state (delay/pause) from previous run
    _load_state()

    # Schedule the main watering job (dynamic date-based, reads config each time)
    scheduler.start()
    _schedule_next_run()

    # If scheduler was paused before restart, re-apply that
    with _lock:
        if not _state["enabled"]:
            scheduler.pause()

    # Start the delay-watcher background thread
    threading.Thread(target=_delay_watcher, daemon=True).start()

    app.run(host="0.0.0.0", port=80, debug=False)
