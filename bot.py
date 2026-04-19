import os
import time
import json
import hashlib
import requests
import feedparser
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── Web server to keep Render happy ───────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Oil Signal Bot - Running")
    def log_message(self, *args):
        pass

def start_web_server():
    HTTPServer(("0.0.0.0", 10000), Handler).serve_forever()

# ── Config ─────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_SHEET_ID   = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_CLIENT_EMAIL = os.environ.get("GOOGLE_CLIENT_EMAIL", "")
GOOGLE_PRIVATE_KEY  = os.environ.get("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
CHECK_INTERVAL    = 60
MIN_CONFIDENCE    = 82

# ── Google Sheets setup ────────────────────────────────────
def get_sheets_service():
    try:
        # Clean the private key — fix any formatting issues
        private_key = GOOGLE_PRIVATE_KEY
        if "\\n" in private_key:
            private_key = private_key.replace("\\n", "\n")
        if not private_key.startswith("-----BEGIN"):
            private_key = private_key.replace(" ", "\n")
        credentials = service_account.Credentials.from_service_account_info(
            {
                "type": "service_account",
                "client_email": GOOGLE_CLIENT_EMAIL,
                "private_key": private_key,
                "token_uri": "https://oauth2.googleapis.com/token",
            },
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        return build("sheets", "v4", credentials=credentials)
    except Exception as e:
        log(f"Sheets setup error: {e}")
        return None

def init_sheet():
    try:
        service = get_sheets_service()
        if not service:
            return
        # Add headers if sheet is empty
        headers = [["Date", "Time", "Action", "Confidence", "Entry", "SL", "TP", "R/R", "Timeframe", "Headline", "Source", "Result", "Notes"]]
        service.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="Sheet1!A1:M1",
            valueInputOption="RAW",
            body={"values": headers}
        ).execute()
        log("✅ Google Sheet initialized")
    except Exception as e:
        log(f"Sheet init error: {e}")

def log_signal_to_sheet(signal, title, source):
    try:
        service = get_sheets_service()
        if not service:
            return
        now = datetime.now(timezone.utc)
        row = [[
            now.strftime("%Y-%m-%d"),
            now.strftime("%H:%M UTC"),
            signal.get("action", ""),
            signal.get("confidence", ""),
            signal.get("entry", ""),
            signal.get("sl", ""),
            signal.get("tp", ""),
            signal.get("rr", ""),
            signal.get("timeframe", ""),
            title[:100],
            source[:50],
            "",  # Result — filled manually
            ""   # Notes — filled manually
        ]]
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="Sheet1!A:M",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": row}
        ).execute()
        log("✅ Signal logged to Google Sheet")
    except Exception as e:
        log(f"Sheet log error: {e}")

def get_weekly_stats():
    try:
        service = get_sheets_service()
        if not service:
            return None
        result = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="Sheet1!A2:M1000"
        ).execute()
        rows = result.get("values", [])
        if not rows:
            return None

        # Filter this week
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        week_rows = [r for r in rows if len(r) > 0 and r[0] >= week_ago]

        if not week_rows:
            return None

        total   = len(week_rows)
        buys    = sum(1 for r in week_rows if len(r) > 2 and r[2] == "BUY")
        sells   = total - buys
        wins    = sum(1 for r in week_rows if len(r) > 11 and r[11].upper() == "WIN")
        losses  = sum(1 for r in week_rows if len(r) > 11 and r[11].upper() == "LOSS")
        open_   = total - wins - losses

        return {
            "total": total,
            "buys": buys,
            "sells": sells,
            "wins": wins,
            "losses": losses,
            "open": open_,
            "win_rate": round((wins / (wins + losses) * 100)) if (wins + losses) > 0 else 0
        }
    except Exception as e:
        log(f"Stats error: {e}")
        return None

def send_weekly_report():
    stats = get_weekly_stats()
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=7)).strftime("%b %d")
    week_end = now.strftime("%b %d")

    if not stats or stats["total"] == 0:
        msg = (
            f"📊 <b>WEEK REPORT — {week_start} to {week_end}</b>\n"
            f"{'─' * 30}\n"
            f"No signals this week.\n"
            f"Markets were quiet 🤙"
        )
    else:
        msg = (
            f"📊 <b>WEEK REPORT — {week_start} to {week_end}</b>\n"
            f"{'─' * 30}\n"
            f"Signals:  {stats['total']}\n"
            f"🟢 BUY:   {stats['buys']}\n"
            f"🔴 SELL:  {stats['sells']}\n"
            f"{'─' * 30}\n"
            f"✅ Won:   {stats['wins']}\n"
            f"❌ Lost:  {stats['losses']}\n"
            f"⏳ Open:  {stats['open']}\n"
            f"{'─' * 30}\n"
        )
        if (stats['wins'] + stats['losses']) > 0:
            msg += f"Win rate: <b>{stats['win_rate']}%</b>\n"
            msg += f"{'─' * 30}\n"

        msg += f"📋 Full log: sheets.google.com\n"
        msg += f"{'─' * 30}\n"
        msg += f"Keep managing your risk 🤙"

    send_telegram(msg)

# ── Live Brent price ───────────────────────────────────────
def get_brent_price():
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/BZ=F",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        price = r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return round(float(price), 2)
    except:
        return None

# ── ALL SOURCES ────────────────────────────────────────────
FEEDS = [
    # Major News Wires
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.reuters.com/reuters/worldNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "https://www.ft.com/rss/home",
    "https://feeds.skynews.com/feeds/rss/world.xml",
    "https://feeds.skynews.com/feeds/rss/business.xml",
    # Energy Specific
    "https://oilprice.com/rss/main",
    "https://www.rigzone.com/news/rss/rigzone_latest.aspx",
    "https://feeds.feedburner.com/EiaWeeklyPetroleumReport",
    "https://www.offshore-technology.com/feed/",
    "https://www.worldoil.com/rss",
    "https://www.ogj.com/rss/",
    # Middle East / Gulf
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://english.alarabiya.net/tools/rss",
    "https://www.arabnews.com/rss.xml",
    "https://gulfnews.com/rss",
    "https://www.thenationalnews.com/rss",
    # Geopolitical
    "https://www.defensenews.com/rss/",
    # X/Twitter via Nitter
    "https://nitter.poast.org/KobeissiLetter/rss",
    "https://nitter.poast.org/spectatorindex/rss",
    "https://nitter.poast.org/zerohedge/rss",
    "https://nitter.poast.org/RaoulGMI/rss",
    "https://nitter.poast.org/LynAldenContact/rss",
    "https://nitter.poast.org/OilPrice_com/rss",
    "https://nitter.poast.org/Reuters/rss",
    "https://nitter.poast.org/business/rss",
    "https://nitter.poast.org/BBCWorld/rss",
    "https://nitter.poast.org/FT/rss",
    "https://nitter.poast.org/WSJ/rss",
    "https://nitter.poast.org/EIAgov/rss",
    "https://nitter.poast.org/OPECSecretariat/rss",
    "https://nitter.poast.org/IEA/rss",
    "https://nitter.poast.org/AlArabiya_Eng/rss",
    "https://nitter.poast.org/realDonaldTrump/rss",
    "https://nitter.poast.org/POTUS/rss",
    "https://nitter.poast.org/StateDept/rss",
    "https://nitter.poast.org/CENTCOM/rss",
]

# ── Keywords & Noise ───────────────────────────────────────
OIL_KEYWORDS = [
    "crude oil", "brent crude", "wti crude", "oil price",
    "oil supply", "oil production", "oil output", "oil cut",
    "oil exports", "oil embargo", "oil sanctions",
    "opec", "opec+", "saudi aramco", "saudi oil",
    "oil barrel", "barrels per day", "bpd",
    "hormuz", "strait of hormuz", "persian gulf tanker",
    "iran oil", "iran nuclear", "iran sanctions",
    "trump iran", "us iran", "iran attack", "iran strike",
    "houthi", "red sea tanker", "oil tanker",
    "russia oil", "russian crude", "oil price cap",
    "oil demand", "oil inventory", "eia crude",
    "oil rally", "oil plunge", "oil surge", "oil crash",
    "oil market", "energy crisis", "oil shock",
    "petroleum", "oil field", "oil pipeline",
]

NOISE_WORDS = [
    "diesel prices", "petrol prices", "gasoline prices",
    "fuel pump", "heating oil retail", "natural gas prices",
    "electricity prices", "coal prices",
    "mortgage", "housing market", "stock market", "nasdaq",
    "bitcoin", "crypto", "gold prices", "silver prices",
    "corn", "wheat", "soybean", "cooking oil",
]

seen = set()
last_weekly_report = None

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)

def is_oil_relevant(title, summary=""):
    text = (title + " " + summary).lower()
    has_oil = any(k in text for k in OIL_KEYWORDS)
    if not has_oil:
        return False
    noise_count = sum(1 for n in NOISE_WORDS if n in text)
    oil_count = sum(1 for k in OIL_KEYWORDS if k in text)
    if noise_count > oil_count:
        return False
    return True

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=15
        )
        if r.status_code == 200:
            log("✅ Telegram sent")
        else:
            log(f"❌ Telegram failed: {r.text[:150]}")
    except Exception as e:
        log(f"Telegram error: {e}")

def analyze_with_claude(title, summary, source, brent_price):
    try:
        price_context = f"Current Brent price: ${brent_price}/bbl." if brent_price else ""
        headers = {
            "content-type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01"
        }
        body = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 400,
            "system": f"""You are a senior crude oil trader with 20 years experience. {price_context}

Analyze news for HIGH CONVICTION crude oil trade signals only.

Rules:
- Only BUY or SELL — never anything else
- Be ruthless — most news is noise, score below 82
- Supply disruption/war risk = BUY
- Supply increase/peace deal/demand drop = SELL
- Only crude oil (Brent/WTI) — ignore diesel/gas/electricity
- SL = entry minus ~$1.20/bbl
- TP = minimum 2x the SL distance
- One line reasoning MAX — sharp and direct

Respond ONLY with raw JSON, no markdown:
{{
  "action": "BUY" or "SELL",
  "confidence": <0-100>,
  "entry": <price as float or null>,
  "sl": <price as float or null>,
  "tp": <price as float or null>,
  "rr": "<e.g. 1:2.1>",
  "timeframe": "<e.g. 2-6 hours>",
  "reasoning": "<one sharp line>",
  "is_oil_relevant": true or false
}}""",
            "messages": [{
                "role": "user",
                "content": f"SOURCE: {source}\nHEADLINE: {title}\nDETAILS: {summary[:600]}"
            }]
        }
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=body,
            timeout=30
        )
        log(f"Anthropic: {r.status_code}")
        if r.status_code != 200:
            log(f"Anthropic error: {r.text[:200]}")
            return None
        text = r.json()["content"][0]["text"].strip()
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        log(f"Claude error: {e}")
        return None

def format_signal(signal, title, source):
    action     = signal.get("action", "")
    confidence = signal.get("confidence", 0)
    entry      = signal.get("entry")
    sl         = signal.get("sl")
    tp         = signal.get("tp")
    rr         = signal.get("rr", "?")
    timeframe  = signal.get("timeframe", "?")
    reasoning  = signal.get("reasoning", "")

    emoji    = "🟢" if action == "BUY" else "🔴"
    strength = ("STRONG " if confidence >= 90 else "") + action
    bars     = "█" * (confidence // 10) + "░" * (10 - confidence // 10)

    entry_str = f"~${entry:.2f}" if entry else "market"
    sl_str    = f"${sl:.2f}"    if sl    else "—"
    tp_str    = f"${tp:.2f}"    if tp    else "—"

    return (
        f"{emoji} <b>CRUDE OIL — {strength}</b>\n"
        f"{'─' * 30}\n"
        f"📊 Confidence: {confidence}%  {bars}\n"
        f"⏱ Window: {timeframe}\n"
        f"{'─' * 30}\n"
        f"🎯 Entry:  {entry_str}\n"
        f"🛑 SL:     {sl_str}\n"
        f"✅ TP:     {tp_str}\n"
        f"📊 R/R:    {rr}\n"
        f"{'─' * 30}\n"
        f"📰 {title[:80]}\n"
        f"🏛 {source}  •  {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
        f"{'─' * 30}\n"
        f"💬 {reasoning}\n"
        f"{'─' * 30}\n"
        f"⚠️ Manage your risk always."
    )

def check_weekly_report():
    global last_weekly_report
    now = datetime.now(timezone.utc)
    # Send every Sunday at 8am Dubai time (4am UTC)
    if now.weekday() == 6 and now.hour == 4 and now.minute < 1:
        today = now.strftime("%Y-%m-%d")
        if last_weekly_report != today:
            log("📊 Sending weekly report...")
            send_weekly_report()
            last_weekly_report = today

def check_feeds():
    log("Checking feeds...")
    brent_price = get_brent_price()
    if brent_price:
        log(f"Brent: ${brent_price}/bbl")

    for feed_url in FEEDS:
        try:
            feed   = feedparser.parse(feed_url)
            source = feed.feed.get("title", feed_url.split("/")[2])

            for entry in feed.entries[:10]:
                title   = entry.get("title", "")
                summary = entry.get("summary", entry.get("description", ""))
                link    = entry.get("link", "")

                uid = hashlib.md5((title + link).encode()).hexdigest()
                if uid in seen:
                    continue
                seen.add(uid)

                if not is_oil_relevant(title, summary):
                    continue

                log(f"⚡ {title[:70]}...")

                signal = analyze_with_claude(title, summary, source, brent_price)
                if not signal:
                    continue

                if not signal.get("is_oil_relevant", True):
                    log("  → Not oil relevant, skipped")
                    continue

                confidence = signal.get("confidence", 0)
                action     = signal.get("action", "")
                log(f"  → {action} | {confidence}%")

                if confidence >= MIN_CONFIDENCE and action in ["BUY", "SELL"]:
                    msg = format_signal(signal, title, source)
                    send_telegram(msg)
                    log_signal_to_sheet(signal, title, source)
                    time.sleep(3)
                else:
                    log(f"  → Below {MIN_CONFIDENCE}%, skipped")

        except Exception as e:
            log(f"Feed error: {e}")

def main():
    global last_weekly_report
    log("=" * 50)
    log("CRUDE OIL SIGNAL BOT — PHASE 1")
    log("=" * 50)
    log(f"TELEGRAM:    {'✅' if TELEGRAM_TOKEN else '❌'}")
    log(f"ANTHROPIC:   {'✅' if ANTHROPIC_API_KEY else '❌'}")
    log(f"GOOGLE SHEET:{'✅' if GOOGLE_SHEET_ID else '❌'}")
    log(f"SOURCES:     {len(FEEDS)}")
    log(f"MIN CONF:    {MIN_CONFIDENCE}%")
    log("=" * 50)

    threading.Thread(target=start_web_server, daemon=True).start()
    log("Web server: ✅ port 10000")

    init_sheet()

    send_telegram(
        "🛢 <b>CRUDE OIL SIGNAL BOT — LIVE</b>\n\n"
        f"📡 Monitoring <b>{len(FEEDS)} sources</b>\n\n"
        "<b>News:</b> Reuters, BBC, FT, NYT, Sky, Al Jazeera, Al Arabiya, Arab News\n"
        "<b>Energy:</b> OilPrice, Rigzone, EIA, OGJ, WorldOil\n"
        "<b>Twitter:</b> @KobeissiLetter @spectatorindex @zerohedge @EIAgov @OPEC @IEA @CENTCOM @realDonaldTrump\n\n"
        f"⚙️ Min confidence: <b>{MIN_CONFIDENCE}%</b>\n"
        "🎯 Signals: <b>BUY or SELL only</b>\n"
        "📊 Every signal: Entry, SL, TP, R/R\n"
        "📋 Track record: Saved to Google Sheets\n"
        "📆 Weekly report: Every Sunday 8am Dubai\n\n"
        "Watching 24/7. Only hear from me when it matters. 🤙"
    )

    while True:
        check_feeds()
        check_weekly_report()
        if len(seen) > 5000:
            seen.clear()
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
