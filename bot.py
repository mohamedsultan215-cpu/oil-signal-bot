import os
import time
import json
import hashlib
import requests
import feedparser
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Dummy web server to satisfy Render free tier ──────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Oil Signal Bot is running")
    def log_message(self, *args):
        pass

def start_web_server():
    HTTPServer(("0.0.0.0", 10000), Handler).serve_forever()

# ── Config ────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CHECK_INTERVAL    = 60

FEEDS = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
]

KEYWORDS = [
    "hormuz", "iran", "opec", "crude oil", "brent", "oil price",
    "houthi", "red sea", "russia oil", "saudi", "oil supply",
    "oil production", "oil tanker", "trump iran", "oil sanctions",
]

seen = set()

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)

def is_relevant(title, summary=""):
    text = (title + " " + summary).lower()
    return any(k in text for k in KEYWORDS)

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=15)
        if r.status_code != 200:
            log(f"Telegram failed: {r.text[:200]}")
        else:
            log("Telegram message sent OK")
    except Exception as e:
        log(f"Telegram exception: {e}")

def analyze(title, summary, source):
    try:
        headers = {
            "content-type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01"
        }
        body = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 300,
            "system": "You are an oil markets trader. Respond ONLY with raw JSON, no markdown, no explanation.\n{\"action\":\"BUY|SELL|HOLD|WATCH\",\"confidence\":0-100,\"reasoning\":\"2 sentences\",\"brent_impact\":\"+/-$X/bbl\",\"timeframe\":\"X hours\"}",
            "messages": [{"role": "user", "content": f"Headline: {title}\nSource: {source}\nDetails: {summary[:400]}"}]
        }
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=30)
        log(f"Anthropic status: {r.status_code}")
        if r.status_code != 200:
            log(f"Anthropic error: {r.text[:300]}")
            return None
        data = r.json()
        text = data["content"][0]["text"].strip().replace("```json","").replace("```","")
        return json.loads(text)
    except Exception as e:
        log(f"Analyze exception: {e}")
        return None

def format_msg(signal, title, source):
    icons = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡", "WATCH": "🔵"}
    a = signal.get("action", "WATCH")
    return (
        f"{icons.get(a,'⚪')} <b>OIL SIGNAL: {a}</b>\n"
        f"📰 {title}\n"
        f"🏛 {source}\n\n"
        f"🎯 Confidence: {signal.get('confidence')}%\n"
        f"📈 Brent: {signal.get('brent_impact','?')}\n"
        f"⏱ {signal.get('timeframe','?')}\n\n"
        f"💬 {signal.get('reasoning','')}\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )

def check():
    log("Checking feeds...")
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
            src  = feed.feed.get("title", url)
            for entry in feed.entries[:8]:
                title   = entry.get("title", "")
                summary = entry.get("summary", "")
                link    = entry.get("link", "")
                uid     = hashlib.md5(link.encode()).hexdigest()
                if uid in seen:
                    continue
                seen.add(uid)
                if is_relevant(title, summary):
                    log(f"Relevant: {title[:70]}")
                    signal = analyze(title, summary, src)
                    if signal:
                        send_telegram(format_msg(signal, title, src))
                        log(f"Sent: {signal.get('action')} {signal.get('confidence')}%")
                    time.sleep(2)
        except Exception as e:
            log(f"Feed error: {e}")

def main():
    log("=== Oil Signal Bot starting ===")
    log(f"TELEGRAM_TOKEN set: {bool(TELEGRAM_TOKEN)}")
    log(f"TELEGRAM_CHAT_ID set: {bool(TELEGRAM_CHAT_ID)}")
    log(f"ANTHROPIC_API_KEY set: {bool(ANTHROPIC_API_KEY)}")

    # Start web server in background
    threading.Thread(target=start_web_server, daemon=True).start()
    log("Web server started on port 10000")

    send_telegram("🛢 <b>Oil Signal Bot is LIVE</b>\n\nMonitoring oil news 24/7.\nYou'll get signals here within 60 seconds of major news.")

    while True:
        check()
        if len(seen) > 2000:
            seen.clear()
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
