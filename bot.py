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

YDL_OPTIONS = {
    # Faster format selection
    'format': 'bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio',
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'cookiefile': COOKIE_PATH if use_cookies else None,
    # Speed up extraction
    'extract_flat': 'in_playlist',
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    },
    'extractor_args': {
        'youtube': {
            'player_client': ['android'],  # Faster than web client
            'max_comments': ['0'],
        }
    },
    # Timeout settings
    'socket_timeout': 10,
    'retries': 2,
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

async def play_next(guild_id: int):
    """Play the next song in queue with detailed logging."""
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
        print(f"⏹️ No more songs in queue for guild {guild_id}")
        await send_to_guild(guild, "⏹️ Queue ended. Use /play to add more songs!")
        music_states.pop(guild_id, None)
        return

    print(f"🎵 Attempting to play: {next_song.get('title', 'Unknown')}")
    print(f"📹 URL: {next_song.get('url', 'No URL')}")

    try:
        # Extract fresh URL
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            print(f"🔍 Re-extracting video info...")
            info = await extract_with_timeout(ydl, next_song['url'], timeout=15)
            
            if not info:
                raise Exception("Failed to extract video info (timeout)")

            audio_url = info['url']
            title = info.get('title', 'Unknown')
            
            # Verify URL is valid
            if not audio_url or not audio_url.startswith('http'):
                raise Exception("Invalid audio URL extracted")

            print(f"✅ Got audio URL, duration: {info.get('duration', 0)}s")
            print(f"✅ Format: {info.get('format_id', 'unknown')} ({info.get('abr', 'unknown')}kbps)")

        # Prepare FFmpeg options
        ffmpeg_opts = FFMPEG_OPTIONS.copy()
        if state.volume != 1.0:
            ffmpeg_opts['options'] += f' -filter:a "volume={state.volume}"'

        print(f"🔊 Creating FFmpeg audio source...")
        source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_opts)
        
        def after_playing(error):
            if error:
                print(f"❌ Player error: {error}")
                state.last_error = str(error)
            # Schedule next song if still connected
            if guild_id in music_states and voice_client.is_connected():
                print(f"📢 Scheduling next song for guild {guild_id}")
                asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)
            else:
                print(f"❌ Not scheduling next song - disconnected")

        print(f"▶️ Starting playback...")
        voice_client.play(source, after=after_playing)
        state.playing = True
        
        # Show quality info
        quality = f"({info.get('abr', 'HQ')}kbps)" if info.get('abr') else "(HQ)"
        await send_to_guild(guild, f"▶️ Now playing: **{title}** {quality}")
        print(f"✅ Successfully started playback\n")
    
    except Exception as e:
        print(f"❌ Error in play_next: {e}")
        print(traceback.format_exc())
        state.last_error = str(e)
        await send_to_guild(guild, f"❌ Error playing song: {e}")
        
        # Try next song if available
        if len(state.entries) > state.current_index + 1:
            print(f"🔄 Trying next song...")
            await play_next(guild_id)
        else:
            print(f"⏹️ No more songs to try, stopping...")
            music_states.pop(guild_id, None)

# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'\n✅ Bot Ready: {bot.user.name} ({bot.user.id})')
    print(f'✅ Connected to {len(bot.guilds)} guilds')
    await bot.sync_commands()
    print("🔄 Commands synced globally")
    await bot.change_presence(activity=discord.Game(name="🎵 HQ Music | /play"))

@bot.event
async def on_voice_state_update(member, before, after):
    """Auto-disconnect when alone."""
    if member.bot:
        return
    
    voice_client = member.guild.voice_client
    if voice_client and len(voice_client.channel.members) == 1:
        print(f"👤 Bot is alone in {voice_client.channel.name}, waiting 30s...")
        await asyncio.sleep(30)
        if voice_client.is_connected() and len(voice_client.channel.members) == 1:
            await voice_client.disconnect()
            music_states.pop(member.guild.id, None)
            await send_to_guild(member.guild, "👋 Left due to inactivity.")

# --- Bot Commands ---
@bot.slash_command(name="play", description="Play YouTube music with premium audio quality")
@option("query", description="Song name, URL, or playlist", required=True, type=str)
async def play(ctx: discord.ApplicationContext, query: str):
    print(f"\n🎵 Play command received: '{query}' from {ctx.author}")
    await ctx.defer()

    voice_client = await ensure_voice_client(ctx)
    if not voice_client:
        print("❌ Failed to get voice client")
        return

    try:
        # Show extraction progress
        await ctx.followup.send(f"🔍 Searching YouTube for '{query}'...")
        
        start_time = time.time()
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            print(f"🔍 Extracting info from YouTube...")
            info = await extract_with_timeout(ydl, query, timeout=20)
            
            if not info:
                raise Exception("YouTube extraction timed out or failed. Check logs.")
            
            print(f"✅ Extraction successful, took {time.time() - start_time:.2f}s")

            entries = info.get('entries', [info])
            if not entries or not entries[0]:
                raise Exception("No results found or video is unavailable")

            print(f"✅ Found {len(entries)} item(s)")

        # Initialize queue
        state = get_music_state(ctx.guild.id)
        state.entries = entries
        state.current_index = 0
        state.playing = False  # Will be set to True in play_next

        # Stop current playback
        if voice_client.is_playing():
            print("⏹️ Stopping current playback...")
            voice_client.stop()

        # Send final response
        first_title = entries[0].get('title', 'Unknown')
        await ctx.followup.send(f"🎵 Queue ready! Starting with **{first_title}**...")
        
        # Start playback
        await play_next(ctx.guild.id)
        print("✅ Play command completed successfully\n")

    except Exception as e:
        print(f"❌ Error in play command: {e}")
        print(traceback.format_exc())
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
    """Show bot diagnostic info."""
    voice_client = ctx.voice_client
    state = get_music_state(ctx.guild.id)
    
    embed = discord.Embed(title="🔧 Debug Info", color=discord.Color.greyple())
    
    # Voice connection status
    if voice_client:
        embed.add_field(
            name="Voice", 
            value=f"Connected: {voice_client.is_connected()}\nPlaying: {voice_client.is_playing()}\nPaused: {voice_client.is_paused()}", 
            inline=False
        )
    else:
        embed.add_field(name="Voice", value="Not connected", inline=False)
    
    # Queue status
    embed.add_field(
        name="Queue", 
        value=f"Items: {len(state.entries)}\nIndex: {state.current_index}\nPlaying: {state.playing}", 
        inline=False
    )
    
    # Cookies
    embed.add_field(name="YouTube Cookies", value="✅ Loaded" if use_cookies else "❌ Not found", inline=False)
    
    await ctx.respond(embed=embed, ephemeral=True)

# --- Run the Bot ---
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
