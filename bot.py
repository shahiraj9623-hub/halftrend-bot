import os
import numpy as np
import pandas as pd
import requests
import yfinance as yf

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TICKER = "GC=F"
AMPLITUDE = 5
TF_MIN = 3
STATE_FILE = "last_alert.txt"
MAX_AGE_MIN = 15  # ignore signals older than this


def send(msg):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
        timeout=15,
    )
    print("Telegram:", r.status_code, r.text[:100])


def fetch_closed_3m():
    df = yf.download(TICKER, period="5d", interval="1m",
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].dropna()
    last_1m = df.index[-1]
    bars = df.resample(f"{TF_MIN}min", label="left", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    ).dropna()
    return bars[bars.index + pd.Timedelta(minutes=TF_MIN) <= last_1m]


def heikin_ashi(df):
    o, h, l, c = (df[k].values for k in ["Open", "High", "Low", "Close"])
    ha_c = (o + h + l + c) / 4.0
    ha_o = np.empty(len(df))
    ha_o[0] = (o[0] + c[0]) / 2.0
    for i in range(1, len(df)):
        ha_o[i] = (ha_o[i - 1] + ha_c[i - 1]) / 2.0
    ha_h = np.maximum.reduce([h, ha_o, ha_c])
    ha_l = np.minimum.reduce([l, ha_o, ha_c])
    return pd.DataFrame({"o": ha_o, "h": ha_h, "l": ha_l, "c": ha_c}, index=df.index)


def halftrend(ha, amplitude=5):
    h, l, c = ha["h"], ha["l"], ha["c"]
    n = len(ha)
    high_price = h.rolling(amplitude, min_periods=1).max().values
    low_price = l.rolling(amplitude, min_periods=1).min().values
    highma = h.rolling(amplitude, min_periods=1).mean().values
    lowma = l.rolling(amplitude, min_periods=1).mean().values
    H, L, C = h.values, l.values, c.values

    trend = np.zeros(n, dtype=int)
    ht = np.full(n, np.nan)
    buy = np.zeros(n, dtype=bool)
    sell = np.zeros(n, dtype=bool)
    cur, nxt = 0, 0
    max_low, min_high = L[0], H[0]
    up, down = np.nan, np.nan

    for i in range(1, n):
        prev = cur
        if nxt == 1:
            max_low = max(low_price[i], max_low)
            if highma[i] < max_low and C[i] < L[i - 1]:
                cur, nxt = 1, 0
                min_high = high_price[i]
        else:
            min_high = min(high_price[i], min_high)
            if lowma[i] > min_high and C[i] > H[i - 1]:
                cur, nxt = 0, 1
                max_low = low_price[i]

        if cur == 0:
            if prev != 0:
                up = down if not np.isnan(down) else up
            else:
                up = max_low if np.isnan(up) else max(max_low, up)
            ht[i] = up
        else:
            if prev != 1:
                down = up if not np.isnan(up) else down
            else:
                down = min_high if np.isnan(down) else min(min_high, down)
            ht[i] = down

        trend[i] = cur
        buy[i] = cur == 0 and prev == 1
        sell[i] = cur == 1 and prev == 0

    return pd.DataFrame({"trend": trend, "ht": ht, "buy": buy, "sell": sell},
                        index=ha.index)


def main():
    bars = fetch_closed_3m()
    if bars is None or len(bars) < AMPLITUDE + 5:
        print("No data / market closed")
        return

    res = halftrend(heikin_ashi(bars), AMPLITUDE)
    sig = res[res["buy"] | res["sell"]]
    if sig.empty:
        print("No signals in data")
        return

    ts = sig.index[-1]
    row = sig.iloc[-1]
    bar_close = ts + pd.Timedelta(minutes=TF_MIN)
    age_min = (pd.Timestamp.now(tz=bar_close.tz) - bar_close).total_seconds() / 60

    last = ""
    if os.path.exists(STATE_FILE):
        last = open(STATE_FILE).read().strip()

    if str(ts) == last:
        print("Already alerted:", ts)
        return
    if age_min > MAX_AGE_MIN:
        print("Latest signal too old:", ts)
        return

    side = "BUY" if row["buy"] else "SELL"
    price = bars.loc[ts, "Close"]
    msg = (f"<b>{side} SIGNAL</b> (HalfTrend)\n"
           f"{TICKER} @ {price:.2f}\n"
           f"HT: {row['ht']:.2f}\n"
           f"Bar close (UTC): {bar_close.tz_convert('UTC').strftime('%d-%b %H:%M')}")
    send(msg)
    with open(STATE_FILE, "w") as f:
        f.write(str(ts))


main()
