import discord
from discord.ext import commands
import yt_dlp
import os

# --- Bot Setup ---
# Set up intents. 'message_content' is needed for reading messages.
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

# Initialize the bot with a command prefix
bot = commands.Bot(command_prefix='!', intents=intents)

# --- FFMPEG and YTDL Options ---
# Suppress noise output from yt_dlp
yt_dlp.utils.bug_reports_message = lambda: ''

# YTDL options for streaming
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'  # Binds to IPv4
}

# FFmpeg options
# -reconnect 1: Reconnect if the stream drops
# -reconnect_streamed 1: Reconnect if the stream is live
# -reconnect_delay_max 5: Max delay before reconnecting
# -vn: No video, just audio
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}


# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')
    await bot.change_presence(activity=discord.Game(name="Music!"))

# --- Helper Function to Play ---
async def play_song(ctx, query):
    """
    Searches YouTube with yt-dlp, gets the first result, and plays it.
    """
    voice_client = ctx.voice_client
    
    # Stop any song that is currently playing
    if voice_client.is_playing():
        voice_client.stop()

    try:
        # Search for the song
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            # Use 'ytsearch' to search YouTube
            info = ydl.extract_info(f"ytsearch:{query}", download=False)
            
            # Check if any videos were found
            if 'entries' not in info or not info['entries']:
                await ctx.send(f"Could not find any songs for '{query}'")
                return

            # Get the URL of the first search result
            # 'url' here is the direct streamable audio URL
            audio_url = info['entries'][0]['url']
            song_title = info['entries'][0]['title']

        # Create the audio source
        source = discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTIONS)

        # Play the audio
        voice_client.play(source)
        await ctx.send(f"▶️ Now playing: **{song_title}**")

    except Exception as e:
        await ctx.send(f"An error occurred: {e}")
        print(f"Error in play_song: {e}")


# --- Bot Commands ---
@bot.command(name='play', help='Plays a song from YouTube')
async def play(ctx, *, query: str):
    """
    Command: !play [search query or URL]
    Joins the user's voice channel and plays the requested song.
    """
    # Check if the user is in a voice channel
    if not ctx.author.voice:
        await ctx.send("You are not in a voice channel!")
        return
    
    voice_channel = ctx.author.voice.channel
    
    # Get or connect to the voice channel
    voice_client = ctx.voice_client
    if not voice_client:
        await voice_channel.connect()
    elif voice_client.channel != voice_channel:
        await voice_client.move_to(voice_channel)

    # Start the song
    await play_song(ctx, query)

@bot.command(name='pause', help='Pauses the current song')
async def pause(ctx):
    voice_client = ctx.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        await ctx.send("⏸️ Paused")
    else:
        await ctx.send("I'm not playing anything right now.")

@bot.command(name='resume', help='Resumes a paused song')
async def resume(ctx):
    voice_client = ctx.voice_client
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        await ctx.send("▶️ Resumed")
    else:
        await ctx.send("I'm not paused.")

@bot.command(name='skip', help='Stops the current song (no queue)')
async def skip(ctx):
    voice_client = ctx.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.stop()
        await ctx.send("⏭️ Skipped")
    else:
        await ctx.send("I'm not playing anything to skip.")

@bot.command(name='leave', help='Disconnects the bot from the voice channel')
async def leave(ctx):
    voice_client = ctx.voice_client
    if voice_client and voice_client.is_connected():
        await voice_client.disconnect()
        await ctx.send("👋 Bye bye!")
    else:
        await ctx.send("I'm not in a voice channel.")

# --- Run the Bot ---
# Get the token from an environment variable
TOKEN = os.getenv('DISCORD_TOKEN')

if TOKEN is None:
    print("Error: DISCORD_TOKEN environment variable not set.")
else:
    try:
        bot.run(TOKEN)
    except discord.errors.LoginFailure:
        print("Error: Invalid Discord token.")
