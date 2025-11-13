import discord
from discord import option # Import the 'option' decorator
import yt_dlp
import os
import asyncio

# --- Bot Setup ---
# Set up intents
intents = discord.Intents.default()
intents.message_content = True # Still good to have for future features
intents.voice_states = True

# Initialize the bot. We use discord.Bot() for slash commands.
bot = discord.Bot(intents=intents)

# --- FFMPEG and YTDL Options ---
yt_dlp.utils.bug_reports_message = lambda: ''

YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': False,
    'quiet': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0'
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

# --- Music State Management ---
# This dictionary will hold the state for each server (guild)
music_queues = {} # guild_id -> { 'entries': [], 'index': 0, 'playing': True }

# --- Helper Function to Play (Autoplay) ---
async def play_next(ctx: discord.ApplicationContext):
    """
    A helper function that is called after a song finishes.
    It checks if there's a next song in the search results and plays it.
    """
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        return # No queue for this guild

    state = music_queues[guild_id]
    voice_client = ctx.voice_client

    # Check if the bot is still connected
    if not voice_client or not voice_client.is_connected():
        del music_queues[guild_id] # Clear state
        return

    # Check if we were told to stop (e.g., by /leave)
    if not state.get('playing', True):
        return

    # Move to the next song index
    state['index'] += 1
    
    # Check if we are at the end of the search results
    if state['index'] >= len(state['entries']):
        # Use followup.send for messages after the initial response
        await ctx.followup.send("Reached the end of search results. Stopping autoplay.")
        del music_queues[guild_id] # Clear state
        return

    # We have a next song, let's play it
    try:
        entry = state['entries'][state['index']]
        audio_url = entry['url']
        song_title = entry['title']
        
        source = discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTIONS)
        
        # The core of the autoplay loop:
        # Play the song, and set 'play_next' to be called again when it's done
        voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))
        
        await ctx.followup.send(f"▶️ Autoplaying: **{song_title}**")
    
    except Exception as e:
        await ctx.followup.send(f"An error occurred during autoplay: {e}")
        print(f"Error in play_next: {e}")
        if guild_id in music_queues:
            del music_queues[guild_id] # Clear state on error


# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')
    # Sync the commands to make them appear in Discord
    await bot.sync_commands() 
    print("Slash commands synced!")
    await bot.change_presence(activity=discord.Game(name="Music! /play"))

# --- Bot Commands ---
@bot.slash_command(name="play", description="Plays songs from YouTube and autoplays search results.")
@option("query", description="Your song search term or a URL", required=True, type=str)
async def play(ctx: discord.ApplicationContext, query: str):
    """
    Command: /play query:"<search query or URL>"
    Joins, plays the first song, and sets up autoplay for subsequent results.
    """
    # Check if the user is in a voice channel
    if not ctx.author.voice:
        # 'ephemeral=True' makes the message only visible to the user
        await ctx.respond("You are not in a voice channel!", ephemeral=True)
        return
    
    voice_channel = ctx.author.voice.channel
    voice_client = ctx.voice_client

    # Connect or move to the user's channel
    if not voice_client:
        await voice_channel.connect()
    elif voice_client.channel != voice_channel:
        await voice_client.move_to(voice_channel)
    
    voice_client = ctx.voice_client # Re-assign after connect/move

    # **CRITICAL** Defer the response.
    # This shows "Bot is thinking..." and gives us more than 3 seconds
    # to search YouTube and start the song.
    await ctx.defer()

    # Stop any song that is currently playing
    if voice_client.is_playing():
        voice_client.stop()

    try:
        # Search for the query
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(query, download=False)
            
            if 'entries' not in info or not info['entries']:
                # Check if it was a single video URL
                if 'url' in info:
                    info['entries'] = [info] # Wrap the single video in a list
                else:
                    await ctx.followup.send(f"Could not find any songs for '{query}'", ephemeral=True)
                    return

        # We have results. Store them for autoplay.
        music_queues[ctx.guild.id] = {
            'entries': info['entries'],
            'index': 0,
            'playing': True
        }
        
        # Play the FIRST song (index 0)
        first_entry = info['entries'][0]
        audio_url = first_entry['url']
        song_title = first_entry['title']

        source = discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTIONS)
        
        # Start the play loop
        voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))
        
        # Use followup.send for the first message after deferring
        await ctx.followup.send(f"▶️ Now playing: **{song_title}**")

    except Exception as e:
        await ctx.followup.send(f"An error occurred: {e}", ephemeral=True)
        print(f"Error in play command: {e}")

@bot.slash_command(name="pause", description="Pauses the current song")
async def pause(ctx: discord.ApplicationContext):
    voice_client = ctx.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        await ctx.respond("⏸️ Paused")
    else:
        await ctx.respond("I'm not playing anything right now.", ephemeral=True)

@bot.slash_command(name="resume", description="Resumes a paused song")
async def resume(ctx: discord.ApplicationContext):
    voice_client = ctx.voice_client
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        await ctx.respond("▶️ Resumed")
    else:
        await ctx.respond("I'm not paused.", ephemeral=True)

@bot.slash_command(name="skip", description="Skips the current song and plays the next one")
async def skip(ctx: discord.ApplicationContext):
    guild_id = ctx.guild.id
    voice_client = ctx.voice_client

    if guild_id not in music_queues or not voice_client or not voice_client.is_playing():
        await ctx.respond("I'm not playing anything to skip.", ephemeral=True)
        return

    # Send the "Skipping" message first
    await ctx.respond("⏭️ Skipping...")
    
    # Stop the current song.
    # The 'after' function (play_next) will *automatically* be called,
    # which then plays the next song and sends the "Autoplaying..." message.
    voice_client.stop()

@bot.slash_command(name="leave", description="Disconnects the bot and clears autoplay")
async def leave(ctx: discord.ApplicationContext):
    guild_id = ctx.guild.id
    voice_client = ctx.voice_client

    # Clear the autoplay state for this guild
    if guild_id in music_queues:
        music_queues[guild_id]['playing'] = False # Stop the 'after' loop
        del music_queues[guild_id]

    if voice_client and voice_client.is_connected():
        await voice_client.disconnect()
        await ctx.respond("👋 Bye bye!")
    else:
        await ctx.respond("I'm not in a voice channel.", ephemeral=True)

# --- Run the Bot ---
TOKEN = os.getenv('DISCORD_TOKEN')
if TOKEN is None:
    print("Error: DISCORD_TOKEN environment variable not set.")
else:
    try:
        bot.run(TOKEN)
    except discord.errors.LoginFailure:
        print("Error: Invalid Discord token.")
