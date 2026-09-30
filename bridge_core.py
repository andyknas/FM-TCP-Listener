"""
FM TCP Bridge - core engine (no GUI).

Listens on a TCP port. Each client connection carries ONE message: the client
connects, sends its payload, and either closes the connection or goes quiet.
The payload is spooled to disk, then delivered to FileMaker Server by running
a script through the OData API:

    POST {fm_host}/fmi/odata/v4/{fm_database}/Script.{fm_script}
    {"scriptParameterValue": "<parameter>"}

Anything that fails to deliver stays in the spool folder and is retried, so
nothing is lost if FileMaker Server is down or the Mac restarts.

Can be run on its own for testing:
    python3 bridge_core.py            (uses the normal settings file)
    python3 bridge_core.py --config /path/to/config.json
"""

import base64
import datetime as _dt
import json
import logging
import logging.handlers
import os
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

APP_NAME = "FM TCP Bridge"
KEYCHAIN_SERVICE = "com.nrgsoft.fmtcpbridge"

SUPPORT_DIR = os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
LOG_DIR = os.path.expanduser(f"~/Library/Logs/{APP_NAME}")
CONFIG_PATH = os.path.join(SUPPORT_DIR, "config.json")

DEFAULTS = {
    # --- TCP listener ---
    "listen_host": "0.0.0.0",          # 127.0.0.1 = this Mac only
    "listen_port": 9100,
    "allowed_clients": [],             # e.g. ["192.168.1.50"]; empty = anyone
    "message_idle_ms": 750,            # message is complete after this much silence...
    "connection_timeout_sec": 15,      # ...or the client closes, or this total time passes
    "max_message_bytes": 1048576,
    "text_encoding": "utf-8",          # bytes that don't decode are sent as base64
    "strip_whitespace": True,

    # --- FileMaker Server (OData) ---
    "fm_host": "",                     # e.g. https://fms.nrgsoft.com
    "fm_database": "",
    "fm_script": "",
    "fm_username": "",                 # password lives in the macOS Keychain
    "verify_tls": True,                # False only for self-signed test servers
    "http_timeout_sec": 30,
    "parameter_mode": "json",          # "json" = envelope with metadata, "raw" = payload text only

    # --- Reply to the TCP client before closing ---
    "reply_mode": "result",            # "result" = FileMaker script result, "ack" = ack_text, "none"
    "ack_text": "OK\r\n",
    "reply_wait_sec": 10,              # how long a client waits for the FileMaker result
    "reply_suffix": "\r\n",

    # --- Retry ---
    "retry_interval_sec": 30,
    "max_attempts": 0,                 # 0 = retry forever; otherwise move to failed/ after N tries
}


# ---------------------------------------------------------------------------
# Settings / Keychain
# ---------------------------------------------------------------------------

def ensure_dirs(support_dir=SUPPORT_DIR):
    for d in (support_dir, os.path.join(support_dir, "queue"),
              os.path.join(support_dir, "failed"), LOG_DIR):
        os.makedirs(d, exist_ok=True)


def load_config(path=CONFIG_PATH):
    """Load settings, writing a default file on first run. Unknown keys are kept."""
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(DEFAULTS, f, indent=2)
    with open(path) as f:
        user = json.load(f)
    cfg = dict(DEFAULTS)
    cfg.update(user)
    # add any new default keys to the file so they're discoverable
    if set(DEFAULTS) - set(user):
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2)
    return cfg


def config_problems(cfg):
    missing = [k for k in ("fm_host", "fm_database", "fm_script", "fm_username") if not cfg.get(k)]
    return [f"{k} is not set" for k in missing]


def get_password(username):
    env = os.environ.get("FM_TCP_BRIDGE_PASSWORD")
    if env is not None:
        return env
    if sys.platform != "darwin":
        return ""
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", username, "-w"],
        capture_output=True, text=True)
    return r.stdout.rstrip("\n") if r.returncode == 0 else ""


def set_password(username, password):
    r = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
         "-a", username, "-l", APP_NAME, "-w", password],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "Keychain write failed")


# ---------------------------------------------------------------------------
# FileMaker OData client
# ---------------------------------------------------------------------------

def odata_script_url(cfg):
    host = cfg["fm_host"].rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = "https://" + host
    db = urllib.parse.quote(cfg["fm_database"], safe="")
    script = urllib.parse.quote(cfg["fm_script"], safe="")
    return f"{host}/fmi/odata/v4/{db}/Script.{script}"


def call_filemaker(cfg, password, parameter):
    """Run the configured script. Returns (http_status, script_code, result_text, raw_body)."""
    url = odata_script_url(cfg)
    body = json.dumps({"scriptParameterValue": parameter}).encode("utf-8")
    token = base64.b64encode(f"{cfg['fm_username']}:{password}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "OData-Version": "4.0",
    })
    ctx = None
    if url.startswith("https://") and not cfg.get("verify_tls", True):
        ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=cfg["http_timeout_sec"], context=ctx) as resp:
            status, raw = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode("utf-8", "replace")
    code, result = None, None
    try:
        doc = json.loads(raw) if raw else {}
        sr = doc.get("scriptResult") or {}
        code = sr.get("code")
        result = sr.get("resultParameter")
        if status >= 300 and "error" in doc:
            result = doc["error"].get("message")
    except ValueError:
        pass
    return status, code, result, raw


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def _now_iso():
    return _dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.engine._handle_client(self.request, self.client_address)


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class Engine:
    """
    on_event(dict) is called from worker threads for every step, so a GUI can
    show traffic. Event "kind" values:
        info, error, in (data from TCP client), out (request to FileMaker),
        fm (FileMaker response), reply (data written back to TCP client)
    """

    def __init__(self, config_path=CONFIG_PATH, support_dir=SUPPORT_DIR, on_event=None):
        self.config_path = config_path
        self.support_dir = support_dir
        self.queue_dir = os.path.join(support_dir, "queue")
        self.failed_dir = os.path.join(support_dir, "failed")
        self.on_event = on_event or (lambda e: None)
        self.cfg = None
        self._server = None
        self._server_thread = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._waiters = {}            # job id -> (Event, result holder)
        self._waiters_lock = threading.Lock()
        self._deliver_lock = threading.Lock()
        self._worker = None
        self.stats = {"received": 0, "delivered": 0, "errors": 0}
        ensure_dirs(support_dir)
        self._setup_logging()

    # ----- logging / events -----
    def _setup_logging(self):
        self.log = logging.getLogger("fmtcpbridge")
        if not self.log.handlers:
            self.log.setLevel(logging.INFO)
            h = logging.handlers.RotatingFileHandler(
                os.path.join(LOG_DIR, "bridge.log"), maxBytes=5_000_000, backupCount=5)
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self.log.addHandler(h)

    def emit(self, kind, text, detail=None, **extra):
        ev = {"kind": kind, "time": _dt.datetime.now(), "text": text, "detail": detail}
        ev.update(extra)
        level = logging.ERROR if kind == "error" else logging.INFO
        self.log.log(level, "[%s] %s%s", kind, text, f"\n{detail}" if detail else "")
        try:
            self.on_event(ev)
        except Exception:
            pass

    # ----- lifecycle -----
    @property
    def running(self):
        return self._server is not None

    def reload_config(self):
        self.cfg = load_config(self.config_path)
        return self.cfg

    def start(self):
        if self.running:
            return
        self.reload_config()
        self._stop.clear()
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._delivery_loop, name="delivery", daemon=True)
            self._worker.start()
        srv = _Server((self.cfg["listen_host"], int(self.cfg["listen_port"])), _Handler, bind_and_activate=False)
        srv.engine = self
        srv.server_bind()
        srv.server_activate()
        self._server = srv
        self._server_thread = threading.Thread(target=srv.serve_forever, name="listener", daemon=True)
        self._server_thread.start()
        probs = config_problems(self.cfg)
        self.emit("info", f"Listening on {self.cfg['listen_host']}:{self.cfg['listen_port']}")
        if probs:
            self.emit("error", "FileMaker settings incomplete - messages will queue until fixed: "
                      + "; ".join(probs))
        self._wake.set()

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self.emit("info", "Listener stopped")

    def shutdown(self):
        self.stop()
        self._stop.set()
        self._wake.set()

    def retry_now(self):
        self._wake.set()

    def pending_count(self):
        try:
            return len([f for f in os.listdir(self.queue_dir) if f.endswith(".json")])
        except OSError:
            return 0

    # ----- TCP side -----
    def _handle_client(self, sock, addr):
        cfg = self.cfg
        peer = f"{addr[0]}:{addr[1]}"
        allowed = cfg.get("allowed_clients") or []
        if allowed and addr[0] not in allowed:
            self.emit("error", f"Rejected connection from {peer} (not in allowed_clients)")
            return

        data = self._read_message(sock, cfg)
        if not data:
            self.emit("info", f"{peer} connected and closed without sending data")
            return

        text, enc = self._decode(data, cfg)
        job = {
            "id": _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:6],
            "receivedAt": _now_iso(),
            "remoteAddr": addr[0],
            "remotePort": addr[1],
            "localPort": int(cfg["listen_port"]),
            "encoding": enc,
            "bytes": len(data),
            "data": text,
            "attempts": 0,
            "next_attempt": 0,
        }
        self.stats["received"] += 1
        self.emit("in", f"{peer}  {len(data)} bytes  [{job['id']}]", text, job_id=job["id"])

        waiter = None
        if cfg["reply_mode"] == "result":
            waiter = (threading.Event(), {})
            with self._waiters_lock:
                self._waiters[job["id"]] = waiter
        self._spool(job)
        self._wake.set()

        reply = None
        if cfg["reply_mode"] == "ack":
            reply = cfg["ack_text"]
        elif waiter:
            if waiter[0].wait(float(cfg["reply_wait_sec"])):
                r = waiter[1]
                reply = (r.get("result") or "") if r.get("ok") else "ERROR " + (r.get("error") or "")
            else:
                reply = "QUEUED " + job["id"]
            with self._waiters_lock:
                self._waiters.pop(job["id"], None)
            if reply and not reply.endswith(("\n", "\r")):
                reply += cfg.get("reply_suffix", "")
        if reply:
            try:
                sock.sendall(reply.encode(cfg["text_encoding"], "replace"))
                self.emit("reply", f"to {peer}  [{job['id']}]", reply)
            except OSError as e:
                self.emit("error", f"Could not reply to {peer}: {e}")

    def _read_message(self, sock, cfg):
        chunks, total = [], 0
        deadline = time.monotonic() + float(cfg["connection_timeout_sec"])
        idle = float(cfg["message_idle_ms"]) / 1000.0
        limit = int(cfg["max_message_bytes"])
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # before any data arrives, wait up to the full timeout; after, only the idle gap
            sock.settimeout(min(remaining, idle) if chunks else remaining)
            try:
                buf = sock.recv(65536)
            except socket.timeout:
                break
            except OSError:
                break
            if not buf:
                break  # client closed
            chunks.append(buf)
            total += len(buf)
            if total >= limit:
                self.emit("error", f"Message hit max_message_bytes ({limit}); truncated")
                break
        return b"".join(chunks)[:limit]

    def _decode(self, data, cfg):
        try:
            text = data.decode(cfg["text_encoding"])
            if cfg.get("strip_whitespace"):
                text = text.strip()
            return text, cfg["text_encoding"]
        except (UnicodeDecodeError, LookupError):
            return base64.b64encode(data).decode("ascii"), "base64"

    # ----- spool -----
    def _job_path(self, job_id, folder=None):
        return os.path.join(folder or self.queue_dir, job_id + ".json")

    def _spool(self, job):
        path = self._job_path(job["id"])
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(job, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _build_parameter(self, job):
        if self.cfg["parameter_mode"] == "raw":
            return job["data"]
        env = {k: job[k] for k in ("id", "receivedAt", "remoteAddr", "remotePort",
                                    "localPort", "encoding", "bytes", "data")}
        return json.dumps(env, ensure_ascii=False)

    # ----- delivery -----
    def _delivery_loop(self):
        while not self._stop.is_set():
            self._wake.wait(timeout=5)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.deliver_pending()
            except Exception as e:
                self.emit("error", f"Delivery loop error: {e}")

    def deliver_pending(self):
        with self._deliver_lock:
            cfg = self.cfg or self.reload_config()
            files = sorted(f for f in os.listdir(self.queue_dir) if f.endswith(".json"))
            for name in files:
                path = os.path.join(self.queue_dir, name)
                try:
                    with open(path) as f:
                        job = json.load(f)
                except (OSError, ValueError) as e:
                    self.emit("error", f"Unreadable queue file {name}: {e}")
                    os.replace(path, os.path.join(self.failed_dir, name))
                    continue
                if job.get("next_attempt", 0) > time.time():
                    continue
                self._deliver(job, path, cfg)

    def _deliver(self, job, path, cfg):
        probs = config_problems(cfg)
        password = get_password(cfg["fm_username"]) if not probs else ""
        if not probs and not password:
            probs = ["no FileMaker password saved in the Keychain"]
        job["attempts"] = job.get("attempts", 0) + 1
        if probs:
            self._fail(job, path, cfg, "; ".join(probs), None)
            return

        param = self._build_parameter(job)
        url = odata_script_url(cfg)
        self.emit("out", f"POST {url}  [{job['id']}] attempt {job['attempts']}",
                  json.dumps({"scriptParameterValue": param}, ensure_ascii=False, indent=2),
                  job_id=job["id"])
        try:
            status, code, result, raw = call_filemaker(cfg, password, param)
        except Exception as e:
            self._fail(job, path, cfg, f"{type(e).__name__}: {e}", None)
            return

        if 200 <= status < 300:
            os.remove(path)
            self.stats["delivered"] += 1
            code_txt = f"script code {code}" if code is not None else "no script code"
            self.emit("fm", f"HTTP {status}  {code_txt}  [{job['id']}]", raw, job_id=job["id"],
                      ok=True)
            if code not in (None, 0, "0"):
                self.emit("error", f"FileMaker script returned error {code} for {job['id']}")
            self._notify(job["id"], ok=True, result="" if result is None else str(result))
        else:
            self.emit("fm", f"HTTP {status}  [{job['id']}]", raw, job_id=job["id"], ok=False)
            self._fail(job, path, cfg, f"HTTP {status}" + (f": {result}" if result else ""), status)

    def _fail(self, job, path, cfg, reason, status):
        self.stats["errors"] += 1
        job["last_error"] = reason
        maxa = int(cfg.get("max_attempts") or 0)
        if maxa and job["attempts"] >= maxa:
            with open(self._job_path(job["id"], self.failed_dir), "w") as f:
                json.dump(job, f)
            os.remove(path)
            self.emit("error", f"Gave up on {job['id']} after {job['attempts']} attempts: {reason}")
        else:
            job["next_attempt"] = time.time() + float(cfg["retry_interval_sec"])
            self._spool(job)
            self.emit("error", f"Delivery failed for {job['id']} ({reason}); "
                      f"retrying in {cfg['retry_interval_sec']}s")
        self._notify(job["id"], ok=False, error=reason)

    def _notify(self, job_id, **result):
        with self._waiters_lock:
            w = self._waiters.get(job_id)
        if w:
            w[1].update(result)
            w[0].set()

    # ----- diagnostics -----
    def test_connection(self):
        """Run the script once with a test parameter; doesn't touch the queue."""
        cfg = self.reload_config()
        probs = config_problems(cfg)
        if probs:
            self.emit("error", "Test: " + "; ".join(probs))
            return False
        password = get_password(cfg["fm_username"])
        if not password:
            self.emit("error", "Test: no FileMaker password saved in the Keychain")
            return False
        param = json.dumps({"test": True, "receivedAt": _now_iso(), "data": ""})
        self.emit("out", f"POST {odata_script_url(cfg)}  [test]",
                  json.dumps({"scriptParameterValue": param}, indent=2))
        try:
            status, code, result, raw = call_filemaker(cfg, password, param)
        except Exception as e:
            self.emit("error", f"Test failed: {type(e).__name__}: {e}")
            return False
        ok = 200 <= status < 300
        self.emit("fm", f"HTTP {status}  [test]", raw, ok=ok)
        if not ok:
            self.emit("error", f"Test failed: HTTP {status}" + (f": {result}" if result else ""))
        return ok


def _main():
    import argparse
    ap = argparse.ArgumentParser(description=f"{APP_NAME} (headless)")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--support-dir", default=None)
    args = ap.parse_args()
    support = args.support_dir or os.path.dirname(os.path.abspath(args.config))

    def show(ev):
        print(f"{ev['time']:%H:%M:%S} {ev['kind'].upper():5} {ev['text']}", flush=True)
        if ev.get("detail"):
            print("      " + ev["detail"].replace("\n", "\n      "), flush=True)

    eng = Engine(config_path=args.config, support_dir=support, on_event=show)
    eng.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        eng.shutdown()


if __name__ == "__main__":
    _main()
