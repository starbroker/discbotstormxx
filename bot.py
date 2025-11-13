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

# === Bot & Music Settings ===

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = discord.Bot(intents=intents)

COOKIE_PATH = 'youtube_cookies.txt'
use_cookies = os.path.exists(COOKIE_PATH)

YTDLP_PROXY = os.getenv('YTDLP_PROXY', None)
YTDLP_SOURCE_ADDRESS = os.getenv('YTDLP_SOURCE_ADDRESS', None)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.5993.90 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:119.0) Gecko/20100101 Firefox/119.0",
    "Mozilla/5.0 (Linux; Android 13; SM-G991U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.5993.90 Mobile Safari/537.36",
]

YDL_OPTIONS = {
    'format': 'bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio',
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': YTDLP_SOURCE_ADDRESS if YTDLP_SOURCE_ADDRESS else '0.0.0.0',
    'cookiefile': COOKIE_PATH if use_cookies else None,
    'extract_flat': 'in_playlist',
    'http_headers': {
        'User-Agent': USER_AGENTS[-1],
    },
    'extractor_args': {
        'youtube': {
            'player_client': ['android'],
            'max_comments': ['0'],
        }
    },
    'socket_timeout': 10,
    'retries': 2,
    'http_chunk_size': 1048576,  # 1MB
    'nocheckcertificate': True,
    'cachedir': False,
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -acodec libopus -ar 48000 -ac 2 -b:a 96k',
}

# === State ===

class GuildMusicState:
    def __init__(self):
        self.entries: list[Dict[str, Any]] = []
        self.current_index: int = 0
        self.playing: bool = False
        self.loop: bool = False
        self.volume: float = 1.0
        self.last_error: str = ""
        self.autoplay_failures: int = 0

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

# === Helpers ===

async def ensure_voice_client(ctx: discord.ApplicationContext) -> Optional[discord.VoiceClient]:
    if not ctx.author.voice:
        await ctx.respond("You are not in a voice channel!", ephemeral=True)
        return None
    voice_channel = ctx.author.voice.channel
    voice_client = ctx.voice_client
    if not voice_client:
        await voice_channel.connect()
        voice_client = ctx.voice_client
    elif voice_client.channel != voice_channel:
        await voice_client.move_to(voice_channel)
    return voice_client

def get_music_state(guild_id: int) -> GuildMusicState:
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState()
    return music_states[guild_id]

async def send_to_guild(guild: discord.Guild, message: str):
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
    loop = asyncio.get_event_loop()
    start_time = time.time()
    try:
        info = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False)),
            timeout=timeout
        )
        print(f"✅ Extraction completed in {time.time() - start_time:.2f}s")
        return info
    except Exception as e:
        print(f"❌ Extraction error: {e}")
        print(traceback.format_exc())
        return None

async def extract_with_auto_bypass(url: str, guild: Optional[discord.Guild]=None, base_timeout=20):
    print(f"🔐 Starting auto-bypass extraction for: {url}")
    attempted = []
    def make_ydl(options_overrides: dict):
        merged = YDL_OPTIONS.copy()
        headers = merged.get('http_headers', {}).copy()
        headers.update(options_overrides.get('http_headers', {}))
        merged['http_headers'] = headers
        for k, v in options_overrides.items():
            if k == 'http_headers':
                continue
            merged[k] = v
        return yt_dlp.YoutubeDL(merged)
    try:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = await extract_with_timeout(ydl, url, timeout=base_timeout)
            if info:
                return info
    except Exception as e:
        print(f"❌ Base extraction threw: {e}")
    strategies = []
    for ua in USER_AGENTS:
        strat = {'http_headers': {'User-Agent': ua}}
        if use_cookies:
            strat['cookiefile'] = COOKIE_PATH
        if YTDLP_PROXY:
            strat['proxy'] = YTDLP_PROXY
        strategies.append(('UA '+ua.split(' ')[0], strat))
    if YTDLP_PROXY:
        strategies.append(('Proxy', {'proxy': YTDLP_PROXY}))
    desktop_ua = USER_AGENTS[0]
    fallback = {'http_headers': {'User-Agent': desktop_ua}, 'socket_timeout': 20, 'retries': 4}
    if use_cookies:
        fallback['cookiefile'] = COOKIE_PATH
    if YTDLP_PROXY:
        fallback['proxy'] = YTDLP_PROXY
    strategies.append(('Fallback', fallback))

    for name, opts in strategies:
        attempted.append(name)
        try:
            ydl = make_ydl(opts)
            info = await extract_with_timeout(ydl, url, timeout=base_timeout+5)
            if info:
                return info
        except Exception as e:
            print(f"❌ Bypass strategy '{name}' error: {e}")

    msg = f"Failed extraction after attempts: {attempted}"
    print(f"❌ Auto-bypass failed: {msg}")
    if guild:
        guidance = (
            "If you're getting Cloudflare/403/429 errors, try the following:\n"
            "- Ensure youtube_cookies.txt is present and fresh (exported from your browser within ~30 minutes)\n"
            "- Set YTDLP_PROXY env var to a working proxy (if you have a whitelisted IP)\n"
            "- Set YTDLP_SOURCE_ADDRESS to the IP you solved CAPTCHA from (if applicable)\n"
            "- Provide a current browser User-Agent in YDL_OPTIONS or via environment\n"
        )
        await send_to_guild(guild, f"❌ Extraction failed for {url}. {guidance}")
    return None

# === Autoplay (always-on!) ===

async def autoplay_from_last_song(state: GuildMusicState, guild: discord.Guild) -> bool:
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

# === Playback Logic ===

async def play_next(guild_id: int):
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
    state.autoplay_failures = 0
    print(f"🎵 Attempting to play: {next_song.get('title', 'Unknown')}")
    print(f"📹 URL: {next_song.get('url', 'No URL')}")
    try:
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
        source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_opts)
        def after_playing(error):
            if error:
                print(f"❌ Player error: {error}")
                state.last_error = str(error)
            if guild_id in music_states and voice_client.is_connected():
                asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)
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
        if len(state.entries) > state.current_index + 1:
            await play_next(guild_id)
        else:
            did_autoplay = await autoplay_from_last_song(state, guild)
            if did_autoplay:
                await play_next(guild_id)
            else:
                state.autoplay_failures += 1
                if state.autoplay_failures <= 3:
                    await send_to_guild(guild, f"⚡ Retrying autoplay (attempt {state.autoplay_failures}) after playback error…")
                    await asyncio.sleep(2)
                    await play_next(guild_id)
                else:
                    await send_to_guild(guild, "⛔ Autoplay could not find new music after error. Use /play to start a new session!")
                    music_states.pop(guild_id, None)
                    await voice_client.disconnect()

# === No auto voice leave! ===

# --- No on_voice_state_update handler! Bot stays 24/7 in VC until /leave or kicked ---

# === Bot Commands ===

@bot.slash_command(name="play", description="Play YouTube music with premium audio quality")
@option("query", description="Song name, URL, or playlist", required=True, type=str)
async def play(ctx: discord.ApplicationContext, query: str):
    await ctx.defer()
    voice_client = await ensure_voice_client(ctx)
    if not voice_client:
        return
    try:
        await ctx.followup.send(f"🔍 Searching YouTube for '{query}'...")
        info = await extract_with_auto_bypass(query, guild=ctx.guild, base_timeout=20)
        if not info:
            raise Exception("YouTube extraction timed out or failed. Check logs.")
        entries = info.get('entries', [info])
        if not entries or not entries[0]:
            raise Exception("No results found or video is unavailable")
        state = get_music_state(ctx.guild.id)
        state.entries = entries
        state.current_index = 0
        state.playing = False
        if voice_client.is_playing():
            voice_client.stop()
        first_title = entries[0].get('title', 'Unknown')
        await ctx.followup.send(f"🎵 Queue ready! Starting with **{first_title}**...")
        await play_next(ctx.guild.id)
    except Exception as e:
        await ctx.followup.send(f"❌ Error: {e}", ephemeral=True)

@bot.slash_command(name="pause", description="Pause the current song")
async def pause(ctx: discord.ApplicationContext):
    voice_client = ctx.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        await ctx.respond("⏸️ Paused")
    else:
        await ctx.respond("❌ Nothing playing.", ephemeral=True)

@bot.slash_command(name="resume", description="Resume a paused song")
async def resume(ctx: discord.ApplicationContext):
    voice_client = ctx.voice_client
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        await ctx.respond("▶️ Resumed")
    else:
        await ctx.respond("❌ Not paused.", ephemeral=True)

@bot.slash_command(name="skip", description="Skip to next song")
async def skip(ctx: discord.ApplicationContext):
    voice_client = ctx.voice_client
    if not voice_client or not voice_client.is_playing():
        await ctx.respond("❌ Nothing to skip.", ephemeral=True)
        return
    state = get_music_state(ctx.guild.id)
    if not state.entries or state.current_index >= len(state.entries) - 1:
        await ctx.respond("⏹️ No more songs in queue.", ephemeral=True)
        voice_client.stop()
        return
    await ctx.respond("⏭️ Skipping...")
    voice_client.stop()

@bot.slash_command(name="leave", description="Disconnect bot and clear queue")
async def leave(ctx: discord.ApplicationContext):
    voice_client = ctx.voice_client
    if voice_client:
        await voice_client.disconnect()
        music_states.pop(ctx.guild.id, None)
        await ctx.respond("👋 Disconnected and cleared queue.")
    else:
        await ctx.respond("❌ Not in a voice channel.", ephemeral=True)

@bot.slash_command(name="queue", description="Show current queue")
async def queue(ctx: discord.ApplicationContext):
    state = get_music_state(ctx.guild.id)
    if not state.entries:
        await ctx.respond("❌ Queue is empty.", ephemeral=True)
        return
    songs = state.entries[state.current_index:state.current_index + 10]
    queue_text = "\n".join([
        f"{i+1}. **{song.get('title', 'Unknown')}**"
        for i, song in enumerate(songs)
    ])
    embed = discord.Embed(title="🎵 Queue", description=queue_text, color=discord.Color.blue())
    embed.set_footer(text=f"Loop: {'On' if state.loop else 'Off'} | Volume: {state.volume:.1f}x")
    await ctx.respond(embed=embed, ephemeral=True)

@bot.slash_command(name="volume", description="Set playback volume (0.1-2.0)")
@option("level", description="Volume level (0.1 to 2.0)", required=True, type=float)
async def volume(ctx: discord.ApplicationContext, level: float):
    if not 0.1 <= level <= 2.0:
        await ctx.respond("❌ Volume must be between 0.1 and 2.0", ephemeral=True)
        return
    state = get_music_state(ctx.guild.id)
    state.volume = level
    await ctx.respond(f"🔊 Volume set to {level:.1f}x")

@bot.slash_command(name="loop", description="Toggle queue looping")
async def loop(ctx: discord.ApplicationContext):
    state = get_music_state(ctx.guild.id)
    state.loop = not state.loop
    status = "🔄 enabled" if state.loop else "❌ disabled"
    await ctx.respond(f"🔁 Loop {status}")

@bot.slash_command(name="nowplaying", description="Show current song info")
async def nowplaying(ctx: discord.ApplicationContext):
    state = get_music_state(ctx.guild.id)
    song = state.current_song
    if not song:
        await ctx.respond("❌ Nothing playing.", ephemeral=True)
        return
    embed = discord.Embed(title="▶️ Now Playing", color=discord.Color.green())
    embed.add_field(name="Title", value=song.get('title', 'Unknown'), inline=False)
    embed.add_field(name="Duration", value=f"{song.get('duration', 0)}s", inline=True)
    if 'abr' in song:
        embed.add_field(name="Quality", value=f"{song.get('abr')}kbps", inline=True)
    if state.last_error:
        embed.add_field(name="Last Error", value=state.last_error, inline=False)
    await ctx.respond(embed=embed, ephemeral=True)

@bot.slash_command(name="debug", description="Show diagnostic information")
async def debug(ctx: discord.ApplicationContext):
    voice_client = ctx.voice_client
    state = get_music_state(ctx.guild.id)
    embed = discord.Embed(title="🔧 Debug Info", color=discord.Color.greyple())
    if voice_client:
        embed.add_field(
            name="Voice", 
            value=f"Connected: {voice_client.is_connected()}\nPlaying: {voice_client.is_playing()}\nPaused: {voice_client.is_paused()}", 
            inline=False
        )
    else:
        embed.add_field(name="Voice", value="Not connected", inline=False)
    embed.add_field(
        name="Queue", 
        value=f"Items: {len(state.entries)}\nIndex: {state.current_index}\nPlaying: {state.playing}", 
        inline=False
    )
    embed.add_field(name="YouTube Cookies", value="✅ Loaded" if use_cookies else "❌ Not found", inline=False)
    embed.add_field(name="Proxy (YTDLP_PROXY)", value=YTDLP_PROXY or "Not set", inline=False)
    embed.add_field(name="Source Address (YTDLP_SOURCE_ADDRESS)", value=YTDLP_SOURCE_ADDRESS or "Not set", inline=False)
    await ctx.respond(embed=embed, ephemeral=True)

# === Bot Startup ===

@bot.event
async def on_ready():
    print(f'\n✅ Bot Ready: {bot.user.name} ({bot.user.id})')
    print(f'✅ Connected to {len(bot.guilds)} guilds')
    await bot.sync_commands()
    print("🔄 Commands synced globally")
    await bot.change_presence(activity=discord.Game(name="🎵 HQ Music | /play"))

TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    print("❌ Error: DISCORD_TOKEN environment variable not set.")
    print("   Set it with: export DISCORD_TOKEN='your_token_here'")
else:
    try:
        print("\n🤖 Starting bot...")
        bot.run(TOKEN)
    except discord.errors.LoginFailure:
        print("❌ Error: Invalid Discord token.")
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        print(traceback.format_exc())
