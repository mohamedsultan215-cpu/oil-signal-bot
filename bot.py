import os
import time
import json
import hashlib
import requests
import feedparser
from datetime import datetime,timezone 

# ── CONFIG ──────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CHECK_INTERVAL   = 60  # seconds between checks

# ── RSS FEEDS ────────────────────────────────────────────
FEEDS = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.reuters.com/reuters/topNews",
    "https://www.ft.com/rss/home",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
]

# ── KEYWORDS THAT TRIGGER ANALYSIS ──────────────────────
KEYWORDS = [
    "hormuz", "strait of hormuz",
    "trump iran", "iran attack", "iran strike", "iran nuclear", "iran sanctions",
    "iran oil", "iran military",
    "opec", "opec+", "oil cut", "oil output", "production cut",
    "crude oil", "brent", "oil price", "oil supply",
    "houthi", "red sea attack", "red sea shipping",
    "russia oil", "russian energy", "oil sanctions",
    "saudi aramco", "saudi oil",
    "oil tanker", "oil pipeline",
    "israel iran", "middle east war", "persian gulf",
    "eia report", "crude inventory", "oil stockpile",
    "china oil", "china demand",
]

seen_articles = set()

def is_oil_relevant(title, summary=""):
    text = (title + " " + summary).lower()
    return any(kw in text for kw in KEYWORDS)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def analyze_with_claude(title, summary, source):
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01"
    }
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 400,
        "system": """You are a senior oil markets trader. Analyze news and return ONLY raw JSON, no markdown.

{
  "action": "BUY" | "SELL" | "HOLD" | "WATCH",
  "confidence": <0-100>,
  "reasoning": "<2 sentences max>",
  "brent_impact": "<e.g. +$3-5/bbl or neutral>",
  "timeframe": "<e.g. 24-48 hours>"
}""",
        "messages": [{
            "role": "user",
            "content": f"SOURCE: {source}\nHEADLINE: {title}\nDETAILS: {summary[:500]}"
        }]
    }
    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=body,
            timeout=30
        )
        data = res.json()
        text = data["content"][0]["text"].strip()
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"Claude error: {e}")
        return None

def format_signal(signal, title, source):
    action = signal.get("action", "WATCH")
    confidence = signal.get("confidence", 0)
    reasoning = signal.get("reasoning", "")
    brent = signal.get("brent_impact", "unknown")
    timeframe = signal.get("timeframe", "unknown")

    emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡", "WATCH": "🔵"}.get(action, "⚪")

    return f"""{emoji} <b>OIL SIGNAL: {action}</b>
📰 {title}
🏛 {source}

🎯 Confidence: {confidence}%
📈 Brent impact: {brent}
⏱ Timeframe: {timeframe}

💬 {reasoning}

🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"""

def check_feeds():
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Checking feeds...")
    for feed_url in FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                title   = entry.get("title", "")
                summary = entry.get("summary", "")
                link    = entry.get("link", "")
                source  = feed.feed.get("title", feed_url)

                article_id = hashlib.md5(link.encode()).hexdigest()
                if article_id in seen_articles:
                    continue
                seen_articles.add(article_id)

                if is_oil_relevant(title, summary):
                    print(f"  ⚡ Relevant: {title[:60]}...")
                    signal = analyze_with_claude(title, summary, source)
                    if signal:
                        msg = format_signal(signal, title, source)
                        send_telegram(msg)
                        print(f"  ✅ Signal sent: {signal.get('action')} ({signal.get('confidence')}%)")
                        time.sleep(3)
        except Exception as e:
            print(f"Feed error ({feed_url}): {e}")

def main():
    print("🛢 Oil Signal Bot started")
    send_telegram("🛢 <b>Oil Signal Bot is now LIVE</b>\n\nMonitoring: Hormuz • Trump/Iran • OPEC • Red Sea • Russia • EIA\n\nYou'll get alerts here within 60 seconds of any major oil news.")
    
    # Keep seen_articles bounded
    while True:
        check_feeds()
        if len(seen_articles) > 1000:
            seen_articles.clear()
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
