import os, re, json, time
import datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo
import requests
import pandas as pd

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

MIN_PRICE = 5.0
MIN_PM_VOLUME = 50_000
MIN_PM_DOLLAR_VOL = 500_000
MIN_ABS_CHANGE = 2.0
EXCHANGES = ["NYSE", "NASDAQ", "AMEX"]
NEWS_CHECK = 40
NEWS_HOURS = 48
FINAL_TOP = 10
SUMMARY_EVERY_MIN = 28
STATE_FILE = "pm_state.json"
ET = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")

TV_URL = "https://scanner.tradingview.com/america/scan"
UA = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"
TV_HEADERS = {"User-Agent": UA, "Content-Type": "application/json",
              "Origin": "https://www.tradingview.com",
              "Referer": "https://www.tradingview.com/"}
NEWS_HEADERS = {"User-Agent": UA}
CORE = ["name", "description", "close", "premarket_close",
        "premarket_change", "premarket_volume"]
OPTIONAL = ["average_volume_10d_calc"]

JUNK = re.compile(
    r"(stock price,? news|quote\s*&\s*history|interactive stock chart|historical (prices|data)"
    r"|stock price today|share price today|price today|\b[A-Z]{1,6}\d{6}[CP]\d{8}\b"
    r"|\b\d{4}\s+\d+(\.\d+)?\s+(put|call)\b)", re.I)
BACKGROUND = re.compile(
    r"\b(this year|year[- ]to[- ]date|ytd|best[- ]performers?|top \d+ (performers|stocks))\b", re.I)


def send(msg):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": msg[:4000]}, timeout=15)
        print("Telegram:", r.status_code, r.text[:150])
        return r.ok
    except Exception as e:
        print("Telegram exception:", e)
        return False


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {"last_summary": ""}


def tv_request(columns, filters, limit):
    payload = {"filter": filters, "options": {"lang": "en"}, "markets": ["america"],
               "symbols": {"query": {"types": []}, "tickers": []}, "columns": columns,
               "sort": {"sortBy": "premarket_volume", "sortOrder": "desc"},
               "range": [0, limit]}
    return requests.post(TV_URL, json=payload, headers=TV_HEADERS, timeout=30)


def stage1():
    filters = [
        {"left": "exchange", "operation": "in_range", "right": EXCHANGES},
        {"left": "type", "operation": "equal", "right": "stock"},
        {"left": "typespecs", "operation": "has", "right": ["common"]},
        {"left": "close", "operation": "egreater", "right": MIN_PRICE},
        {"left": "premarket_volume", "operation": "egreater", "right": MIN_PM_VOLUME},
    ]
    cols = CORE + OPTIONAL
    r = tv_request(cols, filters, 1000)
    if not r.ok:
        cols = CORE
        r = tv_request(cols, filters, 1000)
    r.raise_for_status()
    rows = r.json().get("data", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(zip(cols, x["d"])) for x in rows])
    df.insert(0, "symbol", [x["s"].split(":")[1] for x in rows])
    for c in ["close", "premarket_close", "premarket_change", "premarket_volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["premarket_close", "premarket_change", "premarket_volume"])
    df = df[df["premarket_close"] >= MIN_PRICE].copy()
    df["dvol"] = df["premarket_volume"] * df["premarket_close"]
    df["abs_chg"] = df["premarket_change"].abs()
    df = df[(df["dvol"] >= MIN_PM_DOLLAR_VOL) & (df["abs_chg"] >= MIN_ABS_CHANGE)].copy()
    if df.empty:
        return df
    s = df["dvol"].rank(pct=True) + df["abs_chg"].rank(pct=True)
    if "average_volume_10d_calc" in df.columns:
        avg = pd.to_numeric(df["average_volume_10d_calc"], errors="coerce")
        df["rvol"] = df["premarket_volume"] / avg
        s = s + df["rvol"].rank(pct=True).fillna(0)
    df["pre_score"] = s
    return df.sort_values("pre_score", ascending=False).reset_index(drop=True)


def clean_name(desc, sym):
    name = re.sub(r"\b(?:inc|corp|corporation|ltd|limited|holdings|holding|plc|group|company|co)\b\.?",
                  "", desc or "", flags=re.I)
    name = re.sub(r"[,]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name if len(name) >= 3 else sym


def fetch_rss(url, default_source, strip_suffix=False):
    try:
        r = requests.get(url, headers=NEWS_HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        root = ET.fromstring(r.content)
    except Exception:
        return None
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        pub = it.findtext("pubDate")
        if not title or not pub:
            continue
        src = (it.findtext("source") or default_source).strip()
        if strip_suffix and " - " in title:
            head, tail = title.rsplit(" - ", 1)
            if tail.strip().lower() == src.lower():
                title = head.strip()
        try:
            ts = pd.Timestamp(parsedate_to_datetime(pub))
            ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        except Exception:
            continue
        items.append((ts, title, src))
    return items


def is_relevant(title, sym, name):
    if re.search(r"\b" + re.escape(sym) + r"\b", title):
        return True
    first = name.split()[0].lower() if name else ""
    return len(first) >= 5 and first in title.lower()


def get_news(sym, desc):
    """Returns (tag, top_headline)."""
    name = clean_name(desc, sym)
    days = max(1, round(NEWS_HOURS / 24))
    g_url = ("https://news.google.com/rss/search?q=" + quote_plus(f'"{name}" stock when:{days}d')
             + "&hl=en-US&gl=US&ceid=US:en")
    y_url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym.replace('.', '-')}&region=US&lang=en-US"
    g = fetch_rss(g_url, "Google News", strip_suffix=True)
    y = fetch_rss(y_url, "Yahoo Finance")
    if g is None and y is None:
        return "?", ""
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=NEWS_HOURS)
    seen, cats, bg, generic = set(), [], 0, 0
    for ts, title, src in (y or []) + (g or []):
        key = title.lower()
        if ts < cutoff or key in seen or JUNK.search(title):
            continue
        seen.add(key)
        if not is_relevant(title, sym, name):
            generic += 1
        elif BACKGROUND.search(title):
            bg += 1
        else:
            cats.append((ts, title, src))
    if cats:
        cats.sort(key=lambda x: -x[0].timestamp())
        return f"NEWS({len(cats)})", f"{cats[0][1][:90]} ({cats[0][2]})"
    if bg:
        return "background", ""
    return ("generic" if generic else "no-news"), ""


def main():
    now_et = dt.datetime.now(ET)
    now_ist = now_et.astimezone(IST)
    t = now_et.time()
    if now_et.weekday() >= 5 or not (dt.time(4, 0) <= t < dt.time(9, 30)):
        print("Outside premarket window:", now_ist.strftime("%d-%b %H:%M IST"))
        return

    st = load_state()
    last = st.get("last_summary", "")
    if last:
        gap = (now_et - dt.datetime.fromisoformat(last)).total_seconds() / 60
        if gap < SUMMARY_EVERY_MIN:
            print(f"Last summary {gap:.0f} min ago, skipping")
            return

    df = stage1()
    if df.empty:
        print("No stocks passed filters")
        return

    top = df.head(NEWS_CHECK).copy()
    tags, heads = [], []
    for sym, desc in zip(top["symbol"], top["description"]):
        tg, hd = get_news(sym, desc)
        tags.append(tg)
        heads.append(hd)
        time.sleep(0.3)
    top["tag"] = tags
    top["head"] = heads
    top["final"] = top["pre_score"] + top["tag"].str.startswith("NEWS").astype(float)
    top = top.sort_values("final", ascending=False).head(FINAL_TOP)

    lines = [f"PREMARKET TOP {len(top)} ({now_ist:%H:%M} IST)",
             "Symbol | $Price | Chg% | $Vol(M) | RVOL | News", ""]
    for _, r in top.iterrows():
        rv = f"{r['rvol']:.2f}" if "rvol" in top.columns and pd.notna(r.get("rvol")) else "-"
        lines.append(f"{r['symbol']} | ${r['premarket_close']:.2f} | {r['premarket_change']:+.1f}% | "
                     f"{r['dvol'] / 1e6:.1f}M | {rv} | {r['tag']}")
        if r["head"]:
            lines.append(f"   > {r['head']}")
    lines.append("")
    lines.append("Entry se pehle chart, spread aur levels check karo.")

    if send("\n".join(lines)):
        st["last_summary"] = now_et.isoformat()
        json.dump(st, open(STATE_FILE, "w"))
        print("Summary sent")
    else:
        print("Telegram FAILED, will retry next run")


main()
