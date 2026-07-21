import os
import json
import asyncio
import logging
import re
import tempfile
import mimetypes
from deep_translator import GoogleTranslator
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.network import ConnectionTcpIntermediate
from telethon.sessions import StringSession

# ==========================================
API_ID    = 39644223
API_HASH  = "ceb32e1fd32532a6771756556cc617a2"
BOT_TOKEN = "8759071197:AAHbp2Ivs64k6OgIXUcEvLO471tEOt6eMRs"

# generate_session.py ilə BİR DƏFƏ yaradılıb GitHub Secrets-ə (TG_SESSION adı ilə)
# əlavə olunmalıdır — bu olmadan CI-da interaktiv login mümkün deyil (EOFError).
TG_SESSION = os.environ.get("TG_SESSION", "").strip()

CHANNELS = [
    {"source": -1001099250240, "target": -1003929029095},
    {"source": -1001111348665, "target": -1003996927324},
    {"source": -1001676275372, "target": -1003756746798},
    # ↓ yeni əlavə olunan kanallar
    {"source": -1001860107178, "target": -1003987436790},  # geopolitics_prime
    {"source": -1001330445004, "target": -1004402797222},  # DDrobnitski
    {"source": -1001626824086, "target": -1004491684666},  # Middle_East_Spectator
    {"source": -1001478765631, "target": -1003530398509},  # yurasumy
]

# === "QIZIL ORTA" — sürət vs spam qorxusu ===
SEND_DELAY = 1.2
MAX_FLOOD_RETRY = 2

FIRST_RUN_LOOKBACK_MINUTES = 15
FIRST_RUN_MAX_MESSAGES = 50

EDIT_SYNC_CHECK = 40
MSG_MAP_MAX_SIZE = 300

STATE_FILE = "state.json"
LEGACY_STATE_FILE = "last_ids.txt"
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


def translate(text: str) -> str:
    """Yalnız Google Translate ilə tərcümə edir."""
    try:
        return GoogleTranslator(source="auto", target="az").translate(text)
    except Exception as e:
        log.info(f"❌ Google Translate xətası: {e}")
        return text


# ---------- LİNKLƏRİ QORUMA ----------
URL_PATTERN = re.compile(r'(https?://\S+|www\.\S+)')


def protect_urls(text: str):
    urls = []

    def _replace(m):
        urls.append(m.group(0))
        return f"XLINKX{len(urls) - 1}X"

    protected = URL_PATTERN.sub(_replace, text)
    return protected, urls


def restore_urls(text: str, urls: list) -> str:
    for i, url in enumerate(urls):
        text = re.sub(rf'XLINKX\s*{i}\s*X', url, text, flags=re.IGNORECASE)
    return text


def extract_hidden_links(msg) -> list:
    links = []
    if getattr(msg, "entities", None):
        for e in msg.entities:
            if type(e).__name__ == "MessageEntityTextUrl":
                url = getattr(e, 'url', None)
                if url and 'telegra.ph' not in url and 't.me' not in url and url not in links:
                    links.append(url)
    return links


def translate_preserving_links(msg, text: str) -> str:
    if not text:
        return ""
    protected, urls = protect_urls(text)
    try:
        translated = translate(protected)
    except Exception as e:
        log.info(f"❌ Tərcümə xətası: {e}")
        translated = protected
    translated = restore_urls(translated, urls)

    hidden = [u for u in extract_hidden_links(msg) if u not in urls]
    if hidden:
        translated += "\n\n🔗 " + "\n🔗 ".join(hidden)
    return translated


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'\n*[@\w].*?\|.*?(\|.*?)*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'https?://telegra\.ph\S*', '', text)
    text = re.sub(r'https?://t\.me\S*', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ---------- STATE (last_id + mesaj uyğunluq xəritəsi, JSON) ----------
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def get_channel_state(state: dict, source: int) -> dict:
    key = str(source)
    if key not in state:
        state[key] = {"last_id": None, "msgs": {}}
    return state[key]


def migrate_legacy_state(state: dict):
    if os.path.exists(LEGACY_STATE_FILE):
        with open(LEGACY_STATE_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    k, v = line.split("=")
                    ch = get_channel_state(state, int(k))
                    ch["last_id"] = int(v)
        log.info("♻️  Köhnə last_ids.txt formatından state.json-a keçirildi.")


def remember_message(state: dict, source: int, src_id: int, target_msg, edit_date):
    ch = get_channel_state(state, source)
    ch["msgs"][str(src_id)] = {
        "tid": target_msg.id,
        "ed": edit_date.isoformat() if edit_date else None,
    }
    if len(ch["msgs"]) > MSG_MAP_MAX_SIZE:
        oldest = sorted(ch["msgs"].keys(), key=lambda x: int(x))[: len(ch["msgs"]) - MSG_MAP_MAX_SIZE]
        for k in oldest:
            del ch["msgs"][k]


if not TG_SESSION:
    raise SystemExit(
        "❌ TG_SESSION tapılmadı. Əvvəlcə generate_session.py-i öz kompüterinizdə işə salıb "
        "çıxan sətri GitHub Secrets-ə TG_SESSION adı ilə əlavə edin."
    )

user_client = TelegramClient(StringSession(TG_SESSION), API_ID, API_HASH, connection=ConnectionTcpIntermediate)
bot_client  = TelegramClient("bot_session",  API_ID, API_HASH, connection=ConnectionTcpIntermediate)


async def send_safe(source_msg, translated_text: str, target: int, _retry: int = 0):
    media = source_msg.media
    web_url = None
    temp_path = None

    if media and type(media).__name__ == "MessageMediaWebPage":
        if hasattr(media, 'webpage') and hasattr(media.webpage, 'url'):
            web_url = media.webpage.url
            if 'telegra.ph' not in web_url and 't.me' not in web_url:
                translated_text = translated_text + f"\n\n🔗 {web_url}"
        media = None
    elif media and type(media).__name__ in {"MessageMediaUnsupported", "MessageMediaPoll",
                                            "MessageMediaGame", "MessageMediaGeo",
                                            "MessageMediaContact", "MessageMediaInvoice",
                                            "MessageMediaStory"}:
        media = None

    try:
        if media:
            ext = ".tmp"
            filename = None
            is_voice = False
            is_round = False
            media_type = type(media).__name__

            if media_type == "MessageMediaDocument":
                doc = getattr(media, 'document', None)
                if doc:
                    attrs = getattr(doc, 'attributes', [])
                    mime = getattr(doc, 'mime_type', '')
                    for a in attrs:
                        if hasattr(a, 'file_name') and a.file_name:
                            filename = a.file_name
                            break
                    is_voice = any(type(a).__name__ == 'DocumentAttributeAudio'
                                   and getattr(a, 'voice', False) for a in attrs)
                    is_round = any(type(a).__name__ == 'DocumentAttributeVideo'
                                   and getattr(a, 'round_message', False) for a in attrs)
                    if filename:
                        ext = os.path.splitext(filename)[1] or '.tmp'
                    elif mime:
                        ext = mimetypes.guess_extension(mime) or '.bin'

            elif media_type == "MessageMediaPhoto":
                ext = ".jpg"

            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                temp_path = tmp.name

            downloaded = await user_client.download_media(media, file=temp_path)

            if not downloaded or not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
                log.info(f"⚠️  Media endirilmədi (ID: {source_msg.id}), yalnız mətn göndərilir...")
                if translated_text:
                    sent = await bot_client.send_message(target, translated_text, link_preview=False)
                    log.info(f"✅  Yalnız text göndərildi (ID: {source_msg.id})")
                    return sent
                return None

            size_mb = os.path.getsize(temp_path) / (1024 * 1024)
            if size_mb > 49:
                log.info(f"⚠️  Fayl çox böyükdür ({size_mb:.1f}MB), yalnız mətn göndərilir (ID: {source_msg.id})")
                if translated_text:
                    return await bot_client.send_message(target, translated_text, link_preview=False)
                return None

            sent = await bot_client.send_file(
                target,
                file=temp_path,
                caption=translated_text[:1024] if translated_text else None,
                force_document=False,
                voice_note=is_voice,
                video_note=is_round,
            )

        elif translated_text:
            sent = await bot_client.send_message(target, translated_text, link_preview=False)
        else:
            log.info(f"⚠️  Boş mesaj, ötürülür (ID: {source_msg.id})")
            return None

        log.info(f"✅ Göndərildi (ID: {source_msg.id}) | {datetime.now().strftime('%H:%M:%S')}")
        return sent

    except FloodWaitError as e:
        wait_s = e.seconds + 2
        log.info(f"⏳ LIMIT: {wait_s} saniyə gözlənilir... (cəhd {_retry + 1})")
        await asyncio.sleep(wait_s)
        if _retry < MAX_FLOOD_RETRY:
            return await send_safe(source_msg, translated_text, target, _retry=_retry + 1)
        log.info(f"❌ Flood limiti dəfələrlə keçdi, mesaj ötürüldü (ID: {source_msg.id})")
        return None
    except Exception as e:
        log.info(f"❌ XƏTA (ID: {source_msg.id}): {e}")
        return None
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


async def sync_edits_and_deletes(source: int, target: int, state: dict):
    ch = get_channel_state(state, source)
    if not ch["msgs"]:
        return

    ids_to_check = sorted((int(k) for k in ch["msgs"].keys()), reverse=True)[:EDIT_SYNC_CHECK]
    if not ids_to_check:
        return

    try:
        results = await user_client.get_messages(source, ids=ids_to_check)
    except Exception as e:
        log.info(f"⚠️  Redaktə/silinmə yoxlaması alınmadı: {e}")
        return

    if not isinstance(results, list):
        results = [results]

    for src_id, msg in zip(ids_to_check, results):
        entry = ch["msgs"].get(str(src_id))
        if entry is None:
            continue

        if msg is None:
            try:
                await bot_client.delete_messages(target, entry["tid"])
                log.info(f"🗑️  Silindi (mənbə ID: {src_id})")
            except Exception as e:
                log.info(f"❌ Silmə sinxronizasiya xətası (ID: {src_id}): {e}")
            del ch["msgs"][str(src_id)]
            continue

        new_ed = msg.edit_date.isoformat() if msg.edit_date else None
        if new_ed != entry.get("ed"):
            try:
                text = clean_text(msg.text or "")
                translated = translate_preserving_links(msg, text) if text else ""
                date_str = msg.date.strftime("%d.%m.%Y %H:%M")
                final_text = f"{translated}\n\n📅 {date_str} (redaktə edilib)" if translated else None
                if final_text:
                    await bot_client.edit_message(target, entry["tid"], final_text, link_preview=False)
                    log.info(f"✏️  Redaktə sinxronlaşdırıldı (mənbə ID: {src_id})")
                entry["ed"] = new_ed
            except Exception as e:
                log.info(f"❌ Redaktə sinxronizasiya xətası (ID: {src_id}): {e}")


async def process_channel(source: int, target: int, state: dict):
    log.info(f"\n📡 {source} → {target}")
    ch = get_channel_state(state, source)

    await sync_edits_and_deletes(source, target, state)
    save_state(state)

    last_id = ch["last_id"]

    if last_id is None:
        log.info(f"🆕 İlk işə düşmə — son {FIRST_RUN_LOOKBACK_MINUTES} dəqiqənin mesajları göndərilir...")
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=FIRST_RUN_LOOKBACK_MINUTES)
        messages = []
        async for msg in user_client.iter_messages(source, limit=FIRST_RUN_MAX_MESSAGES, reverse=False):
            if not msg.action and (msg.text or msg.media) and msg.date > cutoff:
                messages.append(msg)
        messages.reverse()

        last_sent_id = 0
        async for msg in user_client.iter_messages(source, limit=1):
            last_sent_id = msg.id
        ch["last_id"] = last_sent_id
        save_state(state)

        log.info(f"📋 {len(messages)} mesaj tapıldı.")
        for msg in messages:
            try:
                text = clean_text(msg.text or "")
                translated = translate_preserving_links(msg, text) if text else ""
                date_str = msg.date.strftime("%d.%m.%Y %H:%M")
                final_text = f"{translated}\n\n📅 {date_str}" if translated else ""
                sent = await send_safe(msg, final_text, target)
                if sent:
                    remember_message(state, source, msg.id, sent, msg.edit_date)
                    if msg.id > ch["last_id"]:
                        ch["last_id"] = msg.id
                    save_state(state)
                    await asyncio.sleep(SEND_DELAY)
            except Exception as e:
                log.info(f"❌ Mesaj emalı xətası (ID: {msg.id}): {e}")
                continue
        return

    messages = []
    async for msg in user_client.iter_messages(source, min_id=last_id, reverse=True):
        if not msg.action and (msg.text or msg.media):
            messages.append(msg)

    log.info(f"📋 {len(messages)} yeni mesaj tapıldı (orijinal ardıcıllıqla göndəriləcək).")

    for msg in messages:
        try:
            text = clean_text(msg.text or "")
            translated = translate_preserving_links(msg, text) if text else ""
            date_str = msg.date.strftime("%d.%m.%Y %H:%M")
            final_text = f"{translated}\n\n📅 {date_str}" if translated else ""

            sent = await send_safe(msg, final_text, target)
            if sent:
                remember_message(state, source, msg.id, sent, msg.edit_date)
                ch["last_id"] = msg.id
                save_state(state)
                await asyncio.sleep(SEND_DELAY)
        except Exception as e:
            log.info(f"❌ Mesaj emalı xətası (ID: {msg.id}): {e}")
            continue


async def main():
    await user_client.start()
    await bot_client.start(bot_token=BOT_TOKEN)
    log.info("🚀 Bot işə düşdü!")

    state = load_state()
    if not state:
        migrate_legacy_state(state)

    for pair in CHANNELS:
        try:
            await process_channel(pair["source"], pair["target"], state)
        except Exception as e:
            log.info(f"❌ KANAL XƏTASI ({pair['source']} → {pair['target']}): {e}")
            continue

    save_state(state)
    log.info("✅ Bot dayandı.")
    await user_client.disconnect()
    await bot_client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
