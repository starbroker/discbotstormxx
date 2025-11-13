import discord
from discord import option
from discord.ext import commands
import yt_dlp
import os
import asyncio

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

# We removed debug_guilds, so the bot uses global commands.
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
music_queues = {} # guild_id -> { 'entries': [], 'index': 0, 'playing': True }

# --- Helper Function to Play (Autoplay) ---
async def play_next(ctx: discord.ApplicationContext):
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        return

    state = music_queues[guild_id]
    voice_client = ctx.voice_client

    if not voice_client or not voice_client.is_connected():
        if guild_id in music_queues:
            del music_queues[guild_id]
        return

    if not state.get('playing', True):
        return

    state['index'] += 1
    
    if state['index'] >= len(state['entries']):
        await ctx.followup.send("Reached the end of search results. Stopping autoplay.")
        if guild_id in music_queues:
            del music_queues[guild_id]
        return

    try:
        entry = state['entries'][state['index']]
        audio_url = entry['url']
        song_title = entry['title']
        
        source = discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTIONS)
        
        voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))
        
        await ctx.followup.send(f"▶️ Autoplaying: **{song_title}**")
    
    except Exception as e:
        await ctx.followup.send(f"An error occurred during autoplay: {e}")
        print(f"Error in play_next: {e}")
        if guild_id in music_queues:
            del music_queues[guild_id]

# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')
    
    # **This is needed for global commands**
    # It can take up to an hour for commands to appear in all servers.
    await bot.sync_commands() 
    
    print("Bot is ready and commands are synced globally.")
    await bot.change_presence(activity=discord.Game(name="Music! /play"))

# --- Bot Commands ---
@bot.slash_command(name="play", description="Plays songs from YouTube and autoplays search results.")
@option("query", description="Your song search term or a URL", required=True, type=str)
async def play(ctx: discord.ApplicationContext, query: str):
    
    if not ctx.author.voice:
        await ctx.respond("You are not in a voice channel!", ephemeral=True)
        return
    
    # Defer the response *immediately*
    await ctx.defer()

    voice_channel = ctx.author.voice.channel
    voice_client = ctx.voice_client
    
    if not voice_client:
        await voice_channel.connect()
    elif voice_client.channel != voice_channel:
        await voice_client.move_to(voice_channel)
    
    voice_client = ctx.voice_client

    if voice_client.is_playing():
        voice_client.stop()

    try:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(query, download=False)
            
            if 'entries' not in info or not info['entries']:
                if 'url' in info:
                    info['entries'] = [info]
                else:
                    await ctx.followup.send(f"Could not find any songs for '{query}'", ephemeral=True)
                    return

        music_queues[ctx.guild.id] = {
            'entries': info['entries'],
            'index': 0,
            'playing': True
        }
        
        first_entry = info['entries'][0]
        audio_url = first_entry['url']
        song_title = first_entry['title']

        source = discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTIONS)
        
        voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))
        
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

    await ctx.respond("⏭️ Skipping...")
    voice_client.stop() # The 'after' function will handle playing the next song

@bot.slash_command(name="leave", description="Disconnects the bot and clears autoplay")
async def leave(ctx: discord.ApplicationContext):
    guild_id = ctx.guild.id
    voice_client = ctx.voice_client

    if guild_id in music_queues:
        music_queues[guild_id]['playing'] = False
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
