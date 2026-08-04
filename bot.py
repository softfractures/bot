import os
import json
import asyncio
import logging
import random
import time
import base64
import google.generativeai as genai
from dotenv import load_dotenv
import websockets
from curl_cffi import requests

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

# Faster delays
MIN_REPLY_DELAY = 2.0
MAX_REPLY_DELAY = 4.0
CHUNK_DELAY = 0.5
MAX_MESSAGES_PER_MINUTE = 5
HISTORY_CACHE_TTL = 60
MAX_HISTORY_PER_GUILD = 50

# Spontaneous messages settings
SPONTANEOUS_CHANCE = 0.03  # 3% chance per minute to send spontaneous message
SPONTANEOUS_COOLDOWN = 180  # 3 minutes cooldown between spontaneous messages
SPONTANEOUS_CHECK_INTERVAL = 60  # Check every minute

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

# Default system prompt
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
active_channels = set()  # Track active channels for spontaneous messages

guild_histories = {}
channel_history_cache = {}

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
    "napisz losowa wulgarna wiadomosc bez powodu",
    "zaczep kogos w kanale",
    "napisz co myslisz o ludziach w tym kanale",
    "wyrzuc z siebie agresje",
    "powiedz co chcesz komus z kanalu"
]

async def send_spontaneous_message():
    global last_spontaneous_time
    
    if not active_channels:
        return
    
    now = time.time()
    if now - last_spontaneous_time < SPONTANEOUS_COOLDOWN:
        return
    
    if random.random() > SPONTANEOUS_CHANCE:
        return
    
    # Pick a random active channel
    channel_id = random.choice(list(active_channels))
    
    # Generate spontaneous message using AI
    try:
        prompt = random.choice(SPONTANEOUS_PROMPTS)
        reply = model.generate_content(prompt)
        if reply.candidates:
            msg = (reply.text or "").strip()
            if msg:
                log.info(f"Sending spontaneous message to channel {channel_id}: {msg}")
                await send_typing(channel_id)
                await asyncio.sleep(1.5)
                await send_message(channel_id, msg)
                last_spontaneous_time = now
    except Exception as e:
        log.exception("Failed to generate spontaneous message")

async def spontaneous_loop():
    """Background task that periodically checks if bot should send spontaneous messages"""
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
            await asyncio.sleep(random.uniform(MIN_REPLY_DELAY, MAX_REPLY_DELAY))

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

    if cmd == ".test" or cmd == ".ping":
        status = "ws: ok" if current_ws else "ws: None"
        await send_reply(channel_id, msg_id, f"bot żyje, {status}")
        return

    if cmd == ".servers":
        if voice_channels:
            lines = ["głosowe:"] + [f"{gid} -> {cid}" for gid, cid in voice_channels.items()]
            await send_reply(channel_id, msg_id, "\n".join(lines))
        else:
            await send_reply(channel_id, msg_id, "nie jestem w żadnym kanale głosowym")
        return

    if cmd == ".prompt":
        if len(parts) > 1:
            # Update prompt
            new_prompt = " ".join(parts[1:])
            if await update_prompt(new_prompt):
                await send_reply(channel_id, msg_id, f"zmieniono prompt na: {new_prompt[:100]}...")
            else:
                await send_reply(channel_id, msg_id, "nie udalo sie zmienic promptu")
        else:
            # Show current prompt
            await send_reply(channel_id, msg_id, f"obecny prompt: {current_system_prompt[:200]}...")
        return

    if cmd == ".resetprompt":
        if await update_prompt(DEFAULT_SYSTEM_PROMPT):
            await send_reply(channel_id, msg_id, "przywrocono domyslny prompt")
        else:
            await send_reply(channel_id, msg_id, "nie udalo sie przywrocic promptu")
        return

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
    
    # Ignore bots
    if is_bot:
        return

    # If it's a command (starts with .), only process if author is in OWNER_IDS
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

    # Check if it's a mention or reply to self
    mentioned = any(m["id"] == self_user_id for m in msg.get("mentions", []))
    replied = False
    if msg.get("referenced_message"):
        ref = msg["referenced_message"]
        if ref.get("author", {}).get("id") == self_user_id:
            replied = True
    
    channel_type = msg.get("channel_type")
    is_dm = channel_type in ("DM", "GROUP_DM")
    
    # Only respond if mentioned, replied to, or DM
    if not (mentioned or replied or is_dm):
        return

    # Faster typing & thinking
    await asyncio.sleep(random.uniform(0.5, 1.5))
    await send_typing(channel_id)
    await asyncio.sleep(random.uniform(0.3, 0.6))
    
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

    await send_typing(channel_id)
    log.info("Waiting 2 seconds (typing) before sending reply...")
    await asyncio.sleep(2.0)

    for i in range(0, len(reply_text), 1900):
        chunk = reply_text[i:i+1900]
        await send_reply(channel_id, msg_id, chunk)
        await asyncio.sleep(CHUNK_DELAY + random.uniform(0, 0.3))

# --------------------------------------------
# FILTER
# --------------------------------------------
async def filter_and_queue(msg):
    global self_user_id
    author_id = str(msg["author"]["id"])
    content = msg.get("content", "")
    is_bot = msg.get("author", {}).get("bot", False)
    
    # Track active channels
    channel_id = msg.get("channel_id")
    if channel_id and not is_bot:
        active_channels.add(channel_id)
    
    # Ignore bots
    if is_bot:
        return

    # Handle commands: only from owners
    if content.startswith("."):
        if author_id in OWNER_IDS or (self_user_id and author_id == self_user_id):
            log.info(f"Queueing command from owner: {content}")
            await message_queue.put(msg)
        return

    # Non‑command messages: queue if it's a DM/group DM or mention/reply to self
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
