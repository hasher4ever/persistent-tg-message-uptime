import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KUMA_URL           = os.environ.get("KUMA_URL", "").rstrip("/")
STATUS_SLUG        = os.environ.get("STATUS_SLUG", "")
BOT_TOKEN          = os.environ.get("BOT_TOKEN", "")
CHAT_ID            = os.environ.get("CHAT_ID", "")
MESSAGE_THREAD_ID  = os.environ.get("MESSAGE_THREAD_ID", "").strip()
POLL_INTERVAL      = int(os.environ.get("POLL_INTERVAL", "60"))
TITLE              = os.environ.get("TITLE", "Status")
PORT               = int(os.environ.get("PORT", "3000"))
STATE_FILE         = os.environ.get("STATE_FILE", "")
BAR_LEN            = int(os.environ.get("BAR_LEN", "14"))

BEAT_GLYPH = {1: "'", 0: ".", 2: "-", 3: "~"}
STATUS_EMOJI = {1: "🟢", 0: "🔴", 2: "🟡", 3: "🔵"}
BEAT_BLANK = " "
NBSP = " "

for k, v in (("KUMA_URL", KUMA_URL), ("STATUS_SLUG", STATUS_SLUG),
             ("BOT_TOKEN", BOT_TOKEN), ("CHAT_ID", CHAT_ID)):
    if not v:
        print(f"[fatal] missing env var: {k}", file=sys.stderr, flush=True)
        sys.exit(1)

state_lock = threading.Lock()
last_tick = {"at": 0, "ok": False, "action": None, "error": None}
message_id: int | None = None
last_states: dict[str, int | None] = {}
pending_alerts: list[tuple[int, float]] = []


def load_message_id():
    if not STATE_FILE:
        return None
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("message_id")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_message_id(mid):
    if not STATE_FILE:
        return
    try:
        d = os.path.dirname(STATE_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({"message_id": mid}, f)
    except OSError as e:
        print(f"[warn] cannot persist state: {e}", file=sys.stderr, flush=True)


def http_get_json(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def tg(method, **params):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(params).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        j = json.loads(r.read())
    if not j.get("ok"):
        raise RuntimeError(f"{method}: {j.get('description', 'unknown error')}")
    return j


def fetch_state():
    page = http_get_json(f"{KUMA_URL}/api/status-page/{STATUS_SLUG}")
    beat = http_get_json(f"{KUMA_URL}/api/status-page/heartbeat/{STATUS_SLUG}")
    out = []
    for group in page.get("publicGroupList", []):
        for m in group.get("monitorList", []):
            mid = str(m["id"])
            beats = beat.get("heartbeatList", {}).get(mid, [])
            history = [b.get("status") for b in beats[-BAR_LEN:]]
            status = history[-1] if history else None
            uptime = beat.get("uptimeList", {}).get(f"{mid}_24", 0) * 100
            out.append({
                "name": m["name"],
                "status": status,
                "uptime": uptime,
                "history": history,
            })
    return out


def bar(history):
    cells = [BEAT_GLYPH.get(s, BEAT_BLANK) for s in history]
    pad = [BEAT_BLANK] * max(0, BAR_LEN - len(cells))
    return "".join(pad + cells)


def is_flaky(m):
    return m["status"] == 1 and any(b in (0, 2) for b in m["history"])


def sort_key(m):
    s = m["status"]
    # down → pending/unknown → flaky → clean up
    if s == 0:
        return (0, m["name"])
    if s != 1:
        return (1, m["name"])
    if is_flaky(m):
        return (2, m["name"])
    return (3, m["name"])


def render(monitors):
    down = [m for m in monitors if m["status"] == 0]
    pending = [m for m in monitors if m["status"] in (None, 2)]
    flaky = [m for m in monitors if is_flaky(m)]
    up_count = sum(1 for m in monitors if m["status"] == 1)
    total = len(monitors)
    stamp = time.strftime("%H:%M", time.gmtime())

    if down:
        names = ", ".join(m["name"] for m in down[:2])
        more = f" +{len(down) - 2}" if len(down) > 2 else ""
        header = f"🔴 DOWN · *{names}*{more} · {TITLE} · {stamp} UTC"
    elif pending:
        names = ", ".join(m["name"] for m in pending[:2])
        more = f" +{len(pending) - 2}" if len(pending) > 2 else ""
        header = f"🟡 PEND · *{names}*{more} · {TITLE} · {stamp} UTC"
    elif flaky:
        names = ", ".join(m["name"] for m in flaky[:2])
        more = f" +{len(flaky) - 2}" if len(flaky) > 2 else ""
        header = f"🟡 FLAKY · *{names}*{more} · {TITLE} · {stamp} UTC"
    else:
        header = f"🟢 *{TITLE}* · {up_count}/{total} up · {stamp} UTC"

    lines = [header, "─────────────────────"]
    if not monitors:
        lines.append("_no monitors found on status page_")
        return "\n".join(lines)
    name_w = max(len(m["name"]) for m in monitors)
    for m in sorted(monitors, key=sort_key):
        pct = f"{m['uptime']:5.1f}%"
        if pct.startswith(" "):
            pct = NBSP + pct[1:]
        dot = STATUS_EMOJI.get(m["status"], "⚪")
        row = f"{pct} {m['name']:<{name_w}} {bar(m['history'])}"
        lines.append(f"{dot} `{row}`")
    return "\n".join(lines)


def detect_transitions(monitors, previous):
    """Return (newly_down, newly_up) lists of service names."""
    newly_down, newly_up = [], []
    for m in monitors:
        prev = previous.get(m["name"])
        curr = m["status"]
        if curr == 0 and prev == 1:
            newly_down.append(m["name"])
        elif curr == 1 and prev == 0:
            newly_up.append(m["name"])
    return newly_down, newly_up


def delete_message(mid):
    try:
        tg("deleteMessage", chat_id=CHAT_ID, message_id=mid)
    except Exception:
        pass


def cleanup_expired_alerts():
    """Delete every prior alert so only the main status message remains."""
    global pending_alerts
    for mid, _ in pending_alerts:
        delete_message(mid)
    pending_alerts = []


def _send_kwargs(text, **extra):
    kw = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown", **extra}
    if MESSAGE_THREAD_ID:
        kw["message_thread_id"] = int(MESSAGE_THREAD_ID)
    return kw


def send_alert(text):
    """Loud, audible message — for state transitions. Auto-deleted after ALERT_TTL."""
    try:
        r = tg("sendMessage", **_send_kwargs(text))
        pending_alerts.append((r["result"]["message_id"], time.time()))
    except Exception as e:
        print(f"[alert] {e}", file=sys.stderr, flush=True)


def do_tick():
    global last_tick, message_id, last_states
    cleanup_expired_alerts()
    try:
        monitors = fetch_state()
        text = render(monitors)

        if last_states:
            newly_down, newly_up = detect_transitions(monitors, last_states)
            for name in newly_down:
                send_alert(f"🔴 DOWN · *{name}* · {TITLE}")
            for name in newly_up:
                send_alert(f"🟢 UP · *{name}* recovered · {TITLE}")

        last_states = {m["name"]: m["status"] for m in monitors}

        if message_id is not None:
            try:
                tg("editMessageText",
                   chat_id=CHAT_ID, message_id=message_id,
                   text=text, parse_mode="Markdown")
                with state_lock:
                    last_tick = {"at": int(time.time() * 1000), "ok": True,
                                 "action": "edited", "error": None}
                return
            except Exception:
                pass
        r = tg("sendMessage", **_send_kwargs(text, disable_notification=True))
        message_id = r["result"]["message_id"]
        save_message_id(message_id)
        with state_lock:
            last_tick = {"at": int(time.time() * 1000), "ok": True,
                         "action": "created", "error": None}
    except Exception as e:
        with state_lock:
            last_tick = {"at": int(time.time() * 1000), "ok": False,
                         "action": None, "error": str(e)}
        print(f"[tick] {e}", file=sys.stderr, flush=True)


def tick_loop():
    while True:
        do_tick()
        time.sleep(POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _respond(self, status, body, ctype="text/plain"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._respond(200, "status-bot running")
        elif path == "/healthz":
            with state_lock:
                snap = dict(last_tick)
            stale = (time.time() * 1000 - snap["at"]) > POLL_INTERVAL * 1000 * 3
            healthy = snap["ok"] and not stale
            body = json.dumps({
                "status": "ok" if healthy else "degraded",
                "lastTick": snap,
                "pollIntervalMs": POLL_INTERVAL * 1000,
                "messageId": message_id,
            })
            self._respond(200 if healthy else 503, body, "application/json")
        else:
            self._respond(404, "not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/tick":
            do_tick()
            with state_lock:
                snap = dict(last_tick)
            self._respond(200, json.dumps(snap), "application/json")
        else:
            self._respond(404, "not found")


def main():
    global message_id
    message_id = load_message_id()
    if message_id:
        print(f"loaded message_id={message_id} from {STATE_FILE}", flush=True)
    threading.Thread(target=tick_loop, daemon=True).start()
    print(f"status-bot listening on :{PORT}, ticking every {POLL_INTERVAL}s",
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
