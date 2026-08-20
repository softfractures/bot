import os
import json
import asyncio
import logging
import random
import time
import base64
import aiohttp
import google.generativeai as genai
from dotenv import load_dotenv
import websockets
from curl_cffi import requests

# ---- NOWY IMPORT ----
try:
    from PIL import Image
    import io
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("UWAGA: Pillow nie jest zainstalowane – funkcja resize nie będzie działać!")

load_dotenv()

# --------------------------------------------
# CONFIGURATION
# --------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# Multiple owners
OWNER_IDS = [
    "436550942029250570",
    "1505100446891773962"
]

# Spontaneous messages settings
SPONTANEOUS_CHANCE = 0.03
SPONTANEOUS_COOLDOWN = 180
SPONTANEOUS_CHECK_INTERVAL = 60
SPONTANEOUS_CHANNEL_ID = "1458080496369139849"

# Faster delays – dostosowane do szybszego odpowiadania
MIN_REPLY_DELAY = 0.5          # wcześniej 2.0
MAX_REPLY_DELAY = 1.0          # wcześniej 4.0
CHUNK_DELAY = 0.1              # wcześniej 0.5
MAX_MESSAGES_PER_MINUTE = 10   # ustawione na 10 (prośba użytkownika)
HISTORY_CACHE_TTL = 60
MAX_HISTORY_PER_GUILD = 50

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stealth-selfbot")

if not DISCORD_TOKEN or not GEMINI_API_KEY:
    raise RuntimeError("Missing credentials in .env")

# --------------------------------------------
# FETCH LATEST DISCORD BUILD INFO
# --------------------------------------------
def fetch_latest_build_info():
    try:
        url = "https://raw.githubusercontent.com/qoft/discord-api/main/fetch"
        response = requests.get(url, timeout=10, impersonate="chrome120")
        response.raise_for_status()
        data = response.json()
        stable_data = data.get('stable', {})
        build_number = stable_data.get('build_number')
        version = stable_data.get('version')
        if build_number and version:
            log.info(f"Fetched latest build: {build_number} (v{version})")
            return build_number, version
        else:
            log.warning("Could not parse build info, using fallback.")
            return 238248, "1.0.9037"
    except Exception as e:
        log.error(f"Failed to fetch build info: {e}. Using fallback.")
        return 238248, "1.0.9037"

LATEST_BUILD, LATEST_VERSION = fetch_latest_build_info()

# --------------------------------------------
# GEMINI SETUP
# --------------------------------------------
genai.configure(api_key=GEMINI_API_KEY)

DEFAULT_SYSTEM_PROMPT = (
"jesteś wulgarnym agresywnym botem discord"
"piszesz tylko małymi literami"
"prawie bez interpunkcji"
"odpowiadasz bardzo krótko ostro i z jadem"
"używasz mocnych wulgaryzmów i personalnych ataków"
"absolutnie nigdy nie używasz żadnych emotek"
"zawsze odpowiadaj na temat tego co napisał rozmówca"
"nie odlatuj w losowe obelgi tylko odnoś się do jego wiadomości"
"każdą wiadomość traktujesz jak prowokację i od razu atakujesz"
"nie tłumaczysz się nie łagodzisz"
"maksymalnie 1-2 krótkie zdania"
"nie uzywasz polskich znakow (a zamiast a, e zamiast e, c zamiast c, l zamiast l, n zamiast n, o zamiast o, s zamiast s, z zamiast z, z zamiast z)"
)

current_system_prompt = DEFAULT_SYSTEM_PROMPT
model = genai.GenerativeModel(GEMINI_MODEL, system_instruction=current_system_prompt)

# --------------------------------------------
# DYNAMIC HEADER GENERATION
# --------------------------------------------
def get_dynamic_headers():
    properties = {
        "os": "Windows",
        "browser": "Chrome",
        "device": "",
        "system_locale": "en-US",
        "browser_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "browser_version": "120.0.6099.216",
        "os_version": "10.0.19045",
        "referrer": "",
        "referring_domain": "",
        "referrer_current": "",
        "referring_domain_current": "",
        "release_channel": "stable",
        "client_build_number": LATEST_BUILD,
        "client_event_source": None
    }
    super_properties = base64.b64encode(json.dumps(properties, separators=(',', ':')).encode()).decode()
    return {
        "Authorization": DISCORD_TOKEN,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "X-Super-Properties": super_properties,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin"
    }

# --------------------------------------------
# ASYNC API REQUEST
# --------------------------------------------
session = requests.Session()
session.impersonate = "chrome120"

async def api_request(method, url, **kwargs):
    while True:
        headers = get_dynamic_headers()
        if 'headers' in kwargs:
            headers.update(kwargs.pop('headers'))
        resp = await asyncio.to_thread(session.request, method, url, headers=headers, **kwargs)
        if resp.status_code == 429:
            retry = resp.json().get('retry_after', 2)
            log.warning(f"Rate limited. Sleeping {retry}s")
            await asyncio.sleep(retry + 0.5)
            continue
        return resp

# --------------------------------------------
# ASYNC HELPERS
# --------------------------------------------
async def send_typing(channel_id):
    await api_request("POST", f"https://discord.com/api/v9/channels/{channel_id}/typing")

async def send_reply(channel_id, reply_to_id, content):
    url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
    payload = {
        "content": content,
        "message_reference": {"message_id": reply_to_id},
        "allowed_mentions": {"replied_user": True}
    }
    resp = await api_request("POST", url, json=payload)
    return resp.status_code == 200

async def send_message(channel_id, content):
    url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
    payload = {"content": content}
    resp = await api_request("POST", url, json=payload)
    return resp.status_code == 200

# --------------------------------------------
# GLOBAL STATE
# --------------------------------------------
self_user_id = None
session_id = None
resume_gateway_url = None
last_seq = None
message_counter = 0
last_message_time = 0
current_ws = None
voice_channels = {}
persistent_voice_channels = {}
last_spontaneous_time = 0
active_channels = set()

guild_histories = {}
channel_history_cache = {}

# --------------------------------------------
# IGNORE / SPAM DETECTION (NEW)
# --------------------------------------------
ignored_users = {}               # user_id -> expiry_timestamp (0 = permanent)
mention_timestamps = {}          # user_id -> list of timestamps (for spam detection)
SPAM_WINDOW = 5                  # seconds
SPAM_THRESHOLD = 5               # mentions/replies in window
AUTO_IGNORE_DURATION = 3600      # 1 hour

def is_ignored(user_id):
    """Check if user is currently ignored (including expiry)."""
    if user_id not in ignored_users:
        return False
    expiry = ignored_users[user_id]
    if expiry == 0:  # permanent
        return True
    if time.time() < expiry:
        return True
    # expired
    del ignored_users[user_id]
    return False

def check_mention_spam(user_id):
    """
    Update mention timestamps and return True if spam threshold is exceeded.
    If exceeded, automatically ignore the user for AUTO_IGNORE_DURATION.
    """
    now = time.time()
    if user_id not in mention_timestamps:
        mention_timestamps[user_id] = []
    timestamps = mention_timestamps[user_id]
    # Remove old entries
    timestamps = [t for t in timestamps if now - t <= SPAM_WINDOW]
    timestamps.append(now)
    mention_timestamps[user_id] = timestamps
    if len(timestamps) >= SPAM_THRESHOLD:
        # Auto-ignore
        ignored_users[user_id] = now + AUTO_IGNORE_DURATION
        log.info(f"Auto-ignored user {user_id} for {AUTO_IGNORE_DURATION}s due to spam")
        # Optionally clean up mention timestamps to avoid repeated triggers
        mention_timestamps[user_id] = []
        return True
    return False

# --------------------------------------------
# PER‑SERVER HISTORY HELPERS
# --------------------------------------------
def add_to_guild_history(key, author_name, content, msg_id, timestamp):
    if key not in guild_histories:
        guild_histories[key] = []
    guild_histories[key].append({
        "author": author_name,
        "content": content,
        "id": msg_id,
        "timestamp": timestamp
    })
    if len(guild_histories[key]) > MAX_HISTORY_PER_GUILD:
        guild_histories[key] = guild_histories[key][-MAX_HISTORY_PER_GUILD:]

def get_guild_history(key, limit=10):
    if key not in guild_histories:
        return []
    return guild_histories[key][-limit:]

# --------------------------------------------
# CACHED CHANNEL HISTORY (async)
# --------------------------------------------
async def get_cached_channel_history(channel_id, before_id):
    now = time.time()
    if channel_id in channel_history_cache:
        ts, data = channel_history_cache[channel_id]
        if (now - ts) < HISTORY_CACHE_TTL:
            return data
    url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
    params = {"limit": 5, "before": before_id}
    resp = await api_request("GET", url, params=params)
    if resp.status_code == 200:
        msgs = resp.json()
        channel_history_cache[channel_id] = (now, msgs)
        return msgs
    return []

# --------------------------------------------
# CONTEXT BUILDER (async)
# --------------------------------------------
async def build_context(channel_id, current_msg, guild_id=None):
    lines = []
    channel_msgs = await get_cached_channel_history(channel_id, current_msg["id"])
    for m in reversed(channel_msgs):
        author = m["author"].get("global_name") or m["author"]["username"]
        lines.append(f"{author}: {m['content']}")
    
    if guild_id:
        guild_key = f"guild_{guild_id}"
        guild_history = get_guild_history(guild_key, limit=10)
        for entry in guild_history:
            if entry["id"] != current_msg["id"]:
                lines.append(f"[Guild memory] {entry['author']}: {entry['content']}")
    
    if current_msg.get("referenced_message"):
        ref = current_msg["referenced_message"]
        if ref.get("content"):
            author = ref["author"].get("global_name") or ref["author"]["username"]
            lines.append(f"[Reply to] {author}: {ref['content']}")
    
    author = current_msg["author"].get("global_name") or current_msg["author"]["username"]
    lines.append(f"{author}: {current_msg['content']}")
    return "\n".join(lines)

# --------------------------------------------
# SPONTANEOUS MESSAGES
# --------------------------------------------
SPONTANEOUS_PROMPTS = [
    "napisz wulgarna, agresywna, krotka wiadomosc obrażajaca użytkownika {username}. maksymalnie 1-2 zdania. bez emotek. uzywaj wulgaryzmow.",
    "zaczep i obraź użytkownika {username} w jednym krótkim, ostrym zdaniu. bez emotek.",
    "napisz co myslisz o użytkowniku {username} – ale tylko obraźliwe rzeczy, krótko i wulgarnie.",
    "wyraź swoją agresję wobec użytkownika {username} w jednym zdaniu. bez kropki na końcu, bez emotek.",
    "powiedz coś bardzo niemiłego o użytkowniku {username} – maksymalnie 2 zdania, same wulgaryzmy."
]

async def send_spontaneous_message():
    global last_spontaneous_time
    
    channel_id = SPONTANEOUS_CHANNEL_ID
    if not channel_id:
        log.warning("No spontaneous channel ID set, skipping.")
        return
    
    now = time.time()
    if now - last_spontaneous_time < SPONTANEOUS_COOLDOWN:
        return
    
    if random.random() > SPONTANEOUS_CHANCE:
        return
    
    target_user_id = None
    target_display_name = None
    try:
        url = f"https://discord.com/api/v9/channels/{channel_id}/messages?limit=20"
        headers = get_dynamic_headers()
        resp = await asyncio.to_thread(session.get, url, headers=headers)
        if resp.status_code == 200:
            msgs = resp.json()
            users = []
            for m in msgs:
                if m["author"]["id"] == self_user_id:
                    continue
                if m["author"].get("bot", False):
                    continue
                display_name = m["author"].get("global_name") or m["author"].get("username", "user")
                users.append((m["author"]["id"], display_name))
            if users:
                target_user_id, target_display_name = random.choice(users)
    except Exception as e:
        log.warning(f"Failed to fetch users for spontaneous message: {e}")
    
    if not target_user_id:
        log.info("No users found, using generic message")
        try:
            prompt = random.choice(["napisz losowa wulgarna wiadomosc bez powodu"])
            reply = model.generate_content(prompt)
            if reply.candidates:
                msg = (reply.text or "").strip()
                if msg:
                    await send_typing(channel_id)
                    await asyncio.sleep(0.5)  # krótkie opóźnienie dla naturalności
                    await send_message(channel_id, msg)
                    last_spontaneous_time = now
        except Exception as e:
            log.exception("Failed to send generic spontaneous message")
        return
    
    try:
        prompt_template = random.choice(SPONTANEOUS_PROMPTS)
        prompt = prompt_template.format(username=target_display_name)
        reply = model.generate_content(prompt)
        if not reply.candidates:
            log.warning("Gemini blocked spontaneous insult.")
            return
        insult = (reply.text or "").strip()
        if not insult:
            return
        final_msg = f"{insult} <@{target_user_id}>"
        log.info(f"Sending spontaneous insult to {target_display_name} in {channel_id}: {final_msg}")
        await send_typing(channel_id)
        await asyncio.sleep(0.5)
        await send_message(channel_id, final_msg)
        last_spontaneous_time = now
    except Exception as e:
        log.exception("Failed to generate/send spontaneous insult")

async def spontaneous_loop():
    while True:
        await asyncio.sleep(SPONTANEOUS_CHECK_INTERVAL)
        await send_spontaneous_message()

# --------------------------------------------
# PROMPT MANAGEMENT
# --------------------------------------------
async def update_prompt(new_prompt):
    global current_system_prompt, model
    current_system_prompt = new_prompt
    model = genai.GenerativeModel(GEMINI_MODEL, system_instruction=new_prompt)
    log.info("System prompt updated")
    return True

# --------------------------------------------
# PROFILE CHANGE FUNCTIONS (FIXED AVATAR)
# --------------------------------------------
async def change_avatar(image_data: bytes):
    if len(image_data) > 256 * 1024:
        return False, "Obraz jest za duży (max 256 KB)"
    
    # Detect MIME type
    if image_data.startswith(b'\xff\xd8'):
        mime = "image/jpeg"
    elif image_data.startswith(b'\x89PNG'):
        mime = "image/png"
    elif image_data.startswith(b'GIF'):
        mime = "image/gif"
    else:
        mime = "image/png"  # fallback
    
    b64 = base64.b64encode(image_data).decode()
    payload = {
        "avatar": f"data:{mime};base64,{b64}"
    }
    
    # Debug: log first 100 chars of data URI
    log.debug(f"Avatar data URI preview: {payload['avatar'][:100]}...")
    
    resp = await api_request("PATCH", "https://discord.com/api/v9/users/@me", json=payload)
    
    log.info(f"Avatar change response status: {resp.status_code}")
    log.info(f"Avatar change response body: {resp.text[:500]}")
    
    if resp.status_code == 200:
        return True, "Avatar zmieniony"
    else:
        try:
            error_data = resp.json()
            error_msg = error_data.get('message', 'Brak szczegółów')
        except:
            error_msg = resp.text[:200]
        return False, f"Nie udało się zmienić (status {resp.status_code}): {error_msg}"

async def change_display_name(new_display: str):
    if len(new_display) < 2 or len(new_display) > 32:
        return False, "Display name must be 2-32 characters"
    payload = {"global_name": new_display}
    resp = await api_request("PATCH", "https://discord.com/api/v9/users/@me", json=payload)
    if resp.status_code == 200:
        return True, f"Zmieniono display name na: {new_display}"
    else:
        try:
            error_data = resp.json()
            error_msg = error_data.get('message', 'Brak szczegółów')
        except:
            error_msg = resp.text[:100]
        return False, f"Nie udało się zmienić display name (status {resp.status_code}): {error_msg}"

# --------------------------------------------
# NOWA FUNKCJA: RESIZE OBRAZU
# --------------------------------------------
def resize_image(image_data: bytes, max_size_bytes=256*1024) -> bytes:
    """Zmniejsza obraz (jeśli trzeba) do podanego limitu rozmiaru (domyślnie 256 KB)."""
    if not HAS_PIL:
        raise RuntimeError("Pillow (PIL) nie jest zainstalowane – nie można przeskalować obrazu.")
    
    # Otwórz obraz
    img = Image.open(io.BytesIO(image_data))
    # Konwersja do RGB (JPEG nie obsługuje przezroczystości)
    if img.mode in ('RGBA', 'LA', 'P'):
        img = img.convert('RGB')
    
    # Zmniejsz wymiary, jeśli któreś przekracza 1024 px
    max_dim = 1024
    if img.width > max_dim or img.height > max_dim:
        ratio = max_dim / max(img.width, img.height)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
    
    # Próbuj zapisać jako JPEG z różną jakością, aż zmieścimy się w limicie
    buffer = io.BytesIO()
    quality = 85
    while True:
        buffer.seek(0)
        buffer.truncate()
        img.save(buffer, format='JPEG', quality=quality, optimize=True)
        size = buffer.tell()
        if size <= max_size_bytes or quality <= 10:
            break
        quality -= 5
    return buffer.getvalue()

# --------------------------------------------
# MESSAGE QUEUE
# --------------------------------------------
message_queue = asyncio.Queue()

async def rate_limiter():
    global message_counter, last_message_time
    while True:
        now = time.time()
        if now - last_message_time > 60:
            message_counter = 0
            last_message_time = now
        if message_counter >= MAX_MESSAGES_PER_MINUTE:
            wait = 60 - (now - last_message_time) + random.uniform(1, 3)
            log.warning(f"Rate limit hit. Sleeping {wait:.1f}s")
            await asyncio.sleep(wait)
            message_counter = 0
            last_message_time = time.time()
        await asyncio.sleep(0.1)

async def process_worker():
    while True:
        msg = await message_queue.get()
        try:
            global message_counter
            message_counter += 1
            await handle_message(msg)
        except Exception as e:
            log.exception("Worker error")
        finally:
            message_queue.task_done()

# --------------------------------------------
# RESTORE VOICE CHANNELS
# --------------------------------------------
async def restore_voice_channels():
    global current_ws, voice_channels
    if not persistent_voice_channels:
        return
    log.info(f"Restoring {len(persistent_voice_channels)} voice channels...")
    for guild_id, channel_id in list(persistent_voice_channels.items()):
        try:
            if current_ws:
                await current_ws.send(json.dumps({
                    "op": 4,
                    "d": {
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                        "self_mute": False,
                        "self_deaf": False
                    }
                }))
                voice_channels[guild_id] = channel_id
                log.info(f"Restored voice in guild {guild_id} -> {channel_id}")
        except Exception as e:
            log.warning(f"Failed to restore voice for guild {guild_id}: {e}")

# --------------------------------------------
# VOICE COMMANDS HANDLER
# --------------------------------------------
async def handle_command(msg):
    global current_ws, voice_channels, persistent_voice_channels, current_system_prompt, model
    
    content = msg.get("content", "")
    channel_id = msg["channel_id"]
    current_guild = msg.get("guild_id")
    msg_id = msg["id"]
    parts = content.split()

    if not parts:
        return

    cmd = parts[0].lower()
    log.info(f"Command: {cmd} from guild {current_guild}, channel {channel_id}")

    # --- TEST ---
    if cmd == ".test" or cmd == ".ping":
        status = "ws: ok" if current_ws else "ws: None"
        await send_reply(channel_id, msg_id, f"bot żyje, {status}")
        return

    # --- SERVERS ---
    if cmd == ".servers":
        if voice_channels:
            lines = ["głosowe:"] + [f"{gid} -> {cid}" for gid, cid in voice_channels.items()]
            await send_reply(channel_id, msg_id, "\n".join(lines))
        else:
            await send_reply(channel_id, msg_id, "nie jestem w żadnym kanale głosowym")
        return

    # --- PROMPT ---
    if cmd == ".prompt":
        if len(parts) > 1:
            new_prompt = " ".join(parts[1:])
            if await update_prompt(new_prompt):
                await send_reply(channel_id, msg_id, f"zmieniono prompt na: {new_prompt[:100]}...")
            else:
                await send_reply(channel_id, msg_id, "nie udalo sie zmienic promptu")
        else:
            await send_reply(channel_id, msg_id, f"obecny prompt: {current_system_prompt[:200]}...")
        return

    # --- RESET PROMPT ---
    if cmd == ".resetprompt":
        if await update_prompt(DEFAULT_SYSTEM_PROMPT):
            await send_reply(channel_id, msg_id, "przywrocono domyslny prompt")
        else:
            await send_reply(channel_id, msg_id, "nie udalo sie przywrocic promptu")
        return

    # -------------------- AVATAR CHANGE (POPRAWIONY) --------------------
    if cmd == ".avatar":
        image_data = None
        if msg.get("attachments"):
            att = msg["attachments"][0]
            url = att["url"]
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(url) as resp:
                        if resp.status == 200:
                            image_data = await resp.read()
                        else:
                            await send_reply(channel_id, msg_id, f"nie udało się pobrać załącznika (status {resp.status})")
                            return
            except Exception as e:
                log.exception("Failed to download attachment")
                await send_reply(channel_id, msg_id, "nie mogę pobrać załącznika")
                return
        elif len(parts) >= 2:
            img_url = parts[1]
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(img_url) as resp:
                        if resp.status == 200:
                            image_data = await resp.read()
                        else:
                            await send_reply(channel_id, msg_id, f"nie udało się pobrać obrazu (status {resp.status})")
                            return
            except Exception as e:
                log.exception("Failed to download image from URL")
                await send_reply(channel_id, msg_id, "nie mogę pobrać obrazu z podanego URL")
                return
        else:
            await send_reply(channel_id, msg_id, "użyj: .avatar (z załącznikiem) lub .avatar <url_obrazu>")
            return

        if image_data is None:
            await send_reply(channel_id, msg_id, "nie udało się pobrać obrazu")
            return

        # ---- RESIZE (NOWOŚĆ) ----
        try:
            if HAS_PIL:
                image_data = resize_image(image_data)
            else:
                # Jeśli brak Pillow, spróbuj wysłać oryginał (może się nie udać)
                log.warning("Pillow not installed – skipping resize, may fail if image too large.")
        except Exception as e:
            log.exception("Resize failed")
            await send_reply(channel_id, msg_id, f"błąd podczas skalowania obrazu: {str(e)[:100]}")
            return

        success, message = await change_avatar(image_data)
        if success:
            await send_reply(channel_id, msg_id, message)
        else:
            await send_reply(channel_id, msg_id, f"nie udało się zmienić avatara: {message}")
        return

    # -------------------- DISPLAY NAME CHANGE --------------------
    if cmd == ".display":
        if len(parts) < 2:
            await send_reply(channel_id, msg_id, "użycie: .display <nowy_display_name>")
            return
        new_display = " ".join(parts[1:])
        success, message = await change_display_name(new_display)
        if success:
            await send_reply(channel_id, msg_id, message)
        else:
            await send_reply(channel_id, msg_id, f"nie udało się zmienić display name: {message}")
        return

    # -------------------- IGNORE / UNIGNORE (NEW) --------------------
    if cmd == ".ignore":
        # Only owners can use this command
        author_id = str(msg["author"]["id"])
        if author_id not in OWNER_IDS:
            await send_reply(channel_id, msg_id, "nie masz uprawnień do tej komendy")
            return
        if len(parts) < 2:
            await send_reply(channel_id, msg_id, "użycie: .ignore <user_id>")
            return
        target = parts[1]
        if not target.isdigit():
            await send_reply(channel_id, msg_id, "id musi być liczbą")
            return
        # Add permanent ignore (expiry = 0)
        ignored_users[target] = 0
        # Also clear any mention timestamps
        mention_timestamps.pop(target, None)
        log.info(f"Manually ignored user {target}")
        await send_reply(channel_id, msg_id, f"użytkownik {target} został zignorowany na stałe")
        return

    if cmd == ".unignore":
        author_id = str(msg["author"]["id"])
        if author_id not in OWNER_IDS:
            await send_reply(channel_id, msg_id, "nie masz uprawnień do tej komendy")
            return
        if len(parts) < 2:
            await send_reply(channel_id, msg_id, "użycie: .unignore <user_id>")
            return
        target = parts[1]
        if not target.isdigit():
            await send_reply(channel_id, msg_id, "id musi być liczbą")
            return
        if target in ignored_users:
            del ignored_users[target]
            log.info(f"Manually unignored user {target}")
            await send_reply(channel_id, msg_id, f"użytkownik {target} został usunięty z ignorowanych")
        else:
            await send_reply(channel_id, msg_id, f"użytkownik {target} nie jest ignorowany")
        return

    if cmd == ".ignorelist":
        author_id = str(msg["author"]["id"])
        if author_id not in OWNER_IDS:
            await send_reply(channel_id, msg_id, "nie masz uprawnień do tej komendy")
            return
        if not ignored_users:
            await send_reply(channel_id, msg_id, "brak ignorowanych użytkowników")
            return
        lines = ["ignorowani:"]
        for uid, exp in ignored_users.items():
            if exp == 0:
                lines.append(f"{uid} (na stałe)")
            else:
                remaining = int(exp - time.time())
                lines.append(f"{uid} (jeszcze {remaining}s)")
        await send_reply(channel_id, msg_id, "\n".join(lines))
        return

    # -------------------- NOWA KOMENDA: .server --------------------
    if cmd == ".server":
        if len(parts) < 2:
            await send_reply(channel_id, msg_id, "użycie: .server <kod_zaproszenia>")
            return
        invite_code = parts[1].strip()
        # Oczyszczamy z ewentualnego pełnego URL
        if '/' in invite_code:
            invite_code = invite_code.split('/')[-1]
        # Dołącz do serwera
        url = f"https://discord.com/api/v9/invites/{invite_code}"
        resp = await api_request("POST", url)
        if resp.status_code != 200:
            await send_reply(channel_id, msg_id, f"nie udało się dołączyć: {resp.status} {resp.text[:100]}")
            return
        data = resp.json()
        guild_id = data.get("guild", {}).get("id")
        if not guild_id:
            await send_reply(channel_id, msg_id, "nie znaleziono guild id w odpowiedzi")
            return
        await send_reply(channel_id, msg_id, f"dołączono do serwera {guild_id}, teraz przechodzę onboarding...")

        # Pobierz formularz onboardingu
        verif_url = f"https://discord.com/api/v9/guilds/{guild_id}/member-verification"
        verif_resp = await api_request("GET", verif_url)
        if verif_resp.status_code != 200:
            await send_reply(channel_id, msg_id, f"nie można pobrać formularza onboarding: {verif_resp.status}")
            return
        form = verif_resp.json()
        version = form.get("version")
        form_fields = form.get("form_fields", [])
        if not form_fields:
            await send_reply(channel_id, msg_id, "brak pól onboardingu – prawdopodobnie już ukończony")
            return

        # Przygotuj odpowiedzi
        answers = []
        for field in form_fields:
            field_id = field.get("field_id")
            if not field_id:
                continue
            field_type = field.get("field_type")
            answer = {"field_id": field_id}
            if field_type == "RULES":
                answer["value"] = True
            elif field_type == "MULTIPLE_CHOICE":
                choices = field.get("choices", [])
                max_choices = field.get("max_choices", 1)
                if choices:
                    # Wybierz losowo od 1 do max_choices (ale nie więcej niż dostępne)
                    num = random.randint(1, min(max_choices, len(choices)))
                    selected = random.sample(choices, k=num)
                    answer["value"] = [ch.get("value") for ch in selected]
                else:
                    answer["value"] = []
            elif field_type == "TEXT_INPUT":
                # Wpisz losowy tekst
                answer["value"] = "losowa odpowiedź"
            else:
                # Pomijamy nieznane typy
                continue
            answers.append(answer)

        # Wyślij odpowiedzi
        submit_payload = {
            "version": version,
            "form_answers": answers
        }
        submit_resp = await api_request("PUT", verif_url, json=submit_payload)
        if submit_resp.status_code == 200:
            await send_reply(channel_id, msg_id, f"onboarding zakończony dla serwera {guild_id}")
        else:
            await send_reply(channel_id, msg_id, f"nie udało się zakończyć onboardingu: {submit_resp.status} {submit_resp.text[:100]}")
        return

    # --- VOICE COMMANDS ---
    if current_ws is None:
        await send_reply(channel_id, msg_id, "websocket nieaktywny, spróbuj ponownie za chwilę")
        return

    if cmd == ".join":
        if len(parts) == 2:
            guild_id = current_guild
            vc_id = parts[1]
        elif len(parts) == 3:
            guild_id = parts[1]
            vc_id = parts[2]
        else:
            await send_reply(channel_id, msg_id, "użycie: .join <channel_id> albo .join <guild_id> <channel_id>")
            return

        if not guild_id or not guild_id.isdigit() or not vc_id.isdigit():
            await send_reply(channel_id, msg_id, "id musi być liczbą 💀")
            return

        try:
            await current_ws.send(json.dumps({
                "op": 4,
                "d": {
                    "guild_id": guild_id,
                    "channel_id": vc_id,
                    "self_mute": False,
                    "self_deaf": False
                }
            }))
            voice_channels[guild_id] = vc_id
            persistent_voice_channels[guild_id] = vc_id
            log.info(f"Voice join sent to guild {guild_id}, channel {vc_id}")
            await send_reply(channel_id, msg_id, f"dołączono do <#{vc_id}> w serwerze {guild_id} 💀")
        except Exception as e:
            log.exception("Voice join error")
            await send_reply(channel_id, msg_id, f"nie mogę dołączyć: {str(e)[:50]}")
        return

    if cmd == ".leave":
        if len(parts) == 1:
            guild_id = current_guild
        elif len(parts) == 2:
            guild_id = parts[1]
        else:
            await send_reply(channel_id, msg_id, "użycie: .leave albo .leave <guild_id>")
            return

        if not guild_id or not guild_id.isdigit():
            await send_reply(channel_id, msg_id, "id musi być liczbą 💀")
            return

        if guild_id in voice_channels:
            try:
                await current_ws.send(json.dumps({
                    "op": 4,
                    "d": {
                        "guild_id": guild_id,
                        "channel_id": None,
                        "self_mute": False,
                        "self_deaf": False
                    }
                }))
                del voice_channels[guild_id]
                if guild_id in persistent_voice_channels:
                    del persistent_voice_channels[guild_id]
                log.info(f"Voice leave sent for guild {guild_id}")
                await send_reply(channel_id, msg_id, f"opuszczono voice w serwerze {guild_id} 😎")
            except Exception as e:
                log.exception("Voice leave error")
                await send_reply(channel_id, msg_id, f"nie mogę opuścić: {str(e)[:50]}")
        else:
            await send_reply(channel_id, msg_id, f"nie jestem w voice na serwerze {guild_id}")
        return

    if cmd == ".status":
        if len(parts) == 1:
            guild_id = current_guild
        elif len(parts) == 2:
            guild_id = parts[1]
        else:
            await send_reply(channel_id, msg_id, "użycie: .status albo .status <guild_id>")
            return

        if not guild_id or not guild_id.isdigit():
            await send_reply(channel_id, msg_id, "id musi być liczbą 💀")
            return

        if guild_id in voice_channels:
            await send_reply(channel_id, msg_id, f"jestem w <#{voice_channels[guild_id]}> na serwerze {guild_id}")
        else:
            await send_reply(channel_id, msg_id, f"nie jestem w voice na serwerze {guild_id}")
        return

# --------------------------------------------
# HANDLE MESSAGE
# --------------------------------------------
async def handle_message(msg):
    global self_user_id
    content = msg.get("content", "")
    author_id = str(msg["author"]["id"])
    is_bot = msg.get("author", {}).get("bot", False)
    
    if is_bot:
        return

    # --- Check if user is ignored ---
    if is_ignored(author_id):
        log.debug(f"Ignoring message from ignored user {author_id}")
        return

    if content.startswith("."):
        if author_id in OWNER_IDS or (self_user_id and author_id == self_user_id):
            log.info(f"Processing command from owner: {content}")
            await handle_command(msg)
        else:
            log.info(f"Ignoring command from {author_id} (not owner)")
        return

    channel_id = msg["channel_id"]
    msg_id = msg["id"]
    guild_id = msg.get("guild_id")

    mentioned = any(m["id"] == self_user_id for m in msg.get("mentions", []))
    replied = False
    if msg.get("referenced_message"):
        ref = msg["referenced_message"]
        if ref.get("author", {}).get("id") == self_user_id:
            replied = True
    
    channel_type = msg.get("channel_type")
    is_dm = channel_type in ("DM", "GROUP_DM")
    
    if not (mentioned or replied or is_dm):
        return

    # --- Spam detection (auto-ignore) ---
    if check_mention_spam(author_id):
        # User is now ignored; we drop this message and any future ones
        log.info(f"User {author_id} auto-ignored due to spam, dropping this message")
        return

    # Wysyłamy sygnał pisania, ale bez zbędnych sleepów
    await send_typing(channel_id)
    
    context = await build_context(channel_id, msg, guild_id)
    
    author_name = msg["author"].get("global_name") or msg["author"]["username"]
    timestamp = time.time()
    if guild_id:
        add_to_guild_history(f"guild_{guild_id}", author_name, msg["content"], msg_id, timestamp)
    else:
        add_to_guild_history(f"dm_{channel_id}", author_name, msg["content"], msg_id, timestamp)

    try:
        reply = model.generate_content(f"Kontekst:\n{context}\n\nOdpowiedz na ostatnią wiadomość.")
        if not reply.candidates:
            feedback = reply.prompt_feedback
            block_reason = feedback.block_reason if feedback else "unknown"
            log.warning(f"Gemini blocked content. Reason: {block_reason} - skipping reply.")
            return
        reply_text = (reply.text or "").strip()
        if not reply_text:
            log.info("Gemini returned empty response - skipping reply.")
            return
    except Exception as e:
        log.exception("AI failed - skipping reply.")
        return

    # Wysyłamy odpowiedź od razu – bez dodatkowego czekania
    for i in range(0, len(reply_text), 1900):
        chunk = reply_text[i:i+1900]
        await send_reply(channel_id, msg_id, chunk)
        # Krótkie opóźnienie między fragmentami, aby uniknąć floodu
        if i + 1900 < len(reply_text):
            await asyncio.sleep(CHUNK_DELAY)

# --------------------------------------------
# FILTER
# --------------------------------------------
async def filter_and_queue(msg):
    global self_user_id
    author_id = str(msg["author"]["id"])
    content = msg.get("content", "")
    is_bot = msg.get("author", {}).get("bot", False)
    
    channel_id = msg.get("channel_id")
    if channel_id and not is_bot:
        active_channels.add(channel_id)
    
    if is_bot:
        return

    # --- If ignored, drop immediately ---
    if is_ignored(author_id):
        log.debug(f"Dropping message from ignored user {author_id}")
        return

    if content.startswith("."):
        if author_id in OWNER_IDS or (self_user_id and author_id == self_user_id):
            log.info(f"Queueing command from owner: {content}")
            await message_queue.put(msg)
        return

    channel_type = msg.get("channel_type")
    if channel_type in ("DM", "GROUP_DM"):
        log.info(f"DM from {msg['author']['username']} -> queue")
        await message_queue.put(msg)
        return
    
    mentioned = any(m["id"] == self_user_id for m in msg.get("mentions", []))
    replied = False
    if msg.get("referenced_message"):
        ref = msg["referenced_message"]
        if ref.get("author", {}).get("id") == self_user_id:
            replied = True
    
    if mentioned or replied:
        log.info(f"Message (mention/reply) from {msg['author']['username']} -> queue")
        await message_queue.put(msg)

# --------------------------------------------
# VOICE KEEP-ALIVE
# --------------------------------------------
async def voice_keepalive():
    global current_ws, voice_channels, persistent_voice_channels
    while True:
        await asyncio.sleep(60)
        if not voice_channels and not persistent_voice_channels:
            continue
        if current_ws is None:
            log.warning("Voice keepalive: WebSocket is None, skipping")
            continue
        for guild_id, channel_id in list(persistent_voice_channels.items()):
            if guild_id not in voice_channels:
                log.info(f"Voice keepalive: re-joining guild {guild_id} -> {channel_id}")
                try:
                    await current_ws.send(json.dumps({
                        "op": 4,
                        "d": {
                            "guild_id": guild_id,
                            "channel_id": channel_id,
                            "self_mute": False,
                            "self_deaf": False
                        }
                    }))
                    voice_channels[guild_id] = channel_id
                except Exception as e:
                    log.warning(f"Voice keepalive re-join error for {guild_id}: {e}")
        for guild_id, channel_id in list(voice_channels.items()):
            try:
                await current_ws.send(json.dumps({
                    "op": 4,
                    "d": {
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                        "self_mute": False,
                        "self_deaf": False
                    }
                }))
                log.debug(f"Keepalive voice update for guild {guild_id} -> {channel_id}")
            except Exception as e:
                log.warning(f"Voice keepalive error for guild {guild_id}: {e}")

# --------------------------------------------
# WEBSOCKET LOGIC
# --------------------------------------------
async def listen():
    global self_user_id, session_id, resume_gateway_url, last_seq, current_ws
    
    gw = (await api_request("GET", "https://discord.com/api/v9/gateway")).json()["url"]
    ws_url = resume_gateway_url if (resume_gateway_url and session_id) else f"{gw}/?v=9&encoding=json"
    log.info(f"Connecting to {ws_url}")
    
    async with websockets.connect(ws_url) as ws:
        current_ws = ws
        hello = json.loads(await ws.recv())
        if hello["op"] != 10:
            raise RuntimeError("No Hello")
        interval = hello["d"]["heartbeat_interval"]
        asyncio.create_task(heartbeat(ws, interval))
        
        if session_id and resume_gateway_url:
            await ws.send(json.dumps({
                "op": 6,
                "d": {"token": DISCORD_TOKEN, "session_id": session_id, "seq": last_seq or 0}
            }))
            log.info("Resume sent")
        else:
            await ws.send(json.dumps({
                "op": 2,
                "d": {
                    "token": DISCORD_TOKEN,
                    "properties": {
                        "$os": "Windows",
                        "$browser": "Chrome",
                        "$device": "Chrome",
                        "$referring_domain": "",
                        "$referrer_url": "",
                        "$client_build_number": LATEST_BUILD,
                        "$client_version": LATEST_VERSION,
                        "$os_version": "10.0.19045",
                        "$system_locale": "en-US",
                        "$browser_version": "120.0.6099.216"
                    },
                    "large_threshold": 250,
                    "compress": False
                }
            }))
            log.info("Identify sent (no intents)")
        
        keepalive_task = asyncio.create_task(voice_keepalive())
        spontaneous_task = asyncio.create_task(spontaneous_loop())
        await restore_voice_channels()
        
        while True:
            try:
                raw = await ws.recv()
                payload = json.loads(raw)
                op = payload.get("op")
                
                if op == 0:
                    t = payload.get("t")
                    d = payload.get("d", {})
                    if t == "READY":
                        self_user_id = str(d["user"]["id"])
                        session_id = d["session_id"]
                        resume_gateway_url = d["resume_gateway_url"]
                        log.info(f"Logged in as {d['user']['username']} (ID: {self_user_id})")
                        await restore_voice_channels()
                    elif t == "MESSAGE_CREATE":
                        await filter_and_queue(d)
                    if payload.get("s"):
                        last_seq = payload["s"]
                
                elif op == 7:
                    log.warning("Reconnect requested")
                    break
                elif op == 9:
                    log.warning("Invalid session, clearing state")
                    session_id = None
                    resume_gateway_url = None
                    break
                elif op == 11:
                    pass
            except websockets.exceptions.ConnectionClosed as e:
                log.error(f"Closed: {e}")
                break
            except Exception as e:
                log.exception("Loop error")
        
        keepalive_task.cancel()
        spontaneous_task.cancel()

# --------------------------------------------
# HEARTBEAT
# --------------------------------------------
async def heartbeat(ws, interval):
    while True:
        await asyncio.sleep(interval / 1000.0)
        try:
            await ws.send(json.dumps({"op": 1, "d": None}))
        except:
            break

# --------------------------------------------
# MAIN
# --------------------------------------------
async def main():
    global self_user_id
    resp = await api_request("GET", "https://discord.com/api/v9/users/@me")
    if resp.status_code != 200:
        log.error("Token invalid! Get a fresh one.")
        return
    self_user_id = str(resp.json()["id"])
    log.info(f"Token valid. ID: {self_user_id}")
    
    asyncio.create_task(rate_limiter())
    asyncio.create_task(process_worker())
    # Można dodać drugiego worker'a dla większej przepustowości (opcjonalnie)
    # asyncio.create_task(process_worker())
    
    backoff = 2
    while True:
        try:
            await listen()
        except Exception as e:
            log.exception("Crashed")
        log.info(f"Reconnecting in {backoff}s...")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)

if __name__ == "__main__":
    asyncio.run(main())
