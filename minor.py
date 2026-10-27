import os, json, math
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KEY = os.environ["TWELVE_API_KEY"].strip()
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

FACTOR = -0.167          # Minor Support Barrier
TF_MIN = 3
MAX_AGE_MIN = 15
STATE_FILE = "minor_state.json"
UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")


def send(msg):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg}, timeout=15)
        print("Telegram:", r.status_code)
        return r.ok
    except Exception as e:
        print("Telegram exception:", e)
        return False


def anchor_utc(now):
    a = now.astimezone(IST).replace(hour=3, minute=33, second=0, microsecond=0)
    if now.astimezone(IST) < a:
        a -= timedelta(days=1)
    return a.astimezone(UTC)


def fetch():
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": "XAU/USD", "interval": "1min", "outputsize": 2000,
                "timezone": "UTC", "apikey": KEY}, timeout=30)
    d = r.json()
    if d.get("status") == "error":
        print("Twelve Data error:", d.get("code"), d.get("message"))
        return None
    df = pd.DataFrame(d["values"])
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c])
    return df.set_index("datetime").sort_index()


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {}


def main():
    now = datetime.now(UTC)
    anchor = anchor_utc(now)
    df = fetch()
    if df is None or df.empty:
        print("No data")
        return

    ts_anchor = pd.Timestamp(anchor)
    if ts_anchor not in df.index:
        print("No 3:33 AM IST candle found for", anchor, "- market closed or data missing")
        return

    open_px = float(df.loc[ts_anchor, "open"])
    level = (math.sqrt(open_px) + FACTOR / 2) ** 2
    print(f"Anchor {anchor:%Y-%m-%d %H:%M} UTC | open {open_px:.2f} | level {level:.2f}")

    day = df[df.index >= ts_anchor]
    bars = day.resample(f"{TF_MIN}min", label="left", closed="left").agg(
        {"open": "first", "close": "last"}).dropna()
    last_1m = df.index[-1]
    now_floor = pd.Timestamp(now).floor("min")
    limit = min(last_1m + pd.Timedelta(minutes=1), now_floor)
    bars = bars[bars.index + pd.Timedelta(minutes=TF_MIN) <= limit]
    if len(bars) < 2:
        print("Not enough closed bars yet")
        return

    st = load_state()
    last_done = pd.Timestamp(st["last_done"]) if st.get("last_done") else None
    closes = bars["close"]

    for i in range(1, len(bars)):
        ts = bars.index[i]
        if last_done is not None and ts <= last_done:
            continue
        prev_c, c = float(closes.iloc[i - 1]), float(closes.iloc[i])
        end = ts + pd.Timedelta(minutes=TF_MIN)
        age = (now - end.to_pydatetime()).total_seconds() / 60
        side = None
        if prev_c <= level < c:
            side = "ABOVE"
        elif prev_c >= level > c:
            side = "BELOW"
        if side and age <= MAX_AGE_MIN:
            msg = (f"XAUUSD 3m: close {c:.2f} crossed {side} level {level:.2f}\n"
                   f"Bar close: {end.astimezone(IST):%d-%b %H:%M} IST")
            if not send(msg):
                print("Telegram FAILED, will retry next run")
                break
            print("Alert sent:", ts)
        st["last_done"] = str(ts)

    json.dump(st, open(STATE_FILE, "w"))


main()
