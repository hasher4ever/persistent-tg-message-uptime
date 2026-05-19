import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Singletons shared across all targets
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "")
CHAT_ID       = os.environ.get("CHAT_ID", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
PORT          = int(os.environ.get("PORT", "3000"))
STATE_FILE    = os.environ.get("STATE_FILE", "")
BAR_LEN       = int(os.environ.get("BAR_LEN", "14"))

# Comma-lists aligned by index. Pass a single value if all targets share it
# (e.g. one Kuma serving 3 status pages → KUMA_URL stays a scalar, STATUS_SLUG
# is a 3-item list); pass full lists for fully-separate Kuma instances.
KUMA_URLS    = [s.strip().rstrip("/") for s in os.environ.get("KUMA_URL", "").split(",") if s.strip()]
STATUS_SLUGS = [s.strip() for s in os.environ.get("STATUS_SLUG", "").split(",") if s.strip()]
THREAD_IDS   = [s.strip() for s in os.environ.get("MESSAGE_THREAD_ID", "").split(",")]
TITLES       = [s.strip() for s in os.environ.get("TITLE", "Status").split(",")]

# Auto-pad shorter lists: KUMA_URL repeats the first value, others use sensible defaults
def _pad(lst, target_len, fill):
    while len(lst) < target_len:
        lst.append(fill)
n = len(STATUS_SLUGS)
if KUMA_URLS:
    _pad(KUMA_URLS, n, KUMA_URLS[0])
_pad(THREAD_IDS, n, "")
_pad(TITLES, n, "Status")

BEAT_GLYPH = {1: "🟢", 0: "🔴", 2: "🟡", 3: "🔵"}
BEAT_BLANK = "⚪"
NBSP = " "

for k, v in (("BOT_TOKEN", BOT_TOKEN), ("CHAT_ID", CHAT_ID)):
    if not v:
        print(f"[fatal] missing env var: {k}", file=sys.stderr, flush=True)
        sys.exit(1)
if not STATUS_SLUGS:
    print("[fatal] STATUS_SLUG required (comma-separated for multiple targets)",
          file=sys.stderr, flush=True)
    sys.exit(1)
if not KUMA_URLS:
    print("[fatal] KUMA_URL required (single value or comma-list aligned with STATUS_SLUG)",
          file=sys.stderr, flush=True)
    sys.exit(1)


class Target:
    """One Kuma status page → one Telegram chat/thread pairing."""

    def __init__(self, kuma_url, slug, thread_id, title):
        self.kuma_url = kuma_url
        self.slug = slug
        self.thread_id = thread_id
        self.title = title
        self.message_id = None
        self.last_states = {}
        self.pending_alerts = []
        self.last_tick = {"at": 0, "ok": False, "action": None, "error": None}
        self.lock = threading.Lock()

    @property
    def tag(self):
        return self.title or self.slug


TARGETS = [
    Target(k, s, t, ti)
    for k, s, t, ti in zip(KUMA_URLS, STATUS_SLUGS, THREAD_IDS, TITLES)
]


def load_all_state():
    if not STATE_FILE:
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_all_state(d):
    if not STATE_FILE:
        return
    try:
        parent = os.path.dirname(STATE_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(d, f)
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


def _send_kwargs(target, text, **extra):
    # disable_notification=True on every send — Kuma's native notifier
    # handles DOWN/UP buzzes, so this bot stays a silent pinned-board.
    kw = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown",
          "disable_notification": True, **extra}
    if target.thread_id:
        kw["message_thread_id"] = int(target.thread_id)
    return kw


def clean_name(name):
    # Strip a leading "[TAG] " prefix — redundant when each thread shows
    # only one env. Kuma keeps the full name; this is display-only.
    if name.startswith("[") and "] " in name:
        return name.split("] ", 1)[1]
    return name


def fetch_state(kuma_url, slug):
    page = http_get_json(f"{kuma_url}/api/status-page/{slug}")
    beat = http_get_json(f"{kuma_url}/api/status-page/heartbeat/{slug}")
    out = []
    for group in page.get("publicGroupList", []):
        for m in group.get("monitorList", []):
            mid = str(m["id"])
            beats = beat.get("heartbeatList", {}).get(mid, [])
            history = [b.get("status") for b in beats[-BAR_LEN:]]
            status = history[-1] if history else None
            uptime = beat.get("uptimeList", {}).get(f"{mid}_24", 0) * 100
            out.append({
                "name": clean_name(m["name"]),
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
    if s == 0:
        return (0, m["name"])
    if s != 1:
        return (1, m["name"])
    if is_flaky(m):
        return (2, m["name"])
    return (3, m["name"])


def render(monitors, title):
    down = [m for m in monitors if m["status"] == 0]
    pending = [m for m in monitors if m["status"] in (None, 2)]
    flaky = [m for m in monitors if is_flaky(m)]
    up_count = sum(1 for m in monitors if m["status"] == 1)
    total = len(monitors)
    stamp = time.strftime("%H:%M", time.gmtime())

    if down:
        names = ", ".join(m["name"] for m in down[:2])
        more = f" +{len(down) - 2}" if len(down) > 2 else ""
        header = f"🔴 DOWN · *{names}*{more} · {title} · {stamp} UTC"
    elif pending:
        names = ", ".join(m["name"] for m in pending[:2])
        more = f" +{len(pending) - 2}" if len(pending) > 2 else ""
        header = f"🟡 PEND · *{names}*{more} · {title} · {stamp} UTC"
    elif flaky:
        names = ", ".join(m["name"] for m in flaky[:2])
        more = f" +{len(flaky) - 2}" if len(flaky) > 2 else ""
        header = f"🟡 FLAKY · *{names}*{more} · {title} · {stamp} UTC"
    else:
        header = f"🟢 *{title}* · {up_count}/{total} up · {stamp} UTC"

    lines = [header, "─────────────────────"]
    if not monitors:
        lines.append("_no monitors found on status page_")
        return "\n".join(lines)
    name_w = max(len(m["name"]) for m in monitors)
    for m in sorted(monitors, key=sort_key):
        pct = f"{m['uptime']:5.1f}%"
        if pct.startswith(" "):
            pct = NBSP + pct[1:]
        row = f"{pct} {m['name']:<{name_w}} {bar(m['history'])}"
        lines.append(f"`{row}`")
    return "\n".join(lines)


def detect_transitions(monitors, previous):
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


def cleanup_expired_alerts(target):
    for mid, _ in target.pending_alerts:
        delete_message(mid)
    target.pending_alerts = []


def send_alert(target, text):
    try:
        r = tg("sendMessage", **_send_kwargs(target, text))
        target.pending_alerts.append((r["result"]["message_id"], time.time()))
    except Exception as e:
        print(f"[alert·{target.tag}] {e}", file=sys.stderr, flush=True)


def do_tick(target):
    cleanup_expired_alerts(target)
    try:
        monitors = fetch_state(target.kuma_url, target.slug)
        text = render(monitors, target.title)

        if target.last_states:
            newly_down, newly_up = detect_transitions(monitors, target.last_states)
            for name in newly_down:
                send_alert(target, f"🔴 DOWN · *{name}* · {target.title}")
            for name in newly_up:
                send_alert(target, f"🟢 UP · *{name}* recovered · {target.title}")
        target.last_states = {m["name"]: m["status"] for m in monitors}

        if target.message_id is not None:
            try:
                tg("editMessageText",
                   chat_id=CHAT_ID, message_id=target.message_id,
                   text=text, parse_mode="Markdown")
                with target.lock:
                    target.last_tick = {"at": int(time.time() * 1000), "ok": True,
                                        "action": "edited", "error": None}
                return
            except Exception:
                pass

        r = tg("sendMessage", **_send_kwargs(target, text, disable_notification=True))
        target.message_id = r["result"]["message_id"]
        state = load_all_state()
        state[target.slug] = {"message_id": target.message_id}
        save_all_state(state)
        with target.lock:
            target.last_tick = {"at": int(time.time() * 1000), "ok": True,
                                "action": "created", "error": None}
    except Exception as e:
        with target.lock:
            target.last_tick = {"at": int(time.time() * 1000), "ok": False,
                                "action": None, "error": str(e)}
        print(f"[tick·{target.tag}] {e}", file=sys.stderr, flush=True)


def tick_loop():
    while True:
        for t in TARGETS:
            do_tick(t)
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
            now_ms = time.time() * 1000
            per_target = {}
            any_unhealthy = False
            for t in TARGETS:
                with t.lock:
                    snap = dict(t.last_tick)
                stale = (now_ms - snap["at"]) > POLL_INTERVAL * 1000 * 3
                healthy = snap["ok"] and not stale
                if not healthy:
                    any_unhealthy = True
                per_target[t.slug] = {
                    "title": t.title,
                    "healthy": healthy,
                    "lastTick": snap,
                    "messageId": t.message_id,
                }
            body = json.dumps({
                "status": "degraded" if any_unhealthy else "ok",
                "pollIntervalMs": POLL_INTERVAL * 1000,
                "targets": per_target,
            })
            self._respond(503 if any_unhealthy else 200, body, "application/json")
        else:
            self._respond(404, "not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/tick":
            for t in TARGETS:
                do_tick(t)
            snap = {t.slug: t.last_tick for t in TARGETS}
            self._respond(200, json.dumps(snap), "application/json")
        else:
            self._respond(404, "not found")


def main():
    state = load_all_state()
    for t in TARGETS:
        t.message_id = state.get(t.slug, {}).get("message_id")
        if t.message_id:
            print(f"loaded message_id={t.message_id} for {t.tag}", flush=True)
    threading.Thread(target=tick_loop, daemon=True).start()
    titles = ", ".join(t.tag for t in TARGETS)
    print(f"status-bot listening on :{PORT}, {len(TARGETS)} target(s) [{titles}], "
          f"ticking every {POLL_INTERVAL}s", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
