import discord
from discord import option
from discord.ext import commands
import yt_dlp
import os
import asyncio
import subprocess
from typing import Dict, Any, Optional
import time
import traceback

# --- DIAGNOSTIC SETUP ---
print("🚀 Starting bot diagnostics...")

# Check FFmpeg installation
try:
    subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
    print("✅ FFmpeg found and working")
except:
    print("❌ CRITICAL: FFmpeg not found! Install it: https://ffmpeg.org/download.html")
    exit(1)

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = discord.Bot(intents=intents)

# --- HIGH-QUALITY Audio Configuration ---

COOKIE_PATH = 'youtube_cookies.txt'
use_cookies = os.path.exists(COOKIE_PATH)

# Optional proxy (for bypass) via env var, e.g. "http://127.0.0.1:8080"
YTDLP_PROXY = os.getenv('YTDLP_PROXY', None)
# Optional source address to match browser IP when solving captchas
YTDLP_SOURCE_ADDRESS = os.getenv('YTDLP_SOURCE_ADDRESS', None)

# A small set of user-agents to try when bypassing anti-bot protections
USER_AGENTS = [
    # Common desktop Chrome UA
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
    " Chrome/118.0.5993.90 Safari/537.36",
    # Common Firefox UA
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:119.0) Gecko/20100101 Firefox/119.0",
    # Android UA (already used in extractor args)
    "Mozilla/5.0 (Linux; Android 13; SM-G991U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.5993.90 Mobile Safari/537.36",
]

# Base yt-dlp options (kept lightweight)
YDL_OPTIONS = {
    # Faster format selection; allow opus where possible
    'format': 'bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio',
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': YTDLP_SOURCE_ADDRESS if YTDLP_SOURCE_ADDRESS else '0.0.0.0',
    'cookiefile': COOKIE_PATH if use_cookies else None,
    # Speed up extraction where possible
    'extract_flat': 'in_playlist',
    'http_headers': {
        'User-Agent': USER_AGENTS[-1],  # default to android-like UA for performance
    },
    'extractor_args': {
        'youtube': {
            'player_client': ['android'],  # Faster than web client
            'max_comments': ['0'],
        }
    },
    # Timeout/retry settings
    'socket_timeout': 10,
    'retries': 2,
    # Small HTTP chunk size to reduce risk of YouTube throttling (>10MB chunks)
    'http_chunk_size': 1048576,  # 1MB
    # sometimes useful to avoid certificate issues in weird environments
    'nocheckcertificate': True,
    # don't use cache to keep behavior predictable
    'cachedir': False,
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -acodec libopus -ar 48000 -ac 2 -b:a 96k',
}

# --- Music Queue Management ---
class GuildMusicState:
    def __init__(self):
        self.entries: list[Dict[str, Any]] = []
        self.current_index: int = 0
        self.playing: bool = False
        self.loop: bool = False
        self.volume: float = 1.0
        self.last_error: str = ""

    def next_song(self) -> Optional[Dict[str, Any]]:
        if not self.entries:
            return None
        
        if self.loop and self.current_index >= len(self.entries) - 1:
            self.current_index = 0
        else:
            self.current_index += 1
        
        if self.current_index >= len(self.entries):
            return None
        
        return self.entries[self.current_index]

    @property
    def current_song(self) -> Optional[Dict[str, Any]]:
        if not self.entries or self.current_index >= len(self.entries):
            return None
        return self.entries[self.current_index]

music_states: Dict[int, GuildMusicState] = {}

# --- Helper Functions ---
async def ensure_voice_client(ctx: discord.ApplicationContext) -> Optional[discord.VoiceClient]:
    """Ensure bot is connected to voice channel."""
    if not ctx.author.voice:
        await ctx.respond("You are not in a voice channel!", ephemeral=True)
        return None

    voice_channel = ctx.author.voice.channel
    voice_client = ctx.voice_client

    if not voice_client:
        print(f"🔌 Connecting to voice channel: {voice_channel.name}")
        await voice_channel.connect()
        voice_client = ctx.voice_client
    elif voice_client.channel != voice_channel:
        print(f"🔌 Moving to voice channel: {voice_channel.name}")
        await voice_client.move_to(voice_channel)
    
    return voice_client

def get_music_state(guild_id: int) -> GuildMusicState:
    """Get or create music state for guild."""
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState()
    return music_states[guild_id]

async def send_to_guild(guild: discord.Guild, message: str):
    """Send message to the first available text channel."""
    try:
        channel = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel) and c.permissions_for(guild.me).send_messages,
            guild.text_channels
        )
        if channel:
            await channel.send(message)
    except Exception as e:
        print(f"❌ Failed to send message to guild {guild.id}: {e}")

async def extract_with_timeout(ydl, url, timeout=15):
    """Extract info with timeout to prevent hangs."""
    loop = asyncio.get_event_loop()
    start_time = time.time()
    
    try:
        info = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False)),
            timeout=timeout
        )
        print(f"✅ Extraction completed in {time.time() - start_time:.2f}s")
        return info
    except asyncio.TimeoutError:
        print(f"❌ Extraction timed out after {timeout}s")
        return None
    except Exception as e:
        print(f"❌ Extraction error: {e}")
        print(traceback.format_exc())
        return None

async def extract_with_auto_bypass(url: str, guild: Optional[discord.Guild]=None, base_timeout=20):
    """
    Try extracting the URL using several strategies to bypass common anti-bot issues.
    This tries:
      1) Base options
      2) If failure suggests Cloudflare/403/429 or timeout, retry with a list of browser UAs
      3) If a proxy is configured (YTDLP_PROXY), try with proxy
      4) If cookies file exists, ensure it's used
    Returns the info dict on success or None on total failure.
    """
    print(f"🔐 Starting auto-bypass extraction for: {url}")
    attempted = []
    # Helper to create a YoutubeDL instance with merged options
    def make_ydl(options_overrides: dict):
        merged = YDL_OPTIONS.copy()
        # deep-merge http_headers if provided
        headers = merged.get('http_headers', {}).copy()
        overrides_headers = options_overrides.get('http_headers', {})
        headers.update(overrides_headers)
        merged['http_headers'] = headers
        # other overrides
        for k, v in options_overrides.items():
            if k == 'http_headers':
                continue
            merged[k] = v
        return yt_dlp.YoutubeDL(merged)

    # First, try a normal extraction
    try:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = await extract_with_timeout(ydl, url, timeout=base_timeout)
            if info:
                return info
    except Exception as e:
        print(f"❌ Base extraction threw: {e}")

    # Build a list of strategies
    strategies = []

    # 1) Try different user-agents (and use cookies if available)
    for ua in USER_AGENTS:
        strat = {'http_headers': {'User-Agent': ua}}
        if use_cookies:
            strat['cookiefile'] = COOKIE_PATH
        if YTDLP_PROXY:
            strat['proxy'] = YTDLP_PROXY
        strategies.append(('UA '+ua.split(' ')[0], strat))

    # 2) If proxy defined, try just proxy (no UA change)
    if YTDLP_PROXY:
        strategies.append(('Proxy', {'proxy': YTDLP_PROXY}))

    # 3) Last-ditch larger timeout with cookies + proxy + desktop UA
    desktop_ua = USER_AGENTS[0]
    fallback = {'http_headers': {'User-Agent': desktop_ua}, 'socket_timeout': 20, 'retries': 4}
    if use_cookies:
        fallback['cookiefile'] = COOKIE_PATH
    if YTDLP_PROXY:
        fallback['proxy'] = YTDLP_PROXY
    strategies.append(('Fallback', fallback))

    # Try each strategy
    for name, opts in strategies:
        attempted.append(name)
        print(f"🛠️ Bypass attempt '{name}' with options: {{ {k: v for k, v in opts.items() if k != 'http_headers'} }}")
        try:
            ydl = make_ydl(opts)
            info = await extract_with_timeout(ydl, url, timeout=base_timeout + 5)
            if info:
                print(f"✅ Bypass strategy '{name}' succeeded")
                return info
            else:
                print(f"❌ Bypass strategy '{name}' returned no info")
        except Exception as e:
            print(f"❌ Bypass strategy '{name}' error: {e}")
            print(traceback.format_exc())

    # If we're here, all strategies failed
    msg = f"Failed extraction after attempts: {attempted}"
    print(f"❌ Auto-bypass failed: {msg}")
    if guild:
        # Friendly guidance to server about next steps (cookies/proxy)
        guidance = "If you're getting Cloudflare/403/429 errors, try the following:\n" 
                   "- Ensure youtube_cookies.txt is present and fresh (exported from your browser within ~30 minutes)\n" 
                   "- Set YTDLP_PROXY env var to a working proxy (if you have a whitelisted IP)\n" 
                   "- Set YTDLP_SOURCE_ADDRESS to the IP you solved CAPTCHA from (if applicable)\n" 
                   "- Provide a current browser User-Agent in YDL_OPTIONS or via environment\n"
        await send_to_guild(guild, f"❌ Extraction failed for {url}. {guidance}")
    return None

# --- Always-on Autoplay Helper ---
async def autoplay_from_last_song(state: GuildMusicState, guild: discord.Guild) -> bool:
    """
    Try to find and queue a related song using the last played song's title.
    Always automatically invoked when queue runs out.
    """
    last_song = state.current_song
    last_query = None

    if last_song:
        last_query = last_song.get('title')
    elif state.entries:
        last_query = state.entries[-1].get('title')
    if not last_query:
        return False

    autoplay_query = f"ytsearch:{last_query} music"
    try:
        info = await extract_with_auto_bypass(autoplay_query, guild=guild, base_timeout=15)
        if info:
            entries = info.get('entries', [info])
            # Exclude tracks with the same video id
            used_ids = {s.get('id') for s in state.entries if 'id' in s}
            for entry in entries:
                if entry.get('id') not in used_ids:
                    state.entries.append(entry)
                    print(f"[AUTOPLAY] Added similar song '{entry.get('title', 'unknown')}'")
                    await send_to_guild(guild, f"🎶 Autoplay: Now queueing a related track: **{entry.get('title','(unknown)')}**")
                    return True
    except Exception as e:
        print(f"❌ Autoplay failed: {e}")
    return False

# --- Modified play_next with always-on autoplay/radio ---
async def play_next(guild_id: int):
    """Play the next song in queue. With always-on autoplay mode."""
    print(f"\n--- play_next() called for guild {guild_id} ---")
    state = get_music_state(guild_id)
    guild = bot.get_guild(guild_id)
    voice_client = discord.utils.get(bot.voice_clients, guild__id=guild_id)

    if not voice_client or not voice_client.is_connected():
        print(f"❌ Voice client not connected for guild {guild_id}")
        music_states.pop(guild_id, None)
        return

    if not state.playing:
        print(f"⏸️ Playing is False for guild {guild_id}")
        return

    next_song = state.next_song()
    if not next_song:
        print(f"⏹️ Queue is empty – triggering autoplay (always-on)...")
        did_autoplay = await autoplay_from_last_song(state, guild)
        if did_autoplay:
            await play_next(guild_id)
            return
        else:
            # Give up after 3 sequential autoplay failures to prevent infinite loop in bad scenarios
            if not hasattr(state, 'autoplay_failures'):
                state.autoplay_failures = 0
            state.autoplay_failures += 1
            if state.autoplay_failures <= 3:
                await send_to_guild(guild, f"⚡ Trying to find music related to previous song (attempt {state.autoplay_failures})…")
                await asyncio.sleep(2)
                await play_next(guild_id)
                return
            else:
                await send_to_guild(guild, "⛔ Autoplay could not load any related music. Use /play to start a new queue!")
                music_states.pop(guild_id, None)
                await voice_client.disconnect()
                return

    # Reset autoplay failure count on success
    if hasattr(state, 'autoplay_failures'):
        state.autoplay_failures = 0

    print(f"🎵 Attempting to play: {next_song.get('title', 'Unknown')}\n")
    print(f"📹 URL: {next_song.get('url', 'No URL')}")

    try:
        print(f"🔍 Re-extracting video info with auto-bypass...")
        info = await extract_with_auto_bypass(next_song['url'], guild=guild, base_timeout=15)
        if not info:
            raise Exception("Failed to extract video info (auto-bypass)")

        audio_url = info.get('url') or info.get('webpage_url') or next_song.get('url')
        title = info.get('title', next_song.get('title', 'Unknown'))
        if not audio_url or not str(audio_url).startswith('http'):
            raise Exception("Invalid audio URL extracted")

        ffmpeg_opts = FFMPEG_OPTIONS.copy()
        if state.volume != 1.0:
            ffmpeg_opts['options'] += f' -filter:a "volume={state.volume}"'

        print(f"🔊 Creating FFmpeg audio source...")
        source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_opts)

        def after_playing(error):
            if error:
                print(f"❌ Player error: {error}")
                state.last_error = str(error)
            if guild_id in music_states and voice_client.is_connected():
                print(f"📢 Scheduling next song for guild {guild_id}")
                asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)

        print(f"▶️ Starting playback...")
        voice_client.play(source, after=after_playing)
        state.playing = True
        quality = f"({info.get('abr', 'HQ')}kbps)" if info.get('abr') else "(HQ)"
        await send_to_guild(guild, f"▶️ Now playing: **{title}** {quality}")
        print(f"✅ Successfully started playback\n")

    except Exception as e:
        print(f"❌ Error in play_next: {e}")
        print(traceback.format_exc())
        state.last_error = str(e)
        await send_to_guild(guild, f"❌ Error playing song: {e}")
        # Try the next song in queue (if any left)
        if len(state.entries) > state.current_index + 1:
            print(f"🔄 Trying next song...")
            await play_next(guild_id)
        else:
            print(f"⏹️ No more songs to try, attempting always-on autoplay...")
            did_autoplay = await autoplay_from_last_song(state, guild)
            if did_autoplay:
                await play_next(guild_id)
                return
            else:
                # Fail-safe: Try a few times, then disconnect
                if not hasattr(state, 'autoplay_failures'):
                    state.autoplay_failures = 0
                state.autoplay_failures += 1
                if state.autoplay_failures <= 3:
                    await send_to_guild(guild, f"⚡ Retrying autoplay (attempt {state.autoplay_failures}) after playback error…")
                    await asyncio.sleep(2)
                    await play_next(guild_id)
                    return
                else:
                    await send_to_guild(guild, "⛔ Autoplay could not find new music after error. Use /play to start a new session!")
                    music_states.pop(guild_id, None)
                    await voice_client.disconnect()

# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'\n✅ Bot Ready: {bot.user.name} ({bot.user.id})')
    print(f'✅ Connected to {len(bot.guilds)} guilds')
    await bot.sync_commands()
    print("🔄 Commands synced globally")
    await bot.change_presence(activity=discord.Game(name="🎵 HQ Music | /play"))

# on_voice_state_update handler has been REMOVED so the bot will NOT leave voice automatically!

# --- Bot Commands ---
# ... rest of your bot.py unchanged ...