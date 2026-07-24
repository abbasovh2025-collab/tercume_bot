import os
import json
import time
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
from telethon.helpers import add_surrogate
from telethon.tl.types import MessageEntityCustomEmoji, DocumentAttributeVideo
from zoneinfo import ZoneInfo

# Telegram mesajların vaxtını UTC saxlayır — çap edərkən Bakı vaxtına çeviririk
LOCAL_TZ = ZoneInfo("Asia/Baku")

# ==========================================
API_ID    = 39644223
API_HASH  = "ceb32e1fd32532a6771756556cc617a2"
BOT_TOKEN = "8759071197:AAHbp2Ivs64k6OgIXUcEvLO471tEOt6eMRs"

# generate_session.py ilə BİR DƏFƏ yaradılıb GitHub Secrets-ə (TG_SESSION adı ilə)
# əlavə olunmalıdır — bu olmadan CI-da interaktiv login mümkün deyil (EOFError).
TG_SESSION = os.environ.get("TG_SESSION", "").strip()

# "src" — kanalın əsas dili. "auto" da işləyir, amma konkret dil yazsanız
# (rus kanal üçün "ru", ingilis üçün "en") Google Translate-in kontekst
# səhvləri xeyli azalır. Bilmirsinizsə "auto" saxlayın.
CHANNELS = [
    {"source": -1001099250240, "target": -1003929029095, "src": "auto"},
    {"source": -1001111348665, "target": -1003996927324, "src": "auto"},
    {"source": -1001676275372, "target": -1003756746798, "src": "auto"},
    {"source": -1001860107178, "target": -1003987436790, "src": "en"},  # geopolitics_prime
    {"source": -1001330445004, "target": -1004402797222, "src": "ru"},  # DDrobnitski
    {"source": -1001626824086, "target": -1004491684666, "src": "en"},  # Middle_East_Spectator
    {"source": -1001478765631, "target": -1003530398509, "src": "ru"},  # yurasumy
]
SOURCE_LANG = {c["source"]: c.get("src", "auto") for c in CHANNELS}

# === "QIZIL ORTA" — sürət vs spam qorxusu ===
SEND_DELAY = 1.2
MAX_FLOOD_RETRY = 2

FIRST_RUN_LOOKBACK_MINUTES = 15
FIRST_RUN_MAX_MESSAGES = 50

EDIT_SYNC_CHECK = 40
MSG_MAP_MAX_SIZE = 300

MAX_CHUNK_CHARS = 3500
CAPTION_LIMIT = 1024

STATE_FILE = "state.json"
LEGACY_STATE_FILE = "last_ids.txt"
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


def _translate_once(text: str, src: str) -> str:
    """Google Translate-ə 1 sorğu — şəbəkə/rate-limit xətalarına görə 2 cəhd edir
    (bug #6 fix: bəzi mesajlar tərcümə olunmadan orijinal dildə qalırdı)."""
    last_err = None
    for attempt in range(2):
        try:
            return GoogleTranslator(source=src, target="az").translate(text)
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(1.5)
    log.info(f"❌ Google Translate xətası (2 cəhddən sonra): {last_err}")
    return text


def translate(text: str, src: str = "auto") -> str:
    """Uzun mətnlər limitə görə hissə-hissə tərcümə olunur ki, yarımçıq
    kəsilmə baş verməsin."""
    if not text:
        return ""
    if len(text) <= MAX_CHUNK_CHARS:
        return _translate_once(text, src)

    chunks = []
    current = ""
    for line in text.split("\n"):
        candidate = (current + "\n" + line) if current else line
        if len(candidate) > MAX_CHUNK_CHARS and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)

    translated_chunks = []
    for chunk in chunks:
        translated_chunks.append(_translate_once(chunk, src) if chunk.strip() else chunk)
    return "\n".join(translated_chunks)


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


def extract_custom_emojis(msg):
    """Premium/custom emojiləri (görünən simvol + document_id) çıxarır."""
    result = []
    if getattr(msg, "entities", None) and msg.text:
        surrogate_text = add_surrogate(msg.text)
        for e in msg.entities:
            if type(e).__name__ == "MessageEntityCustomEmoji":
                try:
                    char = surrogate_text[e.offset:e.offset + e.length]
                except Exception:
                    continue
                doc_id = getattr(e, "document_id", None)
                if char and doc_id:
                    result.append((char, doc_id))
    return result


def translate_preserving_links(msg, text: str, src: str = "auto") -> str:
    if not text:
        return ""
    protected, urls = protect_urls(text)
    translated = translate(protected, src=src)
    translated = restore_urls(translated, urls)

    hidden = [u for u in extract_hidden_links(msg) if u not in urls]
    if hidden:
        translated += "\n\n🔗 " + "\n🔗 ".join(hidden)
    return translated


def build_final_message(msg, translated: str, date_str: str, extra_suffix: str = ""):
    """(mətn, formatting_entities) tuple-i qaytarır; premium emojilər sona əlavə olunur."""
    body = f"{translated}\n\n📅 {date_str}{extra_suffix}" if translated else ""
    if not body:
        return body, None

    emojis = extract_custom_emojis(msg)
    entities = []
    if emojis:
        base_surrogate = add_surrogate(body)
        offset = len(base_surrogate) + 1
        pieces = []
        for char, doc_id in emojis:
            pieces.append(char)
            length = len(char)
            entities.append(MessageEntityCustomEmoji(offset=offset, length=length, document_id=doc_id))
            offset += length + 1
        body = body + " " + " ".join(pieces)

    return body, (entities or None)


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'\n*[@\w].*?\|.*?(\|.*?)*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'https?://telegra\.ph\S*', '', text)
    text = re.sub(r'https?://t\.me\S*', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ---------- MEDİA METADATA (bug #1/#5 fix: GIF/video native atributları qorunur) ----------
def normalize_attrs(attrs):
    """Video atributlarında supports_streaming məcburi True edilir ki,
    Telegram-da video ilk vurmadan yüklənsin/oynasın."""
    new_attrs = []
    for a in attrs:
        if isinstance(a, DocumentAttributeVideo) and not getattr(a, 'supports_streaming', False):
            new_attrs.append(DocumentAttributeVideo(
                duration=a.duration, w=a.w, h=a.h,
                round_message=getattr(a, 'round_message', False),
                supports_streaming=True,
            ))
        else:
            new_attrs.append(a)
    return new_attrs


def media_info(media):
    """(ext, attrs) qaytarır. attrs — orijinal Document-in atributlarıdır
    (GIF/animasiya, video-stream, səsli-not, dairəvi video flag-ları daxil
    olmaqla) ki, Telegram-da fayl orijinaldakı kimi render olunsun."""
    ext = ".tmp"
    attrs = None
    media_type = type(media).__name__

    if media_type == "MessageMediaDocument":
        doc = getattr(media, 'document', None)
        if doc:
            raw_attrs = getattr(doc, 'attributes', []) or []
            attrs = normalize_attrs(raw_attrs)
            filename = None
            for a in raw_attrs:
                if hasattr(a, 'file_name') and a.file_name:
                    filename = a.file_name
                    break
            mime = getattr(doc, 'mime_type', '')
            if filename:
                ext = os.path.splitext(filename)[1] or '.tmp'
            elif mime:
                ext = mimetypes.guess_extension(mime) or '.bin'
    elif media_type == "MessageMediaPhoto":
        ext = ".jpg"

    return ext, attrs


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


def tid_list(entry: dict) -> list:
    tid = entry.get("tid")
    if isinstance(tid, list):
        return tid
    return [tid] if tid is not None else []


def remember_group(state: dict, source: int, group_ids: list, sent_ids: list, edit_date):
    """Bir albomun (və ya tək mesajın) BÜTÜN orijinal ID-lərini eyni hədəf
    ID-siyahısına bağlayır — silmə/redaktə/idempotency bunun üzərindən işləyir."""
    ch = get_channel_state(state, source)
    for gid in group_ids:
        ch["msgs"][str(gid)] = {
            "tid": sent_ids,
            "ed": edit_date.isoformat() if edit_date else None,
        }
    if len(ch["msgs"]) > MSG_MAP_MAX_SIZE:
        oldest = sorted(ch["msgs"].keys(), key=lambda x: int(x))[: len(ch["msgs"]) - MSG_MAP_MAX_SIZE]
        for k in oldest:
            del ch["msgs"][k]


def already_sent(state: dict, source: int, src_id: int) -> bool:
    ch = get_channel_state(state, source)
    return str(src_id) in ch["msgs"]


if not TG_SESSION:
    raise SystemExit(
        "❌ TG_SESSION tapılmadı. Əvvəlcə generate_session.py-i öz kompüterinizdə işə salıb "
        "çıxan sətri GitHub Secrets-ə TG_SESSION adı ilə əlavə edin."
    )

user_client = TelegramClient(StringSession(TG_SESSION), API_ID, API_HASH, connection=ConnectionTcpIntermediate)
bot_client  = TelegramClient("bot_session",  API_ID, API_HASH, connection=ConnectionTcpIntermediate)


def _extract_web_url(source_msg):
    media = source_msg.media
    if media and type(media).__name__ == "MessageMediaWebPage":
        if hasattr(media, 'webpage') and hasattr(media.webpage, 'url'):
            url = media.webpage.url
            if 'telegra.ph' not in url and 't.me' not in url:
                return url
    return None


def _usable_media(source_msg):
    media = source_msg.media
    if not media:
        return None
    mt = type(media).__name__
    if mt in {"MessageMediaWebPage", "MessageMediaUnsupported", "MessageMediaPoll",
              "MessageMediaGame", "MessageMediaGeo", "MessageMediaContact",
              "MessageMediaInvoice", "MessageMediaStory"}:
        return None
    return media


async def send_safe(source_msg, final_text: str, entities, target: int, _retry: int = 0):
    web_url = _extract_web_url(source_msg)
    if web_url:
        final_text = final_text + f"\n\n🔗 {web_url}"
    media = _usable_media(source_msg)
    temp_path = None

    try:
        if media:
            ext, attrs = media_info(media)
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                temp_path = tmp.name

            downloaded = await user_client.download_media(media, file=temp_path)

            if not downloaded or not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
                log.info(f"⚠️  Media endirilmədi (ID: {source_msg.id}), yalnız mətn göndərilir...")
                if final_text:
                    sent = await bot_client.send_message(target, final_text, link_preview=True,
                                                           formatting_entities=entities)
                    return sent
                return None

            size_mb = os.path.getsize(temp_path) / (1024 * 1024)
            if size_mb > 49:
                log.info(f"⚠️  Fayl çox böyükdür ({size_mb:.1f}MB), yalnız mətn göndərilir (ID: {source_msg.id})")
                if final_text:
                    return await bot_client.send_message(target, final_text, link_preview=True,
                                                           formatting_entities=entities)
                return None

            if final_text and len(final_text) > CAPTION_LIMIT:
                media_msg = await bot_client.send_file(target, file=temp_path, caption=None, attributes=attrs)
                await asyncio.sleep(0.4)
                text_msg = await bot_client.send_message(target, final_text, link_preview=True,
                                                           formatting_entities=entities)
                return [media_msg, text_msg]

            sent = await bot_client.send_file(
                target, file=temp_path,
                caption=final_text if final_text else None,
                formatting_entities=entities,
                attributes=attrs,
            )

        elif final_text:
            sent = await bot_client.send_message(target, final_text, link_preview=True,
                                                   formatting_entities=entities)
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
            return await send_safe(source_msg, final_text, entities, target, _retry=_retry + 1)
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


async def send_album(group, final_text, entities, target):
    """BUG #2/#3/#4 FIX: bir neçə şəkilli/videolu postu (album) TƏK qruplaşmış
    mesaj kimi göndərir — hər biri ayrı-ayrı 4 dəfə getmək əvəzinə."""
    temp_paths = []
    attrs_list = []
    try:
        for m in group:
            media = _usable_media(m)
            if not media:
                continue
            ext, attrs = media_info(media)
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                path = tmp.name
            downloaded = await user_client.download_media(media, file=path)
            if downloaded and os.path.exists(path) and os.path.getsize(path) > 0:
                temp_paths.append(path)
                attrs_list.append(attrs)
            else:
                try:
                    os.remove(path)
                except Exception:
                    pass

        if not temp_paths:
            if final_text:
                return await bot_client.send_message(target, final_text, link_preview=True,
                                                       formatting_entities=entities)
            return None

        short_caption = final_text if final_text and len(final_text) <= CAPTION_LIMIT else None
        sent = await bot_client.send_file(target, file=temp_paths, caption=short_caption)
        result = list(sent) if isinstance(sent, list) else [sent]

        if final_text and len(final_text) > CAPTION_LIMIT:
            await asyncio.sleep(0.4)
            text_msg = await bot_client.send_message(target, final_text, link_preview=True,
                                                       formatting_entities=entities)
            result.append(text_msg)

        return result
    except FloodWaitError as e:
        wait_s = e.seconds + 2
        log.info(f"⏳ LIMIT (albom): {wait_s} saniyə gözlənilir...")
        await asyncio.sleep(wait_s)
        return None
    except Exception as e:
        log.info(f"❌ Albom göndərmə xətası: {e}")
        return None
    finally:
        for p in temp_paths:
            try:
                os.remove(p)
            except Exception:
                pass


def group_messages(messages):
    """Ardıcıl mesajları grouped_id-ə görə albom halında qruplaşdırır
    (orijinal kanaldakı kimi bir postda bir neçə şəkil/video)."""
    groups = []
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        gid = getattr(m, "grouped_id", None)
        if gid:
            group = [m]
            j = i + 1
            while j < n and getattr(messages[j], "grouped_id", None) == gid:
                group.append(messages[j])
                j += 1
            groups.append(group)
            i = j
        else:
            groups.append([m])
            i += 1
    return groups


async def process_group(group, source: int, target: int, state: dict, src_lang: str):
    """Bir qrupu (tək mesaj və ya albom) tərcümə edib göndərir.
    Uğurlu olarsa qrupun son mesaj ID-sini qaytarır (last_id irəlilətmək üçün)."""
    rep_id = group[0].id
    if already_sent(state, source, rep_id):
        return group[-1].id
    try:
        text_msg = next((m for m in group if m.text), group[0])
        text = clean_text(text_msg.text or "")
        translated = translate_preserving_links(text_msg, text, src=src_lang) if text else ""
        date_str = text_msg.date.astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")
        final_text, entities = build_final_message(text_msg, translated, date_str)

        if len(group) == 1:
            sent = await send_safe(group[0], final_text, entities, target)
        else:
            sent = await send_album(group, final_text, entities, target)

        if sent:
            group_ids = [m.id for m in group]
            sent_ids = [m.id for m in sent] if isinstance(sent, list) else [sent.id]
            remember_group(state, source, group_ids, sent_ids, text_msg.edit_date)
            save_state(state)
            await asyncio.sleep(SEND_DELAY)
            return group[-1].id
    except Exception as e:
        log.info(f"❌ Mesaj emalı xətası (qrup başlanğıc ID: {rep_id}): {e}")
    return None


async def sync_edits_and_deletes(source: int, target: int, state: dict):
    ch = get_channel_state(state, source)
    if not ch["msgs"]:
        return

    src_lang = SOURCE_LANG.get(source, "auto")
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

        ids = tid_list(entry)
        if msg is None:
            try:
                if ids:
                    await bot_client.delete_messages(target, ids)
                log.info(f"🗑️  Silindi (mənbə ID: {src_id})")
            except Exception as e:
                log.info(f"❌ Silmə sinxronizasiya xətası (ID: {src_id}): {e}")
            del ch["msgs"][str(src_id)]
            continue

        new_ed = msg.edit_date.isoformat() if msg.edit_date else None
        if new_ed != entry.get("ed") and ids:
            try:
                text = clean_text(msg.text or "")
                translated = translate_preserving_links(msg, text, src=src_lang) if text else ""
                date_str = msg.date.astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")
                final_text, entities = build_final_message(msg, translated, date_str, extra_suffix=" (redaktə edilib)")
                if final_text:
                    await bot_client.edit_message(target, ids[-1], final_text, link_preview=True,
                                                    formatting_entities=entities)
                    log.info(f"✏️  Redaktə sinxronlaşdırıldı (mənbə ID: {src_id})")
                entry["ed"] = new_ed
            except Exception as e:
                log.info(f"❌ Redaktə sinxronizasiya xətası (ID: {src_id}): {e}")


async def process_channel(source: int, target: int, state: dict):
    log.info(f"\n📡 {source} → {target}")
    ch = get_channel_state(state, source)
    src_lang = SOURCE_LANG.get(source, "auto")

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

        groups = group_messages(messages)
        log.info(f"📋 {len(messages)} mesaj tapıldı ({len(groups)} qrup).")
        for group in groups:
            new_last = await process_group(group, source, target, state, src_lang)
            if new_last is not None and new_last > (ch["last_id"] or 0):
                ch["last_id"] = new_last
                save_state(state)
        return

    messages = []
    async for msg in user_client.iter_messages(source, min_id=last_id, reverse=True):
        if not msg.action and (msg.text or msg.media):
            messages.append(msg)

    groups = group_messages(messages)
    log.info(f"📋 {len(messages)} yeni mesaj tapıldı ({len(groups)} qrup, orijinal ardıcıllıqla).")

    for group in groups:
        new_last = await process_group(group, source, target, state, src_lang)
        if new_last is not None:
            ch["last_id"] = new_last
            save_state(state)


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
