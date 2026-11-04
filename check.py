"""
YouTube -> Discord Notifier
Prüft die RSS-Feeds der YouTube-Kanäle aus config.json und postet neue
Videos mit Ping per Discord-Webhook. Läuft per GitHub Actions nach Zeitplan.

Nur Python-Standardbibliothek, keine Installation nötig.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

CONFIG_FILE = "config.json"
STATE_FILE = "state.json"
MAX_SEEN = 50  # wie viele Video-IDs pro Kanal gemerkt werden
KEEPALIVE_DAYS = 30  # GitHub pausiert Zeitpläne nach 60 Tagen ohne Commit
FAIL_ALERT_AFTER = 6  # erst nach so vielen komplett fehlgeschlagenen Läufen am Stück als Fehler melden

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}
UA = {"User-Agent": "Mozilla/5.0 (yt-discord-notifier)"}


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def http_get(url, tries=3):
    """Lädt eine Seite. Bei kurzen YouTube-Aussetzern bis zu 3 Versuche mit Pause."""
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception:
            if attempt == tries:
                raise
            time.sleep(5 * attempt)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_no_redirect_opener = urllib.request.build_opener(_NoRedirect)


def is_short(video_id, url):
    """True, wenn das Video ein YouTube Short ist.
    youtube.com/shorts/<id> antwortet bei Shorts mit 200, bei normalen Videos
    mit einer Weiterleitung auf /watch."""
    if "/shorts/" in url:
        return True
    req = urllib.request.Request(f"https://www.youtube.com/shorts/{video_id}", headers=UA)
    try:
        with _no_redirect_opener.open(req, timeout=20) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False  # 3xx (Weiterleitung) = normales Video
    except Exception as e:
        print(f"  Konnte nicht prüfen, ob {video_id} ein Short ist ({e}) – wird als Video behandelt.")
        return False


def resolve_channel_id(entry, state):
    """Gibt die Kanal-ID (UC...) zurück. Akzeptiert 'id', 'url' oder 'handle'."""
    if entry.get("id"):
        return entry["id"]
    url = entry.get("url", "").strip()
    if not url:
        handle = entry.get("handle", "").strip()
        if not handle:
            raise ValueError(f"Kanal ohne 'id', 'url' oder 'handle': {entry}")
        if not handle.startswith("@"):
            handle = "@" + handle
        url = f"https://www.youtube.com/{handle}"
    m = re.search(r"youtube\.com/channel/(UC[\w-]{22})", url)
    if m:
        return m.group(1)
    cache = state.setdefault("resolved", {})
    if url in cache:
        return cache[url]
    html = http_get(url)
    m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', html) \
        or re.search(r'"externalId":"(UC[\w-]{22})"', html)
    if not m:
        raise ValueError(f"Konnte Kanal-ID für {url} nicht finden")
    cache[url] = m.group(1)
    return m.group(1)


def fetch_videos(channel_id):
    """Liste von (video_id, titel, url, kanalname), neueste zuerst."""
    xml = http_get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
    root = ET.fromstring(xml)
    author = root.findtext("atom:title", default="", namespaces=NS)
    videos = []
    for e in root.findall("atom:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS)
        title = e.findtext("atom:title", default="", namespaces=NS)
        link = e.find("atom:link", NS)
        url = link.get("href") if link is not None else f"https://www.youtube.com/watch?v={vid}"
        videos.append((vid, title, url, author))
    return videos


BOT_NAME = ""


def post_discord(webhook, content, ping=True):
    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["roles", "everyone", "users"] if ping else []},
    }
    if BOT_NAME:
        payload["username"] = BOT_NAME
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=body, method="POST",
        headers={"Content-Type": "application/json", **UA},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        if r.status >= 300:
            raise RuntimeError(f"Discord antwortete mit {r.status}")


def format_message(config, entry, title, url, author, with_ping=True):
    template = entry.get("message") or config.get("message", "{ping} {url}")
    ping = entry.get("ping", config.get("ping", "")) if with_ping else ""
    return template.format(
        ping=ping, title=title, url=url,
        channel=entry.get("name") or author,
    ).strip()


def main():
    test_mode = "--test" in sys.argv
    silent_mode = "--silent" in sys.argv  # neue Videos nur merken, nichts posten
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        sys.exit("Fehler: DISCORD_WEBHOOK_URL ist nicht gesetzt (GitHub Secret fehlt).")

    config = load_json(CONFIG_FILE, None)
    if not config or not config.get("channels"):
        sys.exit("Fehler: config.json fehlt oder enthält keine Kanäle.")
    global BOT_NAME
    BOT_NAME = config.get("bot_name", "")
    skip_shorts = config.get("skip_shorts", True)
    state = load_json(STATE_FILE, {})
    seen_all = state.setdefault("seen", {})
    errors = 0
    test_sent = False

    for entry in config["channels"]:
        label = entry.get("name") or entry.get("url") or entry.get("handle") or entry.get("id")
        try:
            cid = resolve_channel_id(entry, state)
            videos = fetch_videos(cid)
        except Exception as ex:
            print(f"[{label}] Fehler beim Abrufen: {ex}")
            errors += 1
            continue

        if test_mode:
            # Testmodus: genau EINE Nachricht insgesamt, ohne Ping
            if test_sent:
                continue
            for vid, title, url, author in videos:
                if skip_shorts and is_short(vid, url):
                    continue
                msg = format_message(config, entry, title, url, author, with_ping=False)
                post_discord(webhook, "🧪 Test (ohne Ping): " + msg, ping=False)
                print(f"[{label}] Testnachricht gesendet: {title}")
                test_sent = True
                break
            continue

        first_run = cid not in seen_all
        seen = seen_all.setdefault(cid, [])

        if first_run:
            # Beim ersten Lauf nur merken, nichts posten (kein Spam alter Videos)
            seen.extend(v[0] for v in videos)
            print(f"[{label}] Erster Lauf: {len(videos)} vorhandene Videos gemerkt.")
        else:
            new = [v for v in videos if v[0] not in seen]
            for vid, title, url, author in reversed(new):  # älteste zuerst posten
                if silent_mode:
                    seen.insert(0, vid)
                    print(f"[{label}] Still gemerkt (nicht gepostet): {title}")
                    continue
                if skip_shorts and is_short(vid, url):
                    seen.insert(0, vid)
                    print(f"[{label}] Short übersprungen: {title}")
                    continue
                post_discord(webhook, format_message(config, entry, title, url, author))
                seen.insert(0, vid)
                print(f"[{label}] Gepostet: {title}")
            if not new:
                print(f"[{label}] Nichts Neues.")

        seen_all[cid] = seen[:MAX_SEEN]

    # Wenn YouTube komplett nicht erreichbar war: nicht sofort als Fehler melden,
    # sondern erst, wenn das mehrere Läufe hintereinander passiert.
    all_failed = errors == len(config["channels"])
    state["fail_streak"] = state.get("fail_streak", 0) + 1 if all_failed else 0

    if not test_mode:
        # Keepalive: alle 30 Tage einen Commit erzeugen, damit GitHub den Zeitplan nicht pausiert
        today = datetime.now(timezone.utc).date()
        last = state.get("keepalive")
        if not last or (today - datetime.fromisoformat(last).date()).days >= KEEPALIVE_DAYS:
            state["keepalive"] = today.isoformat()
        save_json(STATE_FILE, state)

    if all_failed:
        if state["fail_streak"] >= FAIL_ALERT_AFTER:
            sys.exit(f"YouTube war {state['fail_streak']} Läufe hintereinander nicht erreichbar.")
        print(f"YouTube gerade nicht erreichbar ({state['fail_streak']}x am Stück) – nächster Lauf holt alles nach.")


if __name__ == "__main__":
    main()
