"""
BiXBiT Lead Agent - Telegram reader (GitHub Actions version).

Reads new messages from the public Telegram channels and groups listed in the
Google Sheet "BiXBiT Lead Agent - Telegram" (tab "channels"), keeps only the
messages that match the keywords (tab "keywords") and appends them to the
"messages" tab. The Claude agent reads that tab on every run, qualifies the
messages and writes leads to the lead queue.

The sheet is reached through the Apps Script web app (Code.gs), so no Google
Cloud project or service account is needed.

The script only READS Telegram. It never sends messages, joins chats or reacts.

Session: TG_SESSION_STRING (made once by make_session.py) is used when set,
otherwise a local session file TG_SESSION.
"""

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from telethon import TelegramClient
from telethon.errors import FloodWaitError, UsernameInvalidError, UsernameNotOccupiedError, ChannelPrivateError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION_STRING = os.environ.get("TG_SESSION_STRING", "").strip()
SESSION = StringSession(SESSION_STRING) if SESSION_STRING else os.environ.get("TG_SESSION", "bixbit_reader")
WEBAPP_URL = os.environ["SHEET_WEBAPP_URL"]
WEBAPP_TOKEN = os.environ["SHEET_TOKEN"]

FIRST_RUN_DAYS = int(os.environ.get("FIRST_RUN_DAYS", "14"))   # how far back on the first read of a chat
MAX_PER_CHAT = int(os.environ.get("MAX_PER_CHAT", "3000"))      # messages read per chat per run
MIN_TEXT_LEN = 25
PAUSE_BETWEEN_CHATS = 3                                          # seconds, keeps well under Telegram limits
TEXT_LIMIT = 2000                                                # chars stored per message


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def call(action, **payload):
    """POST to the Apps Script web app. Retries a few times on network errors."""
    body = {"token": WEBAPP_TOKEN, "action": action, **payload}
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(WEBAPP_URL, json=body, timeout=60)
            data = r.json()
            if data.get("ok"):
                return data
            last_err = data.get("error", "unknown")
            if last_err in ("unauthorized", "bad_json", "unknown_action", "bad_row", "too_many_rows"):
                break
        except Exception as e:  # network or JSON problem
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"web app call {action} failed: {last_err}")


EXCLUDE = ["в наличии", "прайс", "price list", "pricelist", "in stock", "pre-order", "предзаказ", "предоплат", "оптом", "доставка", "usdt", "под заказ", "продам", "продаю", "for sale", "wts", "добро пожаловать"]


def match(text, keywords):
    t = text.lower()
    if any(x in t for x in EXCLUDE):
        return [], []
    hits = [k for k, _ in keywords if k in t]
    groups = {g for k, g in keywords if k in t}
    # keep a message only if it shows business scale (fleet, hosting, MW, containers...)
    # together with a product topic, hardware or a competitor mention
    useful = "scale" in groups and bool(groups & {"AMS", "Firmware", "Cooling", "competitor", "hardware"})
    return (hits if useful else []), sorted(groups)


async def read_chat(client, handle, last_id, keywords, seen):
    """Returns (new_rows, max_id, title). Reads the chat and, for a broadcast
    channel, its linked discussion group (comments)."""
    entity = await client.get_entity(handle)
    title = getattr(entity, "title", handle)
    targets = [(entity, handle)]
    if getattr(entity, "broadcast", False):
        try:
            full = await client(GetFullChannelRequest(entity))
            linked = full.full_chat.linked_chat_id
            if linked:
                disc = await client.get_entity(linked)
                targets.append((disc, getattr(disc, "username", None) or handle))
        except Exception:
            pass

    rows, max_id = [], last_id
    since = datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_DAYS)
    recent = datetime.now(timezone.utc) - timedelta(days=3)  # comments window after the first run
    for ent, link_handle in targets:
        is_main = ent is entity
        kwargs = {"limit": MAX_PER_CHAT}
        if is_main and last_id:
            kwargs["min_id"] = last_id
        async for m in client.iter_messages(ent, **kwargs):
            if not m.date:
                break
            cutoff = since if not last_id else (None if is_main else recent)
            if cutoff and m.date < cutoff:
                break
            if is_main:
                max_id = max(max_id, m.id)
            text = (m.message or "").strip()
            if len(text) < MIN_TEXT_LEN:
                continue
            key = f"{link_handle}:{m.id}"
            if key in seen:
                continue
            hits, groups = match(text, keywords)
            if not hits:
                continue
            seen.add(key)
            author = ""
            try:
                s = await m.get_sender()
                if s is not None and getattr(s, "bot", False):
                    continue
                if s is not None and getattr(s, "username", None):
                    author = "@" + s.username
            except Exception:
                pass
            uname = getattr(ent, "username", None)
            link = f"https://t.me/{uname}/{m.id}" if uname else ""
            rows.append([
                key,
                link_handle,
                getattr(ent, "title", title),
                m.id,
                m.date.strftime("%Y-%m-%dT%H:%M:%SZ"),
                author,
                text[:TEXT_LIMIT],
                link,
                ", ".join(hits[:8]) + (f" [{', '.join(groups)}]" if groups else ""),
                m.reply_to_msg_id or "",
                "",  # processed  (filled by the Claude agent)
                "",  # lead_id    (filled by the Claude agent)
                "",  # agent_note (filled by the Claude agent)
            ])
    return rows, max_id, title


async def main():
    print("config...", flush=True)
    cfg = call("get_config")
    print("channels:", len(cfg["channels"]), flush=True)
    keywords = [(str(k["keyword"]).strip().lower(), str(k.get("group", "")).strip()) for k in cfg["keywords"]]
    keywords = [k for k in keywords if k[0]]
    seen = set(cfg["seen"])
    ok = failed = new_total = 0
    errors = []

    print("connecting telegram...", flush=True)
    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        print("connected", flush=True)
        for ch in cfg["channels"]:
            row = ch["_row"]
            handle = str(ch.get("handle", "")).strip().lstrip("@").replace("https://t.me/", "")
            enabled = str(ch.get("enabled", "")).strip().upper() == "TRUE"
            if not handle or not enabled:
                continue
            try:
                last_id = int(ch.get("last_msg_id") or 0)
            except ValueError:
                last_id = 0
            print("reading", handle, flush=True)
            try:
                rows, max_id, title = await read_chat(client, handle, last_id, keywords, seen)
                if rows:
                    call("append_messages", rows=rows)
                values = {"last_msg_id": max_id, "last_checked": now_iso(), "status": f"ok, +{len(rows)}"}
                if not ch.get("title"):
                    values["title"] = title
                call("update_channel", row=row, values=values)
                ok += 1
                new_total += len(rows)
            except FloodWaitError as e:
                errors.append(f"{handle}: flood wait {e.seconds}s")
                call("update_channel", row=row, values={"last_checked": now_iso(), "status": f"flood wait {e.seconds}s"})
                failed += 1
                if e.seconds > 300:
                    break
                await asyncio.sleep(e.seconds)
            except (UsernameInvalidError, UsernameNotOccupiedError, ChannelPrivateError, ValueError) as e:
                call("update_channel", row=row, values={"last_checked": now_iso(), "status": f"не найден или закрыт: {type(e).__name__}"})
                failed += 1
            except Exception as e:  # keep going with the other chats
                errors.append(f"{handle}: {type(e).__name__}: {e}"[:200])
                try:
                    call("update_channel", row=row, values={"last_checked": now_iso(), "status": f"ошибка: {type(e).__name__}"})
                except Exception:
                    pass
                failed += 1
            await asyncio.sleep(PAUSE_BETWEEN_CHATS)

    call("append_log", row=[now_iso(), ok, failed, new_total, " | ".join(errors)[:1000]])
    print(f"{now_iso()} ok={ok} failed={failed} new={new_total}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(1)
    except EOFError:
        # no valid session and no terminal to ask for the phone code (e.g. in GitHub Actions)
        print("Сессия Telegram недействительна или не задана. Создайте TG_SESSION_STRING заново: python3 make_session.py")
        sys.exit(3)
