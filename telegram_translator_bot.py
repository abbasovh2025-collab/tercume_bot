import os
import json
import time
import asyncio
import logging
import re
import tempfile
import mimetypes
from groq import Groq
from deep_translator import GoogleTranslator
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.network import ConnectionTcpIntermediate
from telethon.sessions import StringSession
from telethon.helpers import add_surrogate, del_surrogate
from telethon.tl.types import MessageEntityCustomEmoji, DocumentAttributeVideo
from zoneinfo import ZoneInfo

# Telegram mesajların vaxtını UTC saxlayır — çap edərkən Bakı vaxtına çeviririk
LOCAL_TZ = ZoneInfo("Asia/Baku")

# ==========================================
API_ID    = 39644223
API_HASH  = "ceb32e1fd32532a6771756556cc617a2"
BOT_TOKEN = "8759071197:AAHbp2Ivs64k6OgIXUcEvLO471tEOt6eMRs"

TG_SESSION = os.environ.get("TG_SESSION", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()

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

SEND_DELAY = 1.2
MAX_FLOOD_RETRY = 2

FIRST_RUN_LOOKBACK_MINUTES = 15
FIRST_RUN_MAX_MESSAGES = 50

EDIT_SYNC_CHECK = 40
MSG_MAP_MAX_SIZE = 400

MAX_CHUNK_CHARS = 3500
CAPTION_LIMIT = 1024

STATE_FILE = "state.json"
LEGACY_STATE_FILE = "last_ids.txt"

# Alternative Groq Models for fallback
GROQ_MODELS = [
    "llama-3.3-70b-versatile",  # Əsas güclü model (70B)
    "llama-3.1-8b-instant",     # Çox sürətli və yüksək limitli (8B)
    "llama3-70b-8192",          # Ehtiyat 70B
    "gemma2-9b-it"              # Google-un ehtiyat modeli
]
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None


def _translate_groq(text: str, src: str) -> str:
    if not groq_client:
        raise ValueError("GROQ_API_KEY tapılmadı.")

    system_prompt = (
        "You are an expert translator. Translate the input text into natural, fluent Azerbaijani.\n"
        "STRICT RULES:\n"
        "1. Output ONLY the final Azerbaijani translation without any introductory or concluding text, notes, or explanations.\n"
        "2. Keep original line breaks, formatting, emojis, and special structure intact.\n"
        "3. DO NOT change, translate, or remove link placeholders like XLINKX0X, XLINKX1X, etc."
    )

    last_exc = None
    for model_name in GROQ_MODELS:
        try:
            response = groq_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text}
                ],
                temperature=0.2,
                max_tokens=2048,
            )
            res = response.choices[0].message.content.strip()
            if res:
                return res
        except Exception as e:
            last_exc = e
            log.info(f"⚠️ Groq model ({model_name}) xətası: {e}. Növbəti model yoxlanılır...")

    raise ValueError(f"Bütün Groq modelləri xəta verdi: {last_exc}")


def _translate_google(text: str, src: str) -> str:
    """Ehtiyat tərcüməçi (Google Translate)."""
    source_lang = "auto" if not src or src == "auto" else src
    for attempt in range(2):
        try:
            return GoogleTranslator(source=source_lang, target="az").translate(text)
        except Exception as e:
            if attempt == 0:
                time.sleep(1.5)
            else:
                log.info(f"❌ Google Translate xətası: {e}")
    return text


def _translate_once(text: str, src: str) -> str:
    if GROQ_API_KEY:
        try:
            return _translate_groq(text, src)
        except Exception as e:
            log.info(f"⚠️ Groq xətası ({e}), Google Translate-ə keçilir...")
    return _translate_google(text, src)


def translate(text: str, src: str = "auto") -> str:
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


# ---------- LİNKLƏRİ VƏ FORMATI QORUMA ----------
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
            pieces.append(del_surrogate(char))
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


# ---------- MEDİA VƏ THUMBNAIL EMALI ----------
def normalize_attrs(attrs):
    new_attrs = []
    for a in attrs:
        if isinstance(a, DocumentAttributeVideo):
            new_attrs.append(DocumentAttributeVideo(
                duration=a.duration, w=a.w, h=a.h,
                round_message=getattr(a, 'round_message', False),
                supports_streaming=True,
            ))
        else:
            new_attrs.append(a)
    return new_attrs


def media_info(media):
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
            elif mime.startswith("video/"):
                ext = ".mp4"
            elif mime.startswith("image/"):
                ext = ".jpg"
            elif mime:
                ext = mimetypes.guess_extension(mime) or '.bin'
    elif media_type == "MessageMediaPhoto":
        ext = ".jpg"

    return ext, attrs


async def download_media_with_thumb(msg):
    """Media faylını və video kover şəklini (thumbnail) endirir."""
    media = _usable_media(msg)
    if not media:
        return None, None, None

    ext, attrs = media_info(media)
    temp_media = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    media_path = temp_media.name
    temp_media.close()

    try:
        downloaded = await user_client.download_media(media, file=media_path)
        if not downloaded or not os.path.exists(media_path) or os.path.getsize(media_path) == 0:
            if os.path.exists(media_path):
                os.remove(media_path)
            return None, None, None
    except Exception as e:
        log.info(f"⚠️ Media yükləmə xətası (ID: {msg.id}): {e}")
        if os.path.exists(media_path):
            try:
                os.remove(media_path)
            except Exception:
                pass
        return None, None, None

    thumb_path = None
    if type(media).__name__ == "MessageMediaDocument":
        doc = getattr(media, 'document', None)
        if doc and getattr(doc, 'thumbs', None):
            try:
                temp_thumb = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                thumb_path = temp_thumb.name
                temp_thumb.close()
                downloaded_thumb = await user_client.download_media(doc.thumbs[-1], file=thumb_path)
                if not downloaded_thumb or not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0:
                    if os.path.exists(thumb_path):
                        os.remove(thumb_path)
                    thumb_path = None
            except Exception as e:
                log.info(f"⚠️ Thumbnail yükləmə xətası: {e}")
                if thumb_path and os.path.exists(thumb_path):
                    try:
                        os.remove(thumb_path)
                    except Exception:
                        pass
                thumb_path = None

    return media_path, thumb_path, attrs


# ---------- STATE VƏ DUBİKATLARDAN QORUNMA ----------
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
        state[key] = {"last_id": None, "msgs": {}, "groups": {}}
    if "groups" not in state[key]:
        state[key]["groups"] = {}
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
        log.info("♻️ Köhnə last_ids.txt formatından state.json-a keçirildi.")


def tid_list(entry: dict) -> list:
    tid = entry.get("tid")
    if isinstance(tid, list):
        return tid
    return [tid] if tid is not None else []


def remember_group(state: dict, source: int, group_ids: list, sent_ids: list, edit_date, grouped_id=None):
    ch = get_channel_state(state, source)
    for gid in group_ids:
        ch["msgs"][str(gid)] = {
            "tid": sent_ids,
            "ed": edit_date.isoformat() if edit_date else None,
        }
    if grouped_id:
        ch["groups"][str(grouped_id)] = {
            "tid": sent_ids,
            "date": datetime.now(timezone.utc).isoformat()
        }

    if len(ch["msgs"]) > MSG_MAP_MAX_SIZE:
        oldest = sorted(ch["msgs"].keys(), key=lambda x: int(x))[: len(ch["msgs"]) - MSG_MAP_MAX_SIZE]
        for k in oldest:
            del ch["msgs"][k]

    if len(ch["groups"]) > MSG_MAP_MAX_SIZE:
        oldest_g = list(ch["groups"].keys())[: len(ch["groups"]) - MSG_MAP_MAX_SIZE]
        for k in oldest_g:
            del ch["groups"][k]


def already_sent(state: dict, source: int, msg_id: int, grouped_id=None) -> bool:
    ch = get_channel_state(state, source)
    if str(msg_id) in ch["msgs"]:
        return True
    if grouped_id and str(grouped_id) in ch["groups"]:
        return True
    return False


if not TG_SESSION:
    raise SystemExit(
        "❌ TG_SESSION tapılmadı. Əvvəlcə GitHub Secrets-ə TG_SESSION adı ilə əlavə edin."
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
        final_text = (final_text + f"\n\n🔗 {web_url}") if final_text else f"🔗 {web_url}"

    media_path, thumb_path, attrs = await download_media_with_thumb(source_msg)

    try:
        if media_path:
            size_mb = os.path.getsize(media_path) / (1024 * 1024)
            if size_mb > 49:
                log.info(f"⚠️ Fayl çox böyükdür ({size_mb:.1f}MB), yalnız mətn göndərilir (ID: {source_msg.id})")
                if final_text:
                    return await bot_client.send_message(target, final_text, link_preview=True,
                                                         formatting_entities=entities)
                return None

            if final_text and len(final_text) > CAPTION_LIMIT:
                media_msg = await bot_client.send_file(
                    target, file=media_path, thumb=thumb_path, caption=None, attributes=attrs, supports_streaming=True
                )
                await asyncio.sleep(0.4)
                text_msg = await bot_client.send_message(target, final_text, link_preview=True,
                                                         formatting_entities=entities)
                return [media_msg, text_msg]

            sent = await bot_client.send_file(
                target, file=media_path, thumb=thumb_path,
                caption=final_text if final_text else None,
                formatting_entities=entities,
                attributes=attrs,
                supports_streaming=True
            )
        elif final_text:
            sent = await bot_client.send_message(target, final_text, link_preview=True,
                                                 formatting_entities=entities)
        else:
            log.info(f"⚠️ Boş mesaj, ötürülür (ID: {source_msg.id})")
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
        for p in [media_path, thumb_path]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


async def send_album(group, final_text, entities, target):
    """3-4 fayldan ibarət albomu videoların atributlarını və koverlərin qoruyaraq göndərir."""
    temp_items = []
    file_paths = []
    thumb_paths = []
    attributes_list = []

    try:
        for m in group:
            m_path, t_path, attrs = await download_media_with_thumb(m)
            if m_path:
                temp_items.append((m_path, t_path))
                file_paths.append(m_path)
                thumb_paths.append(t_path)
                attributes_list.append(attrs)

        if not file_paths:
            if final_text:
                return await bot_client.send_message(target, final_text, link_preview=True,
                                                     formatting_entities=entities)
            return None

        # Mətni albomun üzərinə bərkitmək üçün CAPTION_LIMIT (1024) idarə olunur
        album_caption = None
        overflow_text = None

        if final_text:
            if len(final_text) <= CAPTION_LIMIT:
                album_caption = final_text
            else:
                # Albom başlığında yerləşəcək hissə
                cut_pos = final_text.rfind('\n', 0, 1000)
                if cut_pos == -1:
                    cut_pos = final_text.rfind(' ', 0, 1000)
                if cut_pos == -1:
                    cut_pos = 1000

                album_caption = final_text[:cut_pos] + "..."
                overflow_text = "..." + final_text[cut_pos:]

        sent = await bot_client.send_file(
            target,
            file=file_paths,
            caption=album_caption,
            formatting_entities=entities if album_caption == final_text else None,
            thumb=thumb_paths,
            attributes=attributes_list,
            supports_streaming=True
        )
        result = list(sent) if isinstance(sent, list) else [sent]

        # Əgər mətn 1024 simvoldan böyük idisə, qalan hissə albomun altına göndərilir
        if overflow_text:
            await asyncio.sleep(0.4)
            overflow_msg = await bot_client.send_message(target, overflow_text, link_preview=True)
            result.append(overflow_msg)

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
        for m_path, t_path in temp_items:
            for p in [m_path, t_path]:
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass


def group_messages(messages):
    """Bütün mesajları xronoloji ardıcıllıqla və `grouped_id` ilə dürüst qruplaşdırır."""
    messages = sorted(messages, key=lambda x: x.id)
    groups = []
    group_map = {}

    for m in messages:
        gid = getattr(m, "grouped_id", None)
        if gid:
            if gid not in group_map:
                group_map[gid] = []
                groups.append(group_map[gid])
            group_map[gid].append(m)
        else:
            groups.append([m])

    return groups


async def fetch_and_group_messages(source: int, min_id: int, state: dict):
    messages = []
    async for msg in user_client.iter_messages(source, min_id=min_id, reverse=True):
        if not msg.action and (msg.text or msg.media):
            messages.append(msg)

    if not messages:
        return []

    groups = group_messages(messages)
    now = datetime.now(timezone.utc)
    result_groups = []

    for group in groups:
        gid = getattr(group[0], "grouped_id", None)
        if gid:
            # Yarımçıq albomların tam yüklənməsini gözləmək üçün
            last_msg_in_group = group[-1]
            age_sec = (now - last_msg_in_group.date).total_seconds()
            if age_sec < 15 and group == groups[-1]:
                log.info(f"⏳ Qrup media (GroupedID: {gid}) hələ yüklənir, növbəti run-da emal ediləcək.")
                break
        result_groups.append(group)

    return result_groups


async def process_group(group, source: int, target: int, state: dict, src_lang: str):
    rep_id = group[0].id
    gid = getattr(group[0], "grouped_id", None)

    if any(already_sent(state, source, m.id, getattr(m, "grouped_id", None)) for m in group):
        log.info(f"⏭️ Artıq göndərilib, ötürülür (ID: {rep_id})")
        return group[-1].id

    try:
        text_msg = next((m for m in group if m.text and m.text.strip()), group[0])
        text = clean_text(text_msg.text or "")
        translated = translate_preserving_links(text_msg, text, src=src_lang) if text else ""
        date_str = text_msg.date.astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")
        final_text, entities = build_final_message(text_msg, translated, date_str)

        if len(group) == 1:
            sent = await send_safe(group[0], final_text, entities, target)
        else:
            sent = await send_album(group, final_text, entities, target)

        group_ids = [m.id for m in group]
        sent_ids = [m.id for m in sent] if isinstance(sent, list) else ([sent.id] if sent else [])

        remember_group(state, source, group_ids, sent_ids, text_msg.edit_date, grouped_id=gid)
        save_state(state)
        await asyncio.sleep(SEND_DELAY)
        return group[-1].id

    except Exception as e:
        log.info(f"❌ Mesaj emalı xətası (qrup başlanğıc ID: {rep_id}): {e}")
        remember_group(state, source, [m.id for m in group], [], group[0].edit_date, grouped_id=gid)
        save_state(state)
        return group[-1].id


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
        log.info(f"⚠️ Redaktə/silinmə yoxlaması alınmadı: {e}")
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
                log.info(f"🗑️ Silindi (mənbə ID: {src_id})")
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
                    log.info(f"✏️ Redaktə sinxronlaşdırıldı (mənbə ID: {src_id})")
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

    groups = await fetch_and_group_messages(source, last_id, state)
    total_msgs = sum(len(g) for g in groups)
    log.info(f"📋 {total_msgs} yeni mesaj tapıldı ({len(groups)} qrup, orijinal ardıcıllıqla).")

    for group in groups:
        new_last = await process_group(group, source, target, state, src_lang)
        if new_last is not None and new_last > (ch["last_id"] or 0):
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
