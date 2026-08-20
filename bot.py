import os
import json
import asyncio
import logging
import random
import time
import base64
from dotenv import load_dotenv
import websockets
from curl_cffi import requests
from PIL import Image
import io
import aiohttp

# Import the new Google GenAI package
from google import genai
from google.genai import types

load_dotenv()

# --------------------------------------------
# CONFIGURATION
# --------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

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

# Faster delays
MIN_REPLY_DELAY = 0.5
MAX_REPLY_DELAY = 1.0
CHUNK_DELAY = 0.1
MAX_MESSAGES_PER_MINUTE = 10
HISTORY_CACHE_TTL = 60
MAX_HISTORY_PER_GUILD = 50

# Avatar settings
MAX_AVATAR_SIZE = 256 * 1024  # 256 KB max
MAX_AVATAR_DIMENSION = 1024  # Max width/height for avatar

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
client = genai.Client(api_key=GEMINI_API_KEY)

DEFAULT_SYSTEM_PROMPT = (
"jestes wulgarnym agresywnym botem discord\n"
"piszesz tylko malymi literami\n"
"prawie bez interpunkcji\n"
"odpowiadasz bardzo krotko ostro i z jadem\n"
"uzywasz mocnych wulgaryzmow i personalnych atakow\n"
"w normalnych odpowiedziach absolutnie nigdy nie uzywasz zadnych emotek\n"
"zawsze odpowiadaj na temat tego co napisal rozmowca\n"
"nie odlatuj w losowe obelgi tylko odnos sie do jego wiadomosci\n"
"kazda wiadomosc traktujesz jak prowokacje i od razu atakujesz\n"
"nie tlumaczysz sie nie lagodzis\n"
"maksymalnie 1-2 krotkie zdania\n"
"nie uzywasz polskich znakow\n"
"zawsze pisz ze spacjami miedzy slowami nigdy nie sklejaj slow ze soba"
)

current_system_prompt = DEFAULT_SYSTEM_PROMPT

def get_gemini_response(prompt):
    """Get response from Gemini"""
    try:
        system_message = f"{current_system_prompt}\n\n{prompt}"
        
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=system_message,
            config=types.GenerateContentConfig(
                temperature=0.9,
                max_output_tokens=150,
                top_p=0.95,
                top_k=40,
            )
        )
        
        return response.text if response.text else None
    except Exception as e:
        log.exception(f"Gemini API error: {e}")
        return None

# --------------------------------------------
# SUPPORTED IMPERSONATIONS
# --------------------------------------------
SUPPORTED_IMPERSONATIONS = ["chrome120", "chrome123", "chrome124"]

def get_supported_impersonate():
    return random.choice(SUPPORTED_IMPERSONATIONS)

def get_random_user_agent():
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ]
    return random.choice(user_agents)

def get_dynamic_headers():
    user_agent = get_random_user_agent()
    build_variation = random.randint(-50, 50)
    current_build = LATEST_BUILD + build_variation
    
    properties = {
        "os": "Windows",
        "browser": "Chrome",
        "device": "",
        "system_locale": "en-US",
        "browser_user_agent": user_agent,
        "browser_version": "120.0.6099.216",
        "os_version": "10.0.19045",
        "referrer": "",
        "referring_domain": "",
        "referrer_current": "",
        "referring_domain_current": "",
        "release_channel": "stable",
        "client_build_number": current_build,
        "client_event_source": None
    }
    super_properties = base64.b64encode(json.dumps(properties, separators=(',', ':')).encode()).decode()
    
    return {
        "Authorization": DISCORD_TOKEN,
        "User-Agent": user_agent,
        "X-Super-Properties": super_properties,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

# --------------------------------------------
# ASYNC API REQUEST
# --------------------------------------------
session = requests.Session()
session.impersonate = "chrome120"

async def api_request(method, url, **kwargs):
    while True:
        try:
            headers = get_dynamic_headers()
            if 'headers' in kwargs:
                headers.update(kwargs.pop('headers'))
            
            session.impersonate = "chrome120"
            
            resp = await asyncio.to_thread(session.request, method, url, headers=headers, **kwargs)
            
            if resp.status_code == 429:
                retry = resp.json().get('retry_after', 2)
                log.warning(f"Rate limited. Sleeping {retry}s")
                await asyncio.sleep(retry + 0.5)
                continue
            return resp
            
        except Exception as e:
            if "ImpersonateError" in str(e) or "impersonate" in str(e).lower():
                log.warning(f"Impersonation error: {e}, retrying with chrome120")
                session.impersonate = "chrome120"
                await asyncio.sleep(1)
                continue
            log.exception(f"API request error: {e}")
            await asyncio.sleep(2)
            continue

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
# AVATAR HELPER - WITH RESIZING
# --------------------------------------------
def resize_avatar(image_data):
    """
    Resize and compress avatar image to fit Discord's requirements
    - Max size: 256 KB
    - Max dimension: 1024x1024
    """
    try:
        # Open the image
        img = Image.open(io.BytesIO(image_data))
        
        # Convert to RGB if needed (for PNG with alpha, etc.)
        if img.mode in ('RGBA', 'LA', 'P'):
            # Create a white background
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Resize if too large
        width, height = img.size
        max_dim = MAX_AVATAR_DIMENSION
        if width > max_dim or height > max_dim:
            ratio = min(max_dim / width, max_dim / height)
            new_width = int(width * ratio)
            new_height = int(height * ratio)
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            log.info(f"Resized avatar from {width}x{height} to {new_width}x{new_height}")
        
        # Try different quality levels to get under 256 KB
        qualities = [85, 75, 65, 55, 45, 35, 25]
        
        for quality in qualities:
            output = io.BytesIO()
            img.save(output, format='JPEG', quality=quality, optimize=True)
            compressed = output.getvalue()
            
            if len(compressed) <= MAX_AVATAR_SIZE:
                log.info(f"Avatar compressed to {len(compressed)} bytes at quality {quality}")
                return compressed, "image/jpeg"
        
        # If still too large, use lowest quality and hope for the best
        output = io.BytesIO()
        img.save(output, format='JPEG', quality=20, optimize=True)
        compressed = output.getvalue()
        log.warning(f"Avatar still {len(compressed)} bytes after max compression")
        return compressed, "image/jpeg"
        
    except Exception as e:
        log.exception(f"Error resizing avatar: {e}")
        return None, None

# --------------------------------------------
# DISCORD SERVER JOIN + ONBOARDING
# --------------------------------------------
async def join_server_and_onboard(invite_code):
    """
    Join a Discord server using an invite code and complete onboarding
    """
    try:
        # Step 1: Validate the invite first
        log.info(f"Checking invite: {invite_code}")
        invite_info = await api_request("GET", f"https://discord.com/api/v9/invites/{invite_code}")
        
        if invite_info.status_code == 404:
            return False, "Invite invalid or expired"
        
        if invite_info.status_code != 200:
            return False, f"Failed to check invite (status {invite_info.status_code})"
        
        invite_data = invite_info.json()
        guild_name = invite_data.get('guild', {}).get('name', 'Unknown Server')
        log.info(f"Found server: {guild_name}")
        
        # Step 2: Join the server
        log.info(f"Attempting to join {guild_name}...")
        join_response = await api_request(
            "POST", 
            f"https://discord.com/api/v9/invites/{invite_code}",
            json={}
        )
        
        if join_response.status_code == 200:
            join_data = join_response.json()
            guild_id = join_data.get('guild', {}).get('id')
            channel_id = join_data.get('channel', {}).get('id')
            
            log.info(f"Successfully joined {guild_name} (ID: {guild_id})")
            
            # Step 3: Handle onboarding (if present)
            if guild_id:
                await complete_onboarding(guild_id)
            
            return True, f"Joined {guild_name} successfully!"
        else:
            try:
                error = join_response.json()
                error_msg = error.get('message', 'Unknown error')
            except:
                error_msg = join_response.text[:100]
            return False, f"Failed to join: {error_msg}"
            
    except Exception as e:
        log.exception(f"Error joining server: {e}")
        return False, str(e)

async def complete_onboarding(guild_id):
    """
    Complete onboarding for a server by fetching onboarding questions
    and submitting random answers
    """
    try:
        log.info(f"Checking onboarding for guild {guild_id}")
        
        # Get the onboarding data
        onboarding_resp = await api_request(
            "GET", 
            f"https://discord.com/api/v9/guilds/{guild_id}/onboarding"
        )
        
        if onboarding_resp.status_code != 200:
            log.info(f"No onboarding required for guild {guild_id}")
            return
        
        onboarding_data = onboarding_resp.json()
        prompts = onboarding_data.get('prompts', [])
        
        if not prompts:
            log.info(f"No onboarding prompts for guild {guild_id}")
            return
        
        log.info(f"Found {len(prompts)} onboarding prompts")
        
        # Prepare answers for each prompt
        answers = {}
        for prompt in prompts:
            prompt_id = prompt.get('id')
            title = prompt.get('title', 'Unknown prompt')
            options = prompt.get('options', [])
            
            if not options:
                log.info(f"No options for prompt: {title}")
                continue
            
            # Randomly select options for this prompt
            is_multiple = prompt.get('type') == 1  # 1 = multiple, 0 = single
            selected_options = []
            
            if is_multiple:
                # Select random number of options (at least 1, up to half of available)
                num_to_select = random.randint(1, max(1, len(options) // 2))
                selected_options = random.sample(options, min(num_to_select, len(options)))
            else:
                # Select single random option
                selected_options = [random.choice(options)]
            
            # Build answer format
            option_ids = [opt.get('id') for opt in selected_options if opt.get('id')]
            if option_ids:
                answers[prompt_id] = option_ids
                log.info(f"Selected {len(option_ids)} option(s) for prompt: {title}")
        
        if not answers:
            log.info("No answers to submit for onboarding")
            return
        
        # Submit onboarding answers
        log.info(f"Submitting onboarding answers for guild {guild_id}")
        onboarding_submit = await api_request(
            "PUT",
            f"https://discord.com/api/v9/guilds/{guild_id}/onboarding/answers",
            json={
                "answers": answers
            }
        )
        
        if onboarding_submit.status_code == 200:
            log.info(f"Onboarding completed successfully for guild {guild_id}")
        else:
            log.warning(f"Failed to submit onboarding answers: {onboarding_submit.status_code}")
            
    except Exception as e:
        log.exception(f"Error completing onboarding: {e}")

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
# IGNORE / SPAM DETECTION
# --------------------------------------------
ignored_users = {}
mention_timestamps = {}
SPAM_WINDOW = 5
SPAM_THRESHOLD = 5
AUTO_IGNORE_DURATION = 3600

def is_ignored(user_id):
    if user_id not in ignored_users:
        return False
    expiry = ignored_users[user_id]
    if expiry == 0:
        return True
    if time.time() < expiry:
        return True
    del ignored_users[user_id]
    return False

def check_mention_spam(user_id):
    now = time.time()
    if user_id not in mention_timestamps:
        mention_timestamps[user_id] = []
    timestamps = mention_timestamps[user_id]
    timestamps = [t for t in timestamps if now - t <= SPAM_WINDOW]
    timestamps.append(now)
    mention_timestamps[user_id] = timestamps
    if len(timestamps) >= SPAM_THRESHOLD:
        ignored_users[user_id] = now + AUTO_IGNORE_DURATION
        log.info(f"Auto-ignored user {user_id} for {AUTO_IGNORE_DURATION}s due to spam")
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
# CONTEXT BUILDER
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
    "napisz wulgarna agresywna odpowiedz do {username}, na podstawie tego co napisal. krotko ostro z jadem.",
    "zjeb {username} za to co napisal. ostro bez emocji.",
    "odpierdol sie do {username} za jego glupote. krotko.",
    "wyzwij {username} od czegos obraźliwego za jego wiadomosc.",
]

async def send_spontaneous_message():
    global last_spontaneous_time
    
    channel_id = SPONTANEOUS_CHANNEL_ID
    if not channel_id:
        return
    
    now = time.time()
    if now - last_spontaneous_time < SPONTANEOUS_COOLDOWN:
        return
    
    if random.random() > SPONTANEOUS_CHANCE:
        return
    
    target_user_id = None
    target_display_name = None
    target_content = None
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
                if is_ignored(str(m["author"]["id"])):
                    continue
                display_name = m["author"].get("global_name") or m["author"].get("username", "user")
                users.append((m["author"]["id"], display_name, m.get("content", "")))
            if users:
                target_user_id, target_display_name, target_content = random.choice(users)
    except Exception as e:
        log.warning(f"Failed to fetch users: {e}")
    
    if not target_user_id:
        try:
            prompt = "napisz agresywna wulgarna wiadomosc do nikogo. krotko ostro."
            reply = get_gemini_response(prompt)
            if reply:
                msg = reply.strip()
                if msg:
                    await send_typing(channel_id)
                    await asyncio.sleep(0.5)
                    await send_message(channel_id, msg)
                    last_spontaneous_time = now
        except Exception as e:
            log.exception("Failed to send generic message")
        return
    
    try:
        ping_msg = f"@{target_display_name}"
        await send_typing(channel_id)
        await asyncio.sleep(0.3)
        await send_message(channel_id, ping_msg)
        
        prompt_template = random.choice(SPONTANEOUS_PROMPTS)
        if target_content:
            prompt = f"wiadomosc uzytkownika: {target_content}\n{prompt_template.format(username=target_display_name)}"
        else:
            prompt = prompt_template.format(username=target_display_name)
        
        reply = get_gemini_response(prompt)
        if not reply:
            return
        msg_text = reply.strip()
        if not msg_text:
            return
        
        await asyncio.sleep(0.5)
        await send_typing(channel_id)
        await asyncio.sleep(0.3)
        await send_message(channel_id, msg_text)
        last_spontaneous_time = now
    except Exception as e:
        log.exception("Failed to send spontaneous message")

async def spontaneous_loop():
    while True:
        await asyncio.sleep(SPONTANEOUS_CHECK_INTERVAL)
        await send_spontaneous_message()

# --------------------------------------------
# PROMPT MANAGEMENT
# --------------------------------------------
async def update_prompt(new_prompt):
    global current_system_prompt
    current_system_prompt = new_prompt
    log.info("System prompt updated")
    return True

# --------------------------------------------
# PROFILE CHANGE FUNCTIONS
# --------------------------------------------
async def change_avatar(image_data: bytes):
    """Change avatar with automatic resizing"""
    # Resize the avatar
    resized_data, mime_type = resize_avatar(image_data)
    
    if resized_data is None:
        return False, "Failed to process image"
    
    # Encode to base64
    b64 = base64.b64encode(resized_data).decode()
    payload = {"avatar": f"data:{mime_type};base64,{b64}"}
    
    # Send request
    resp = await api_request("PATCH", "https://discord.com/api/v9/users/@me", json=payload)
    
    if resp.status_code == 200:
        return True, f"Avatar zmieniony! ({len(resized_data)} bytes)"
    else:
        try:
            error_data = resp.json()
            error_msg = error_data.get('message', 'Brak szczegółów')
        except:
            error_msg = resp.text[:200]
        return False, f"Nie udało się (status {resp.status_code}): {error_msg}"

async def change_display_name(new_display: str):
    if len(new_display) < 2 or len(new_display) > 32:
        return False, "Display name must be 2-32 characters"
    payload = {"global_name": new_display}
    resp = await api_request("PATCH", "https://discord.com/api/v9/users/@me", json=payload)
    if resp.status_code == 200:
        return True, f"Zmieniono na: {new_display}"
    else:
        try:
            error_data = resp.json()
            error_msg = error_data.get('message', 'Brak szczegółów')
        except:
            error_msg = resp.text[:100]
        return False, f"Nie udało się (status {resp.status_code}): {error_msg}"

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
            log.warning(f"Rate limit. Sleeping {wait:.1f}s")
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
    global current_ws, voice_channels, persistent_voice_channels, current_system_prompt
    
    content = msg.get("content", "")
    channel_id = msg["channel_id"]
    current_guild = msg.get("guild_id")
    msg_id = msg["id"]
    parts = content.split()

    if not parts:
        return

    cmd = parts[0].lower()
    log.info(f"Command: {cmd} from guild {current_guild}")

    # --------------------------------------------
    # .server - JOIN SERVER VIA INVITE
    # --------------------------------------------
    if cmd == ".server":
        author_id = str(msg["author"]["id"])
        if author_id not in OWNER_IDS:
            await send_reply(channel_id, msg_id, "nie masz uprawnien")
            return
        
        if len(parts) < 2:
            await send_reply(channel_id, msg_id, "uzycie: .server <invite_code>")
            await send_reply(channel_id, msg_id, "np: .server discord" + " (dla discord.gg/discord)")
            return
        
        invite_code = parts[1].strip()
        # Remove any URL parts if user pasted full link
        if "discord.gg/" in invite_code:
            invite_code = invite_code.split("discord.gg/")[-1].split("?")[0]
        elif "discord.com/invite/" in invite_code:
            invite_code = invite_code.split("discord.com/invite/")[-1].split("?")[0]
        elif "discordapp.com/invite/" in invite_code:
            invite_code = invite_code.split("discordapp.com/invite/")[-1].split("?")[0]
        
        # Clean up the code
        invite_code = invite_code.split("/")[0].strip()
        
        if not invite_code:
            await send_reply(channel_id, msg_id, "nieprawidlowy kod zaproszenia")
            return
        
        await send_reply(channel_id, msg_id, f"próba dołączenia do serwera z kodem: {invite_code}...")
        
        success, message = await join_server_and_onboard(invite_code)
        
        if success:
            await send_reply(channel_id, msg_id, f"✅ {message}")
        else:
            await send_reply(channel_id, msg_id, f"❌ {message}")
        return

    if cmd == ".test" or cmd == ".ping":
        status = "ws: ok" if current_ws else "ws: None"
        await send_reply(channel_id, msg_id, f"bot zyje {status}")
        return

    if cmd == ".servers":
        if voice_channels:
            lines = ["glosowe:"] + [f"{gid} -> {cid}" for gid, cid in voice_channels.items()]
            await send_reply(channel_id, msg_id, "\n".join(lines))
        else:
            await send_reply(channel_id, msg_id, "nie jestem w zadnym kanale glosowym")
        return

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

    if cmd == ".resetprompt":
        if await update_prompt(DEFAULT_SYSTEM_PROMPT):
            await send_reply(channel_id, msg_id, "przywrocono domyslny prompt")
        else:
            await send_reply(channel_id, msg_id, "nie udalo sie przywrocic promptu")
        return

    # --------------------------------------------
    # .avatar - WITH AUTO-RESIZE
    # --------------------------------------------
    if cmd == ".avatar":
        author_id = str(msg["author"]["id"])
        if author_id not in OWNER_IDS:
            await send_reply(channel_id, msg_id, "nie masz uprawnien")
            return
        
        image_data = None
        
        # Check if image is attached
        if msg.get("attachments"):
            att = msg["attachments"][0]
            url = att["url"]
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(url) as resp:
                        if resp.status == 200:
                            image_data = await resp.read()
                        else:
                            await send_reply(channel_id, msg_id, f"nie udalo sie pobrac (status {resp.status})")
                            return
            except Exception as e:
                log.exception("Failed to download attachment")
                await send_reply(channel_id, msg_id, "nie moge pobrac zalacznika")
                return
        # Check if URL is provided
        elif len(parts) >= 2:
            img_url = parts[1]
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(img_url) as resp:
                        if resp.status == 200:
                            image_data = await resp.read()
                        else:
                            await send_reply(channel_id, msg_id, f"nie udalo sie pobrac (status {resp.status})")
                            return
            except Exception as e:
                log.exception("Failed to download image")
                await send_reply(channel_id, msg_id, "nie moge pobrac obrazu")
                return
        else:
            await send_reply(channel_id, msg_id, "uzyj: .avatar (z zalacznikiem) lub .avatar <url>")
            return

        if image_data is None:
            await send_reply(channel_id, msg_id, "nie udalo sie pobrac obrazu")
            return

        # Send initial message
        await send_reply(channel_id, msg_id, "⏳ przetwarzanie obrazka...")
        
        # Change avatar with auto-resize
        success, message = await change_avatar(image_data)
        
        if success:
            await send_reply(channel_id, msg_id, f"✅ {message}")
        else:
            await send_reply(channel_id, msg_id, f"❌ {message}")
        return

    if cmd == ".display":
        if len(parts) < 2:
            await send_reply(channel_id, msg_id, "uzycie: .display <nowy_display_name>")
            return
        new_display = " ".join(parts[1:])
        success, message = await change_display_name(new_display)
        if success:
            await send_reply(channel_id, msg_id, message)
        else:
            await send_reply(channel_id, msg_id, f"nie udalo sie: {message}")
        return

    if cmd == ".ignore":
        author_id = str(msg["author"]["id"])
        if author_id not in OWNER_IDS:
            await send_reply(channel_id, msg_id, "nie masz uprawnien")
            return
        if len(parts) < 2:
            await send_reply(channel_id, msg_id, "uzycie: .ignore <user_id>")
            return
        target = parts[1]
        if not target.isdigit():
            await send_reply(channel_id, msg_id, "id musi byc liczba")
            return
        ignored_users[target] = 0
        mention_timestamps.pop(target, None)
        log.info(f"Manually ignored user {target}")
        await send_reply(channel_id, msg_id, f"uzytkownik {target} zignorowany na stale")
        return

    if cmd == ".unignore":
        author_id = str(msg["author"]["id"])
        if author_id not in OWNER_IDS:
            await send_reply(channel_id, msg_id, "nie masz uprawnien")
            return
        if len(parts) < 2:
            await send_reply(channel_id, msg_id, "uzycie: .unignore <user_id>")
            return
        target = parts[1]
        if not target.isdigit():
            await send_reply(channel_id, msg_id, "id musi byc liczba")
            return
        if target in ignored_users:
            del ignored_users[target]
            log.info(f"Unignored user {target}")
            await send_reply(channel_id, msg_id, f"uzytkownik {target} usuniety z ignorowanych")
        else:
            await send_reply(channel_id, msg_id, f"uzytkownik {target} nie jest ignorowany")
        return

    if cmd == ".ignorelist":
        author_id = str(msg["author"]["id"])
        if author_id not in OWNER_IDS:
            await send_reply(channel_id, msg_id, "nie masz uprawnien")
            return
        if not ignored_users:
            await send_reply(channel_id, msg_id, "brak ignorowanych uzytkownikow")
            return
        lines = ["ignorowani:"]
        for uid, exp in ignored_users.items():
            if exp == 0:
                lines.append(f"{uid} (na stale)")
            else:
                remaining = int(exp - time.time())
                lines.append(f"{uid} (jeszcze {remaining}s)")
        await send_reply(channel_id, msg_id, "\n".join(lines))
        return

    if current_ws is None:
        await send_reply(channel_id, msg_id, "websocket nieaktywny")
        return

    if cmd == ".join":
        if len(parts) == 2:
            guild_id = current_guild
            vc_id = parts[1]
        elif len(parts) == 3:
            guild_id = parts[1]
            vc_id = parts[2]
        else:
            await send_reply(channel_id, msg_id, "uzycie: .join <channel_id> albo .join <guild_id> <channel_id>")
            return

        if not guild_id or not guild_id.isdigit() or not vc_id.isdigit():
            await send_reply(channel_id, msg_id, "id musi byc liczba")
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
            await send_reply(channel_id, msg_id, f"dolaczono do <#{vc_id}> w serwerze {guild_id}")
        except Exception as e:
            log.exception("Voice join error")
            await send_reply(channel_id, msg_id, f"nie moge dolaczyc: {str(e)[:50]}")
        return

    if cmd == ".leave":
        if len(parts) == 1:
            guild_id = current_guild
        elif len(parts) == 2:
            guild_id = parts[1]
        else:
            await send_reply(channel_id, msg_id, "uzycie: .leave albo .leave <guild_id>")
            return

        if not guild_id or not guild_id.isdigit():
            await send_reply(channel_id, msg_id, "id musi byc liczba")
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
                await send_reply(channel_id, msg_id, f"opuszczono voice w serwerze {guild_id}")
            except Exception as e:
                log.exception("Voice leave error")
                await send_reply(channel_id, msg_id, f"nie moge opuscic: {str(e)[:50]}")
        else:
            await send_reply(channel_id, msg_id, f"nie jestem w voice na serwerze {guild_id}")
        return

    if cmd == ".status":
        if len(parts) == 1:
            guild_id = current_guild
        elif len(parts) == 2:
            guild_id = parts[1]
        else:
            await send_reply(channel_id, msg_id, "uzycie: .status albo .status <guild_id>")
            return

        if not guild_id or not guild_id.isdigit():
            await send_reply(channel_id, msg_id, "id musi byc liczba")
            return

        if guild_id in voice_channels:
            await send_reply(channel_id, msg_id, f"jestem w <#{voice_channels[guild_id]}> w {guild_id}")
        else:
            await send_reply(channel_id, msg_id, f"nie jestem w voice na {guild_id}")
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

    if is_ignored(author_id):
        log.debug(f"Ignoring user {author_id}")
        return

    if content.startswith("."):
        if author_id in OWNER_IDS or (self_user_id and author_id == self_user_id):
            await handle_command(msg)
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

    if check_mention_spam(author_id):
        log.info(f"User {author_id} auto-ignored due to spam")
        return

    await send_typing(channel_id)
    
    context = await build_context(channel_id, msg, guild_id)
    
    author_name = msg["author"].get("global_name") or msg["author"]["username"]
    timestamp = time.time()
    if guild_id:
        add_to_guild_history(f"guild_{guild_id}", author_name, msg["content"], msg_id, timestamp)
    else:
        add_to_guild_history(f"dm_{channel_id}", author_name, msg["content"], msg_id, timestamp)

    try:
        prompt = f"Kontekst:\n{context}\n\nOdpowiedz na ostatnia wiadomosc."
        reply_text = get_gemini_response(prompt)
        if not reply_text:
            log.info("Gemini returned empty response - skipping reply.")
            return
    except Exception as e:
        log.exception("AI failed - skipping reply.")
        return

    for i in range(0, len(reply_text), 1900):
        chunk = reply_text[i:i+1900]
        await send_reply(channel_id, msg_id, chunk)
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

    if is_ignored(author_id):
        return

    if content.startswith("."):
        if author_id in OWNER_IDS or (self_user_id and author_id == self_user_id):
            await message_queue.put(msg)
        return

    channel_type = msg.get("channel_type")
    if channel_type in ("DM", "GROUP_DM"):
        await message_queue.put(msg)
        return
    
    mentioned = any(m["id"] == self_user_id for m in msg.get("mentions", []))
    replied = False
    if msg.get("referenced_message"):
        ref = msg["referenced_message"]
        if ref.get("author", {}).get("id") == self_user_id:
            replied = True    
    if mentioned or replied:
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
            continue
        for guild_id, channel_id in list(persistent_voice_channels.items()):
            if guild_id not in voice_channels:
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
                    log.warning(f"Voice keepalive error for {guild_id}: {e}")
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
            except Exception as e:
                log.warning(f"Voice keepalive error for {guild_id}: {e}")

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
            log.info("Identify sent")
        
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
        log.error("Token invalid!")
        return
    self_user_id = str(resp.json()["id"])
    log.info(f"Token valid. ID: {self_user_id}")
    
    asyncio.create_task(rate_limiter())
    asyncio.create_task(process_worker())
    
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
