import os, json, requests
import numpy as np
import pandas as pd
import yfinance as yf
from zoneinfo import ZoneInfo
import datetime as dt

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MIN_PRICE = 5.0
MIN_PM_VOLUME = 50_000
MIN_PM_DOLLAR_VOL = 500_000
MIN_ABS_CHANGE = 2.0
EXCHANGES = ["NYSE", "NASDAQ", "AMEX"]
TOP_N = 25
AMPLITUDE = 5
TF_MIN = 3
MAX_AGE_MIN = 15
SUMMARY_EVERY_MIN = 28
STATE_FILE = "pm_state.json"
ET = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")

TV_URL = "https://scanner.tradingview.com/america/scan"
TV_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36",
    "Content-Type": "application/json",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}


def send(msg):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg}, timeout=15)
    print("Telegram:", r.status_code)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {"last_summary": "", "alerts": {}}


def save_state(st):
    json.dump(st, open(STATE_FILE, "w"))


def in_window(now_et):
    if now_et.weekday() >= 5:
        return False
    t = now_et.time()
    return dt.time(4, 0) <= t < dt.time(9, 30)


def tv_list():
    cols = ["name", "description", "close", "premarket_close",
            "premarket_change", "premarket_volume"]
    filters = [
        {"left": "exchange", "operation": "in_range", "right": EXCHANGES},
        {"left": "type", "operation": "equal", "right": "stock"},
        {"left": "typespecs", "operation": "has", "right": ["common"]},
        {"left": "close", "operation": "egreater", "right": MIN_PRICE},
        {"left": "premarket_volume", "operation": "egreater", "right": MIN_PM_VOLUME},
    ]
    payload = {
        "filter": filters, "options": {"lang": "en"}, "markets": ["america"],
        "symbols": {"query": {"types": []}, "tickers": []}, "columns": cols,
        "sort": {"sortBy": "premarket_volume", "sortOrder": "desc"},
        "range": [0, 1000],
    }
    r = requests.post(TV_URL, json=payload, headers=TV_HEADERS, timeout=30)
    r.raise_for_status()
    rows = r.json().get("data", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(zip(cols, x["d"])) for x in rows])
    df.insert(0, "symbol", [x["s"].split(":")[1] for x in rows])
    for c in ["premarket_close", "premarket_change", "premarket_volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["premarket_close", "premarket_change", "premarket_volume"])
    df = df[df["premarket_close"] >= MIN_PRICE].copy()
    df["dvol"] = df["premarket_volume"] * df["premarket_close"]
    df["abs_chg"] = df["premarket_change"].abs()
    df = df[(df["dvol"] >= MIN_PM_DOLLAR_VOL) & (df["abs_chg"] >= MIN_ABS_CHANGE)].copy()
    if df.empty:
        return df
    df["score"] = df["dvol"].rank(pct=True) + df["abs_chg"].rank(pct=True)
    return df.sort_values("score", ascending=False).head(TOP_N).reset_index(drop=True)


def heikin_ashi(df):
    o, h, l, c = (df[k].values for k in ["Open", "High", "Low", "Close"])
    ha_c = (o + h + l + c) / 4.0
    ha_o = np.empty(len(df))
    ha_o[0] = (o[0] + c[0]) / 2.0
    for i in range(1, len(df)):
        ha_o[i] = (ha_o[i - 1] + ha_c[i - 1]) / 2.0
    return pd.DataFrame({"h": np.maximum.reduce([h, ha_o, ha_c]),
                         "l": np.minimum.reduce([l, ha_o, ha_c]),
                         "c": ha_c}, index=df.index)


def halftrend(ha, amplitude=5):
    h, l, c = ha["h"], ha["l"], ha["c"]
    n = len(ha)
    hp = h.rolling(amplitude, min_periods=1).max().values
    lp = l.rolling(amplitude, min_periods=1).min().values
    hma = h.rolling(amplitude, min_periods=1).mean().values
    lma = l.rolling(amplitude, min_periods=1).mean().values
    H, L, C = h.values, l.values, c.values
    buy = np.zeros(n, dtype=bool)
    sell = np.zeros(n, dtype=bool)
    cur, nxt = 0, 0
    max_low, min_high = L[0], H[0]
    for i in range(1, n):
        prev = cur
        if nxt == 1:
            max_low = max(lp[i], max_low)
            if hma[i] < max_low and C[i] < L[i - 1]:
                cur, nxt = 1, 0
                min_high = hp[i]
        else:
            min_high = min(hp[i], min_high)
            if lma[i] > min_high and C[i] > H[i - 1]:
                cur, nxt = 0, 1
                max_low = lp[i]
        buy[i] = cur == 0 and prev == 1
        sell[i] = cur == 1 and prev == 0
    return buy, sell


def scan(symbols):
    data = yf.download(symbols, period="2d", interval="1m", prepost=True,
                       group_by="ticker", threads=True, progress=False,
                       auto_adjust=False)
    now = pd.Timestamp.now(tz=ET)
    found = []
    for s in symbols:
        try:
            d = data[s] if isinstance(data.columns, pd.MultiIndex) else data
            d = d[["Open", "High", "Low", "Close"]].dropna()
            if d.empty:
                continue
            bars = d.resample(f"{TF_MIN}min", label="left", closed="left").agg(
                {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
            bars = bars[bars.index + pd.Timedelta(minutes=TF_MIN) <= now]
            if len(bars) < AMPLITUDE + 5:
                continue
            buy, sell = halftrend(heikin_ashi(bars), AMPLITUDE)
            idx = np.where(buy | sell)[0]
            if len(idx) == 0:
                continue
            i = idx[-1]
            ts = bars.index[i]
            end = ts + pd.Timedelta(minutes=TF_MIN)
            if (now - end).total_seconds() / 60 > MAX_AGE_MIN:
                continue
            found.append((s, "BUY" if buy[i] else "SELL", float(bars["Close"].iloc[i]), ts))
        except Exception as e:
            print("skip", s, e)
    return found


def main():
    now_et = dt.datetime.now(ET)
    now_ist = now_et.astimezone(IST)
    if not in_window(now_et):
        print("Outside premarket window:", now_ist.strftime("%d-%b %H:%M IST"))
        return

    st = load_state()
    df = tv_list()
    if df.empty:
        print("TV list empty")
        return
    info = df.set_index("symbol")

    last = st.get("last_summary", "")
    due = True
    if last:
        due = (now_et - dt.datetime.fromisoformat(last)).total_seconds() / 60 >= SUMMARY_EVERY_MIN
    if due:
        lines = [f"TV LIST ({now_ist:%H:%M} IST)  Symbol | PM% | $Vol(M)"]
        for _, r in df.iterrows():
            lines.append(f"{r['symbol']} | {r['premarket_change']:+.1f}% | {r['dvol'] / 1e6:.1f}M")
        send("\n".join(lines))
        st["last_summary"] = now_et.isoformat()

    for sym, side, price, ts in scan(list(df["symbol"])):
        if st["alerts"].get(sym) == str(ts):
            continue
        r = info.loc[sym]
        send(f"TV + HALFTREND {side}\n{sym} @ {price:.2f}\n"
             f"PM change: {r['premarket_change']:+.2f}%\n"
             f"PM $Vol: {r['dvol'] / 1e6:.1f}M\n"
             f"Bar close: {(ts + pd.Timedelta(minutes=TF_MIN)).astimezone(IST):%H:%M} IST")
        st["alerts"][sym] = str(ts)

    save_state(st)


main()
