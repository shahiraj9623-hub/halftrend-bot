import os, json, re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

KEY = os.environ["TWELVE_API_KEY"].strip()
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
ADMIN_ID = 8933066024          # only this Telegram user can send commands

OFFSET = 0.0835                # Minor level offset (factor 0.167 / 2)
MAX_LEVELS = 20
SOFT_LIMIT = 700               # after this many Twelve Data calls/day, slow down
HARD_LIMIT = 780               # after this, stop price checks until next UTC day
SLOW_GAP_MIN = 14              # minutes between price checks in slow mode
MORNING_MAX_TRIES = 5
STATE_FILE = "levels_state.json"

UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")
TG = f"https://api.telegram.org/bot{TOKEN}"
CMD = re.compile(
    r"^/?(add|remove|list|clear)(?:@\w+)?(?:\s+([\d,]+(?:\.\d+)?))?\s*$", re.I)


def tg_send(text):
    try:
        r = requests.post(f"{TG}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": text}, timeout=15)
        print("Telegram:", r.status_code)
        return r.ok
    except Exception as e:
        print("Telegram exception:", e)
        return False


def load_state():
    st = {"offset": None, "webhook_cleared": False, "levels": [],
          "morning_done": "", "morning_tries": {"date": "", "n": 0},
          "calls": {"date": "", "n": 0}, "last_call": "", "limit_notice": ""}
    if os.path.exists(STATE_FILE):
        try:
            st.update(json.load(open(STATE_FILE)))
        except Exception:
            pass
    return st


def save_state(st):
    json.dump(st, open(STATE_FILE, "w"))


def today_calls(st, now):
    d = now.strftime("%Y-%m-%d")
    if st["calls"].get("date") != d:
        st["calls"] = {"date": d, "n": 0}
    return st["calls"]["n"]


def td_get(st, now, params):
    """One Twelve Data request. Every request is counted."""
    n = today_calls(st, now)
    st["calls"]["n"] = n + 1
    st["last_call"] = now.isoformat()
    p = {"symbol": "XAU/USD", "interval": "1min", "timezone": "UTC", "apikey": KEY}
    p.update(params)
    r = requests.get("https://api.twelvedata.com/time_series", params=p, timeout=30)
    d = r.json()
    if d.get("status") == "error":
        print("Twelve Data error:", d.get("code"), d.get("message"))
        return None
    bars = []
    for v in d.get("values", []):
        bars.append({
            "dt": datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC),
            "open": float(v["open"]), "high": float(v["high"]),
            "low": float(v["low"]), "close": float(v["close"])})
    bars.sort(key=lambda b: b["dt"])
    return bars


def anchor_utc(now):
    a = now.astimezone(IST).replace(hour=3, minute=33, second=0, microsecond=0)
    if now.astimezone(IST) < a:
        a -= timedelta(days=1)
    return a.astimezone(UTC)


# ---------------- Telegram commands ----------------
def handle_commands(st, now):
    if not st["webhook_cleared"]:
        r = requests.get(f"{TG}/deleteWebhook", timeout=15)
        print("deleteWebhook:", r.status_code)
        if r.ok:
            st["webhook_cleared"] = True

    params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if st["offset"] is not None:
        params["offset"] = st["offset"]
    r = requests.get(f"{TG}/getUpdates", params=params, timeout=20)
    if r.status_code == 409:
        st["webhook_cleared"] = False
        print("getUpdates conflict (webhook), will retry next run")
        return
    data = r.json()
    if not data.get("ok"):
        print("getUpdates error:", data.get("description"))
        return

    first_run = st["offset"] is None
    for u in data.get("result", []):
        st["offset"] = u["update_id"] + 1
        if first_run:
            continue                     # ignore old messages on the very first run
        m = u.get("message")
        if not m:
            continue
        if m.get("from", {}).get("id") != ADMIN_ID:
            continue
        if str(m.get("chat", {}).get("id")) != CHAT_ID:
            continue
        mt = CMD.match((m.get("text") or "").strip())
        if not mt:
            continue
        cmd, num = mt.group(1).lower(), mt.group(2)
        levels = st["levels"]

        if cmd in ("add", "remove"):
            if not num:
                tg_send(f"Usage: {cmd.capitalize()} 4096")
                continue
            p = float(num.replace(",", ""))
            if not (1000 <= p <= 20000):
                tg_send(f"Invalid price: {p}")
                continue
            if cmd == "add":
                if any(abs(l["price"] - p) < 0.005 for l in levels):
                    tg_send(f"Level {p:.2f} is already active.")
                elif len(levels) >= MAX_LEVELS:
                    tg_send(f"Maximum {MAX_LEVELS} levels reached.")
                else:
                    levels.append({"price": p, "added": now.isoformat()})
                    tg_send(f"Level added: {p:.2f}. Active levels: {len(levels)}")
            else:
                before = len(levels)
                st["levels"] = [l for l in levels if abs(l["price"] - p) >= 0.005]
                if len(st["levels"]) < before:
                    tg_send(f"Level removed: {p:.2f}. Active levels: {len(st['levels'])}")
                else:
                    tg_send(f"Level {p:.2f} not found.")
        elif cmd == "list":
            if levels:
                tg_send("Active levels: " + ", ".join(
                    f"{l['price']:.2f}" for l in sorted(levels, key=lambda x: x["price"])))
            else:
                tg_send("No active levels.")
        elif cmd == "clear":
            st["levels"] = []
            tg_send("All levels cleared.")

    if first_run:
        if st["offset"] is None:
            st["offset"] = 0
        tg_send("Levels bot is ready. Commands: Add 4096, Remove 4096, List, Clear")


# ---------------- Morning Minor levels (once a day) ----------------
def morning(st, now):
    anchor = anchor_utc(now)
    key = anchor.astimezone(IST).strftime("%Y-%m-%d")
    if st["morning_done"] == key:
        return
    if now < anchor + timedelta(minutes=5):
        return
    tries = st["morning_tries"]
    if tries.get("date") != key:
        tries = {"date": key, "n": 0}
        st["morning_tries"] = tries
    if tries["n"] >= MORNING_MAX_TRIES:
        return
    tries["n"] += 1

    bars = td_get(st, now, {
        "start_date": anchor.strftime("%Y-%m-%d %H:%M:%S"),
        "end_date": (anchor + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"),
        "outputsize": 5})
    if not bars:
        print("No 3:33 AM IST candle yet (market closed or data missing)")
        return
    bar = next((b for b in bars if b["dt"] == anchor), None)
    if bar is None:
        print("3:33 AM IST candle not found")
        return
    root = bar["open"] ** 0.5
    res = (root + OFFSET) ** 2
    sup = (root - OFFSET) ** 2
    ok = tg_send(f"XAUUSD levels (open {bar['open']:.2f} at 03:33 AM IST)\n"
                 f"Minor Support: {sup:.2f}\nMinor Resistance: {res:.2f}")
    if ok:
        st["morning_done"] = key


# ---------------- Price level alerts ----------------
def check_levels(st, now):
    levels = st["levels"]
    if not levels:
        return
    n = today_calls(st, now)
    day = now.strftime("%Y-%m-%d")

    if n >= HARD_LIMIT:
        if st["limit_notice"] != day + "-hard":
            tg_send(f"Twelve Data daily limit reached ({n} calls). "
                    "Level checks are paused until the next UTC day.")
            st["limit_notice"] = day + "-hard"
        return
    if n >= SOFT_LIMIT:
        if st["limit_notice"] not in (day, day + "-hard"):
            tg_send(f"Twelve Data usage is high ({n} calls today). "
                    f"Level checks are slowed to every {SLOW_GAP_MIN} minutes.")
            st["limit_notice"] = day
        if st["last_call"]:
            last = datetime.fromisoformat(st["last_call"])
            if now - last < timedelta(minutes=SLOW_GAP_MIN):
                return

    bars = td_get(st, now, {"outputsize": 15})
    if not bars:
        return
    price = bars[-1]["close"]

    remaining = []
    for lv in levels:
        p = lv["price"]
        added = datetime.fromisoformat(lv["added"])
        start = added.replace(second=0, microsecond=0) + timedelta(minutes=1)
        hit = any(b["dt"] >= start and b["low"] <= p <= b["high"] for b in bars)
        if hit:
            sent = tg_send(f"XAUUSD level {p:.2f} hit. Price now {price:.2f}. "
                           f"Time: {now.astimezone(IST):%I:%M %p} IST")
            if not sent:
                remaining.append(lv)      # retry next run
        else:
            remaining.append(lv)
    st["levels"] = remaining


def main():
    st = load_state()
    now = datetime.now(UTC)
    for name, fn in (("commands", handle_commands),
                     ("morning", morning),
                     ("levels", check_levels)):
        try:
            fn(st, now)
        except Exception as e:
            print(name, "error:", repr(e))
    save_state(st)


main()
