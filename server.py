#!/usr/bin/env python3
"""
Web server for the vehicle price tracker.
Serves the static report AND provides an API for:
  GET  /api/status          — current run status
  POST /api/run             — trigger a scrape run (background)
  POST /api/vehicle/add     — add a new vehicle + search config
  GET  /api/schedule        — get current schedule config
  POST /api/schedule        — update schedule config
"""

import json
import logging
import os
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from config.logging_config import setup_logging
from db.connection import get_connection

setup_logging()
logger = logging.getLogger(__name__)

REPORT_DIR = os.path.join(os.path.dirname(__file__), "report")
BASE_DIR = os.path.dirname(__file__)
VENV_PYTHON = os.path.join(BASE_DIR, "venv", "bin", "python")
SCHEDULE_FILE = os.path.join(BASE_DIR, "config", "schedule.json")

# ── Schedule defaults ────────────────────────────────────────────────────────
DEFAULT_SCHEDULE = {
    "enabled": True,
    "frequency": "weekly",   # "daily" | "weekly" | "interval"
    "weekday": 6,            # 0=Mon … 6=Sun (only for weekly)
    "hour": 6,               # 0-23
    "minute": 0,             # 0-59
    "interval_hours": 24,    # only for "interval"
}

# ── Global state ─────────────────────────────────────────────────────────────
_run_lock = threading.Lock()
_run_state = {
    "running": False,
    "started_at": None,
    "log": [],
    "exit_code": None,
    "last_run": None,
}


# ── Schedule helpers ─────────────────────────────────────────────────────────

def load_schedule() -> dict:
    try:
        with open(SCHEDULE_FILE) as f:
            s = json.load(f)
            # fill missing keys with defaults
            return {**DEFAULT_SCHEDULE, **s}
    except Exception:
        return dict(DEFAULT_SCHEDULE)


def save_schedule(cfg: dict):
    os.makedirs(os.path.dirname(SCHEDULE_FILE), exist_ok=True)
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    logger.info("Schedule saved: %s", cfg)


def _last_scheduled_ts(cfg: dict) -> float:
    """Return the Unix timestamp of the most recent PAST scheduled slot.

    The scheduler fires when now >= last_scheduled AND last_run < last_scheduled,
    i.e. the due slot has arrived but hasn't been served yet.
    """
    now = datetime.now()
    freq = cfg.get("frequency", "weekly")
    hour = cfg.get("hour", 6)
    minute = cfg.get("minute", 0)

    if freq == "interval":
        hours = cfg.get("interval_hours", 24)
        last_run = cfg.get("last_run")  # persisted in schedule.json
        if last_run:
            return last_run + hours * 3600
        return time.time() - 1  # never run → due immediately

    # daily / weekly: find the most recent past occurrence of HH:MM
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if freq == "daily":
        if target > now:          # today's slot is still in the future
            target -= timedelta(days=1)
        return target.timestamp()

    # weekly
    weekday = cfg.get("weekday", 6)
    days_ago = (now.weekday() - weekday) % 7
    target -= timedelta(days=days_ago)
    if target > now:
        target -= timedelta(days=7)
    return target.timestamp()


def _next_run_ts(cfg: dict) -> float:
    """Return the Unix timestamp of the NEXT scheduled run (for display only)."""
    now = datetime.now()
    freq = cfg.get("frequency", "weekly")
    hour = cfg.get("hour", 6)
    minute = cfg.get("minute", 0)

    if freq == "interval":
        hours = cfg.get("interval_hours", 24)
        last_run = cfg.get("last_run") or _run_state.get("last_run")
        if last_run:
            return last_run + hours * 3600
        return time.time() + hours * 3600

    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if freq == "daily":
        if target <= now:
            target += timedelta(days=1)
        return target.timestamp()

    weekday = cfg.get("weekday", 6)
    days_ahead = (weekday - now.weekday()) % 7
    if days_ahead == 0 and target <= now:
        days_ahead = 7
    target += timedelta(days=days_ahead)
    return target.timestamp()


def _scheduler_loop():
    """Background thread: fires scrape runs according to schedule."""
    logger.info("Scheduler thread started")
    while True:
        time.sleep(30)
        cfg = load_schedule()
        if not cfg.get("enabled"):
            continue

        last_scheduled = _last_scheduled_ts(cfg)
        # Use persisted last_run from schedule.json (survives restarts)
        last_run = cfg.get("last_run") or _run_state.get("last_run") or 0

        if time.time() >= last_scheduled and last_run < last_scheduled:
            logger.info(
                "Scheduled run triggered — slot: %s, last_run: %s",
                datetime.fromtimestamp(last_scheduled).strftime("%Y-%m-%d %H:%M"),
                datetime.fromtimestamp(last_run).strftime("%Y-%m-%d %H:%M") if last_run else "never",
            )
            with _run_lock:
                already = _run_state["running"]
            if not already:
                t = threading.Thread(target=_do_scrape_run, daemon=True)
                t.start()


# ── Scrape run ───────────────────────────────────────────────────────────────

def _do_scrape_run(target=None):
    with _run_lock:
        if _run_state["running"]:
            return
        _run_state["running"] = True
        _run_state["started_at"] = time.time()
        _run_state["log"] = []
        _run_state["exit_code"] = None

    cmd = [VENV_PYTHON, os.path.join(BASE_DIR, "main.py")]
    if target:
        cmd += ["--target", target]

    xvfb = "/usr/bin/xvfb-run"
    if os.path.exists(xvfb):
        cmd = [xvfb, "-a", "--server-args=-screen 0 1920x1080x24"] + cmd

    logger.info("Starting scrape run: %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=BASE_DIR,
        )
        for line in proc.stdout:
            line = line.rstrip()
            logger.info("[scrape] %s", line)
            with _run_lock:
                _run_state["log"].append(line)
                if len(_run_state["log"]) > 500:
                    _run_state["log"] = _run_state["log"][-500:]
        proc.wait()
        now_ts = time.time()
        with _run_lock:
            _run_state["exit_code"] = proc.returncode
            _run_state["last_run"] = now_ts
        # Persist last_run so the scheduler survives service restarts
        try:
            cfg = load_schedule()
            cfg["last_run"] = now_ts
            save_schedule(cfg)
        except Exception as e:
            logger.warning("Could not persist last_run: %s", e)
        logger.info("Scrape run finished, exit code %d", proc.returncode)
    except Exception as e:
        logger.error("Scrape run error: %s", e)
        with _run_lock:
            _run_state["log"].append(f"ERROR: {e}")
            _run_state["exit_code"] = -1
    finally:
        try:
            subprocess.run(
                [VENV_PYTHON, os.path.join(BASE_DIR, "report.py")],
                cwd=BASE_DIR, timeout=60,
            )
            logger.info("Report regenerated")
        except Exception as e:
            logger.error("Report generation failed: %s", e)
        with _run_lock:
            _run_state["running"] = False


# ── Vehicle management ───────────────────────────────────────────────────────

def _get_vehicles():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT v.id, v.name, v.description,
                       sc.id AS config_id, sc.platform, sc.search_url, sc.active
                FROM vehicles v
                LEFT JOIN search_configs sc ON sc.vehicle_id = v.id
                ORDER BY v.name, sc.platform
            """)
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]
        # Group by vehicle
        vehicles = {}
        for r in rows:
            vid = r["id"]
            if vid not in vehicles:
                vehicles[vid] = {
                    "id": vid,
                    "name": r["name"],
                    "description": r["description"],
                    "configs": [],
                }
            if r["config_id"] is not None:
                vehicles[vid]["configs"].append({
                    "id": r["config_id"],
                    "platform": r["platform"],
                    "search_url": r["search_url"],
                    "active": r["active"],
                })
        return list(vehicles.values())
    finally:
        conn.close()


def _add_vehicle(name, description, platform, search_url):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO vehicles (name, description)
                   VALUES (%s, %s)
                   ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description
                   RETURNING id""",
                (name.strip(), description.strip()),
            )
            vehicle_id = cur.fetchone()[0]
            cur.execute(
                """INSERT INTO search_configs (vehicle_id, platform, search_url, active)
                   VALUES (%s, %s, %s, TRUE)""",
                (vehicle_id, platform.strip(), search_url.strip()),
            )
            conn.commit()
            logger.info("Added vehicle '%s' on %s", name, platform)
            return vehicle_id
    finally:
        conn.close()


def _edit_vehicle(vehicle_id, name, description, config_id, platform, search_url):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE vehicles SET name = %s, description = %s WHERE id = %s",
                (name.strip(), description.strip(), vehicle_id),
            )
            if config_id:
                cur.execute(
                    "UPDATE search_configs SET platform = %s, search_url = %s WHERE id = %s",
                    (platform.strip(), search_url.strip(), config_id),
                )
            conn.commit()
            logger.info("Edited vehicle id=%d", vehicle_id)
    finally:
        conn.close()


def _delete_vehicle_data(vehicle_id):
    """Delete all scraped data for a vehicle, keeping the vehicle + search_configs."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Delete snapshots first (FK references listings + scrape_runs)
            cur.execute("""
                DELETE FROM listing_snapshots
                WHERE listing_id IN (
                    SELECT l.id FROM listings l
                    JOIN search_configs sc ON sc.id = l.search_config_id
                    WHERE sc.vehicle_id = %s
                )
            """, (vehicle_id,))
            # Delete scrape_run snapshots via scrape_run FK
            cur.execute("""
                DELETE FROM listing_snapshots
                WHERE scrape_run_id IN (
                    SELECT sr.id FROM scrape_runs sr
                    JOIN search_configs sc ON sc.id = sr.search_config_id
                    WHERE sc.vehicle_id = %s
                )
            """, (vehicle_id,))
            cur.execute("""
                DELETE FROM listings
                WHERE search_config_id IN (
                    SELECT id FROM search_configs WHERE vehicle_id = %s
                )
            """, (vehicle_id,))
            cur.execute("""
                DELETE FROM scrape_runs
                WHERE search_config_id IN (
                    SELECT id FROM search_configs WHERE vehicle_id = %s
                )
            """, (vehicle_id,))
            conn.commit()
            logger.info("Deleted scraped data for vehicle id=%d", vehicle_id)
    finally:
        conn.close()


def _delete_vehicle(vehicle_id):
    """Delete a vehicle entirely including its search configs and all scraped data."""
    _delete_vehicle_data(vehicle_id)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM search_configs WHERE vehicle_id = %s", (vehicle_id,))
            cur.execute("DELETE FROM vehicles WHERE id = %s", (vehicle_id,))
            conn.commit()
            logger.info("Deleted vehicle id=%d", vehicle_id)
    finally:
        conn.close()


# ── HTTP Handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.debug("HTTP %s", fmt % args)

    def _send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        ext = os.path.splitext(path)[1].lower()
        types = {".html": "text/html", ".js": "text/javascript",
                 ".css": "text/css", ".json": "application/json"}
        ct = types.get(ext, "application/octet-stream")
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/status":
            with _run_lock:
                state = dict(_run_state)
            cfg = load_schedule()
            next_ts = _next_run_ts(cfg) if cfg.get("enabled") else None
            state["schedule"] = cfg
            state["next_run"] = next_ts
            self._send_json(200, state)
            return

        if path == "/api/schedule":
            cfg = load_schedule()
            next_ts = _next_run_ts(cfg) if cfg.get("enabled") else None
            self._send_json(200, {**cfg, "next_run": next_ts})
            return

        if path == "/api/vehicles":
            self._send_json(200, _get_vehicles())
            return

        if path == "/" or path == "/index.html":
            self._send_file(os.path.join(REPORT_DIR, "index.html"))
        else:
            self._send_file(os.path.join(REPORT_DIR, path.lstrip("/")))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = dict(urllib.parse.parse_qsl(body))

        path = self.path.split("?")[0]

        if path == "/api/run":
            with _run_lock:
                already_running = _run_state["running"]
            if already_running:
                self._send_json(409, {"error": "Scrape läuft bereits"})
                return
            target = data.get("target")
            t = threading.Thread(target=_do_scrape_run, args=(target,), daemon=True)
            t.start()
            self._send_json(200, {"status": "started"})

        elif path == "/api/vehicle/add":
            name = data.get("name", "").strip()
            description = data.get("description", "").strip()
            platform = data.get("platform", "").strip()
            search_url = data.get("search_url", "").strip()
            if not name or not platform or not search_url:
                self._send_json(400, {"error": "name, platform und search_url sind Pflichtfelder"})
                return
            try:
                vid = _add_vehicle(name, description, platform, search_url)
                subprocess.run(
                    [VENV_PYTHON, os.path.join(BASE_DIR, "report.py")],
                    cwd=BASE_DIR, timeout=60,
                )
                self._send_json(200, {"status": "ok", "vehicle_id": vid})
            except Exception as e:
                logger.error("Add vehicle error: %s", e)
                self._send_json(500, {"error": str(e)})

        elif path == "/api/schedule":
            allowed = {"enabled", "frequency", "weekday", "hour", "minute", "interval_hours"}
            cfg = load_schedule()
            for k, v in data.items():
                if k in allowed:
                    cfg[k] = v
            save_schedule(cfg)
            next_ts = _next_run_ts(cfg) if cfg.get("enabled") else None
            self._send_json(200, {**cfg, "next_run": next_ts})

        elif path == "/api/vehicle/edit":
            vehicle_id = data.get("vehicle_id")
            name = data.get("name", "").strip()
            description = data.get("description", "").strip()
            config_id = data.get("config_id")
            platform = data.get("platform", "").strip()
            search_url = data.get("search_url", "").strip()
            if not vehicle_id or not name:
                self._send_json(400, {"error": "vehicle_id und name sind Pflichtfelder"})
                return
            try:
                _edit_vehicle(vehicle_id, name, description, config_id, platform, search_url)
                subprocess.run(
                    [VENV_PYTHON, os.path.join(BASE_DIR, "report.py")],
                    cwd=BASE_DIR, timeout=60,
                )
                self._send_json(200, {"status": "ok"})
            except Exception as e:
                logger.error("Edit vehicle error: %s", e)
                self._send_json(500, {"error": str(e)})

        elif path == "/api/vehicle/delete-data":
            vehicle_id = data.get("vehicle_id")
            if not vehicle_id:
                self._send_json(400, {"error": "vehicle_id ist ein Pflichtfeld"})
                return
            try:
                _delete_vehicle_data(vehicle_id)
                subprocess.run(
                    [VENV_PYTHON, os.path.join(BASE_DIR, "report.py")],
                    cwd=BASE_DIR, timeout=60,
                )
                self._send_json(200, {"status": "ok"})
            except Exception as e:
                logger.error("Delete vehicle data error: %s", e)
                self._send_json(500, {"error": str(e)})

        elif path == "/api/vehicle/delete":
            vehicle_id = data.get("vehicle_id")
            if not vehicle_id:
                self._send_json(400, {"error": "vehicle_id ist ein Pflichtfeld"})
                return
            try:
                _delete_vehicle(vehicle_id)
                subprocess.run(
                    [VENV_PYTHON, os.path.join(BASE_DIR, "report.py")],
                    cwd=BASE_DIR, timeout=60,
                )
                self._send_json(200, {"status": "ok"})
            except Exception as e:
                logger.error("Delete vehicle error: %s", e)
                self._send_json(500, {"error": str(e)})

        else:
            self._send_json(404, {"error": "Not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Start background scheduler
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), Handler)
    logger.info("Server running on http://0.0.0.0:%d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
