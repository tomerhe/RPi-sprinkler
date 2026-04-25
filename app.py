#!/usr/bin/env python3
"""
Pi Sprinkler - Flask web application
Replaces all CGI scripts with a single modern web app.
Run with: sudo python3 app.py
"""

import configparser
import datetime
import hmac
import json
import os
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

# ── Global state ───────────────────────────────────────────────────────────────
_lock  = threading.Lock()
_state = {
    "running":   False,   # never persisted — always False on startup
    "enabled":   True,
    "delay":     False,
    "resume_at": 0.0,     # epoch time when delay expires
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
        _pi.write(MASTER_PIN, 0)  # master NC wiring: LOW = energized = NC open = valve CLOSED (safe default)

def gpio_write(pin, on: bool):
    if GPIO_AVAILABLE:
        _pi.write(pin, 0 if on else 1)
    

def gpio_read(pin) -> bool:
    return GPIO_AVAILABLE and (_pi.read(pin) == 0)

def any_station_on() -> bool:
    return any(gpio_read(p) for p in STATIONS)

def master_on():
    """Open master valve. Relay wired NC: de-energize (HIGH) = valve open."""
    if MASTER_PIN is not None:
        gpio_write(MASTER_PIN, False)   # write 1 = de-energize = NC closed = valve open

def master_off():
    """Close master valve. Relay wired NC: energize (LOW) = valve closed."""
    if MASTER_PIN is not None:
        gpio_write(MASTER_PIN, True)    # write 0 = energize = NC open = valve closed

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
        f"&daily=temperature_2m_max,precipitation_sum"
        f"&current_weather=true"
        f"&temperature_unit=celsius&precipitation_unit=mm"
        f"&past_days=1&forecast_days=1&timezone=auto"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        cw    = data["current_weather"]
        daily = data["daily"]
        return {
            "temperature":      cw["temperature"],
            "conditions":       WMO_CODES.get(cw.get("weathercode", -1), "Unknown"),
            "today_high":       daily["temperature_2m_max"][1],
            "yesterday_high":   daily["temperature_2m_max"][0],
            "today_precip":        precip_label(daily["precipitation_sum"][1]),
            "yesterday_precip":    precip_label(daily["precipitation_sum"][0]),
            "yesterday_precip_mm": daily["precipitation_sum"][0],
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
    try:
        cfg = read_cfg()

        # Rain skip: check yesterday's precipitation against threshold
        w = fetch_weather()
        if not w.get("error"):
            try:
                threshold = float(cfg.get('RuntimeParams', 'precipitation'))
            except (configparser.Error, ValueError):
                threshold = 0.0
            yesterday_mm = float(w.get("yesterday_precip_mm") or 0)
            if threshold > 0 and yesterday_mm >= threshold:
                log(f"<Skipped \u2014 yesterday's rain {yesterday_mm:.1f}mm \u2265 threshold {threshold:.1f}mm>")
                return

        ran = True
        log("<Master ON>")
        master_on()

        minutes: dict[int, float] = {}
        if cfg.has_section("WateringMinutes"):
            for k, v in cfg.items("WateringMinutes"):
                try:
                    minutes[int(k) - 1] = float(v)
                except ValueError:
                    pass
        names = station_names()

        for i, pin in enumerate(STATIONS):
            with _lock:
                if not _state["enabled"]:
                    break
            dur = minutes.get(i, 0)
            if dur > 0:
                name = names.get(i, f"Station {i + 1}")
                gpio_write(pin, True)
                log(f"<{name} ON>")
                time.sleep(dur * 60)
                gpio_write(pin, False)
                log(f"<{name} OFF>")
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
        time.sleep(30)


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
@app.before_request
def require_login():
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

    body = f"""
    <h2>Home</h2>
    <p>Current time: <strong>{now}</strong></p>
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
            all_off()

    cfg        = read_cfg()
    stn_names  = station_names()

    rows = ""
    for i, pin in enumerate(STATIONS):
        on   = gpio_read(pin)
        name = stn_names.get(i, f"Station {i + 1}")
        rows += f"""
        <tr class="{'on' if on else ''}">
          <td>{i + 1}</td>
          <td>{name}</td>
          <td>{'&#x25CF; ON' if on else '&#x25CB; OFF'}</td>
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
        test_section = """
        <form method="post">
          <input type="hidden" name="action" value="test_cancel">
          <button class="btn btn-red">&#x25A0; Cancel Test Run</button>
        </form>"""
    else:
        test_section = """
        <form method="post" style="display:flex;gap:.6rem;align-items:center">
          <input type="hidden" name="action" value="test_start">
          <label>Duration per station (sec):</label>
          <input type="number" name="duration" value="10" min="1" style="width:80px">
          <button class="btn btn-green">&#x25B6; Start Test Run</button>
        </form>"""

    body = f"""
    {alert}
    <h2>Manual Station Control</h2>
    <table>
      <thead><tr><th>#</th><th>Name</th><th>State</th><th>Action</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <h3>Test Run</h3>
    {test_section}
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
            alert = '<div class="alert">Settings saved successfully.</div>'
        elif action == "reset":
            if os.path.exists(CONFIG_FILE):
                backup = CONFIG_FILE + "." + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                os.rename(CONFIG_FILE, backup)
            ensure_default_cfg()
            cfg   = read_cfg()
            alert = '<div class="alert">Settings reset to defaults.</div>'

    rows = ""
    for section in cfg.sections():
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
        if cfg.has_section("WateringMinutes"):
            for k in cfg.options("WateringMinutes"):
                val = request.form.get(f"min_{k}", "").strip()
                try:
                    cfg.set("WateringMinutes", k, str(max(0.0, float(val))))
                except ValueError:
                    pass
            write_cfg(cfg)
            alert = '<div class="alert">Durations saved.</div>'

    names = station_names()
    rows  = ""
    if cfg.has_section("WateringMinutes"):
        for k, v in cfg.items("WateringMinutes"):
            try:
                idx  = int(k) - 1
                mins = float(v)
                name = names.get(idx, f"Station {idx + 1}")
                rows += f"""<tr>
                  <td>{name}</td>
                  <td><input type="number" name="min_{k}" value="{mins:.0f}" min="0" step="1" style="width:70px"> min</td>
                </tr>"""
            except ValueError:
                pass

    body = f"""
    {alert}
    <h2>Program</h2>
    <p>The sprinkler runs <strong>every day at 03:00</strong>.
       Set duration to 0 to skip a station.</p>
    <form method="post">
      <table>
        <thead><tr><th>Station</th><th>Duration</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="margin-top:1rem">
        <button class="btn btn-green" type="submit">Save</button>
      </p>
    </form>"""
    return page("Program", body)


@app.route("/log")
def show_log():
    entries = []
    try:
        with open(LOG_FILE) as f:
            for raw in f:
                line    = raw.strip()
                if not line:
                    continue
                escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                if "Master" in line:
                    color = "#2a6496"
                elif "Test" in line:
                    color = "#8a6d3b"
                elif "ON" in line or "OFF" in line:
                    color = "#3c763d"
                else:
                    color = "#555"
                entries.append(f'<p style="color:{color};margin:.15rem 0;font-family:monospace;font-size:.9rem">{escaped}</p>')
    except FileNotFoundError:
        pass

    entries.reverse()   # newest first
    lines = "\n".join(entries)
    body = f"<h2>Log</h2>{lines or '<p>Log is empty.</p>'}"
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

    # Schedule the main watering job
    scheduler.add_job(job1, 'cron', hour=3, minute=0)
    scheduler.start()

    # If scheduler was paused before restart, re-apply that
    with _lock:
        if not _state["enabled"]:
            scheduler.pause()

    # Start the delay-watcher background thread
    threading.Thread(target=_delay_watcher, daemon=True).start()

    app.run(host="0.0.0.0", port=80, debug=False)
