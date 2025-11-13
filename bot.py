import discord
from discord import option
from discord.ext import commands
import yt_dlp
import os
import asyncio
from typing import Dict, Any, Optional

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = discord.Bot(intents=intents)

# --- HIGH-QUALITY Audio Configuration ---

# IMPORTANT: Create youtube_cookies.txt file to avoid bot detection
# 1. Install browser extension: "Get cookies.txt LOCALLY"
# 2. Go to YouTube.com and log in
# 3. Click extension icon → Export → Save as "youtube_cookies.txt" in same folder
# 4. NEVER commit this file to git (add to .gitignore)!

COOKIE_PATH = 'youtube_cookies.txt'
use_cookies = os.path.exists(COOKIE_PATH)

YDL_OPTIONS = {
    # Get best audio format (Opus 160kbps > M4A 128kbps > other)
    'format': 'bestaudio/best',
    'extract_flat': False,  # Full extraction for quality data
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    # Prioritize formats: Opus > M4A > AAC
    'format_sort': ['ext:opus', 'ext:m4a', 'abr:160', 'abr'],
    # Browser headers to avoid detection
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    },
    # Skip unnecessary data
    'extractor_args': {
        'youtube': {
            'max_comments': ['0'],
            'player_client': ['android', 'web'],  # Use multiple clients
        }
    },
}

# Add cookies if available (most reliable method)
if use_cookies:
    YDL_OPTIONS['cookiefile'] = COOKIE_PATH
    print("✅ Using YouTube cookies for authentication")
else:
    print("⚠️ No cookies found. You may encounter playback errors.")

# FFmpeg optimized for Discord's Opus codec
FFMPEG_OPTIONS = {
    'before_options': (
        '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 '
        '-reconnect_on_http_error 4xx,5xx'
    ),
    'options': (
        '-vn -acodec libopus -ar 48000 -ac 2 '  # Discord native format
        '-b:a 96k -bufsize 96k '  # Match Discord's bitrate
        '-application audio '  # Optimize for music playback
    ),
}

# --- Music Queue Management ---
class GuildMusicState:
    def __init__(self):
        self.entries: list[Dict[str, Any]] = []
        self.current_index: int = 0
        self.playing: bool = False
        self.loop: bool = False
        self.volume: float = 1.0

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
        await voice_channel.connect()
        voice_client = ctx.voice_client
    elif voice_client.channel != voice_channel:
        await voice_client.move_to(voice_channel)
    
    # Set maximum bitrate (96kbps standard, 128kbps with Nitro boost)
    if hasattr(voice_client, 'channel') and voice_client.channel:
        try:
            await voice_client.channel.edit(bitrate=96000)
        except discord.Forbidden:
            print("⚠️ Could not set bitrate (missing permissions)")
    
    return voice_client

def get_music_state(guild_id: int) -> GuildMusicState:
    """Get or create music state for guild."""
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState()
    return music_states[guild_id]

async def send_to_guild(guild: discord.Guild, message: str):
    """Send message to the first available text channel."""
    channel = discord.utils.find(
        lambda c: isinstance(c, discord.TextChannel) and c.permissions_for(guild.me).send_messages,
        guild.text_channels
    )
    if channel:
        await channel.send(message)

async def play_next(guild_id: int):
    """Play the next song in queue."""
    state = get_music_state(guild_id)
    guild = bot.get_guild(guild_id)
    voice_client = discord.utils.get(bot.voice_clients, guild__id=guild_id)

    if not voice_client or not voice_client.is_connected():
        music_states.pop(guild_id, None)
        return

    if not state.playing:
        return

    next_song = state.next_song()
    if not next_song:
        await send_to_guild(guild, "⏹️ Queue ended. Use /play to add more songs!")
        music_states.pop(guild_id, None)
        return

    try:
        # Re-extract to get fresh URL and format info
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(next_song['url'], download=False)
            audio_url = info['url']
            title = info.get('title', 'Unknown')
            format_note = info.get('format_note', '')
            abr = info.get('abr', '')

        # Apply volume filter if needed
        ffmpeg_opts = FFMPEG_OPTIONS.copy()
        if state.volume != 1.0:
            ffmpeg_opts['options'] += f' -filter:a "volume={state.volume}"'

        source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_opts)
        
        def after_playing(error):
            if error:
                print(f"❌ Player error: {error}")
            # Only play next if still connected
            if guild_id in music_states and voice_client.is_connected():
                asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)

        voice_client.play(source, after=after_playing)
        state.playing = True
        
        # Show quality info
        quality = f"({format_note} - {abr}kbps)" if format_note and abr else "(HQ Audio)"
        await send_to_guild(guild, f"▶️ Now playing: **{title}** {quality}")
    
    except Exception as e:
        await send_to_guild(guild, f"❌ Error playing next song: {e}")
        print(f"Error in play_next: {e}")
        # Try to play next song instead of stopping completely
        if len(state.entries) > state.current_index + 1:
            await play_next(guild_id)
        else:
            music_states.pop(guild_id, None)

# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'✅ Logged in as {bot.user.name} ({bot.user.id})')
    await bot.sync_commands()
    print("🔄 Commands synced globally")
    await bot.change_presence(activity=discord.Game(name="🎵 HQ Music | /play"))

@bot.event
async def on_voice_state_update(member, before, after):
    """Auto-disconnect when alone in voice channel."""
    if member.bot:
        return
    
    voice_client = member.guild.voice_client
    if voice_client and len(voice_client.channel.members) == 1:
        await asyncio.sleep(30)
        if voice_client.is_connected() and len(voice_client.channel.members) == 1:
            await voice_client.disconnect()
            music_states.pop(member.guild.id, None)
            await send_to_guild(member.guild, "👋 Left voice channel due to inactivity.")

# --- Bot Commands ---
@bot.slash_command(name="play", description="Play YouTube music with premium audio quality")
@option("query", description="Song name, URL, or playlist", required=True, type=str)
async def play(ctx: discord.ApplicationContext, query: str):
    await ctx.defer()

    voice_client = await ensure_voice_client(ctx)
    if not voice_client:
        return

    try:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(query, download=False)
            
            entries = info.get('entries', [info])
            if not entries or not entries[0]:
                await ctx.followup.send("❌ No results found.", ephemeral=True)
                return

        state = get_music_state(ctx.guild.id)
        state.entries = entries
        state.current_index = 0
        state.playing = False

        # Stop current playback if any
        if voice_client.is_playing():
            voice_client.stop()

        await play_next(ctx.guild.id)
        await ctx.followup.send("🎵 Starting high-quality playback...")

    except Exception as e:
        await ctx.followup.send(f"❌ Error: {e}", ephemeral=True)
        print(f"Play command error: {e}")

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
    voice_client.stop()  # after callback will play next

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
    
    await ctx.respond(embed=embed, ephemeral=True)

# --- Run the Bot ---
TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    print("❌ Error: DISCORD_TOKEN environment variable not set.")
    print("   Set it with: export DISCORD_TOKEN='your_token_here'")
else:
    try:
        bot.run(TOKEN)
    except discord.errors.LoginFailure:
        print("❌ Error: Invalid Discord token.")
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
