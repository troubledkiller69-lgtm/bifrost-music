import asyncio
import json
import logging
import os
import re as _re_module
import random
import time
from collections import defaultdict
from typing import Dict, Optional, List, Set
import discord
from discord import app_commands
from discord.ext import commands

from services.spotify import SpotifyService
from services.youtube import YouTubeService, Song

logger = logging.getLogger("BifrostMusic.Cog")

class GuildQueue:
    """Manages the music queue and voice client state per Discord guild."""

    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: List[Song] = []
        self.current: Optional[Song] = None
        self.loop_mode: str = "off"  # "off", "track", "queue"
        self.autoplay: bool = False
        self.volume: float = 0.8
        self.filter_options: str = ""
        self.voice_client: Optional[discord.VoiceClient] = None
        self.text_channel: Optional[discord.TextChannel] = None
        self.now_playing_message: Optional[discord.Message] = None
        self.play_lock = asyncio.Lock()
        self.notify_users: Set[int] = set()  # user IDs who want DM notifications
        self.dedication: Optional[dict] = None  # {"from": user, "to": member}
        self.play_start_time: Optional[float] = None  # epoch time when current track started
        self.autoplay_history: List[str] = []  # titles of recently played songs for dedup
        # Round Robin state
        self.round_robin: bool = False
        self._rr_last_requester: Optional[str] = None  # last requester who got a song played
        # Quiz state
        self.quiz_active: bool = False
        self.quiz_answer: Optional[str] = None  # current round's answer
        self.quiz_scores: Dict[int, int] = {}  # user_id -> score

    def clear(self):
        """Reset queue state."""
        self.queue.clear()
        self.current = None
        self.now_playing_message = None
        self.dedication = None
        self.play_start_time = None
        self.autoplay_history.clear()
        self.quiz_active = False
        self.quiz_answer = None
        self.quiz_scores = {}

class MusicPlayerView(discord.ui.View):
    def __init__(self, cog: 'MusicCog', guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="play_pause")
    async def toggle_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        g_queue = self.cog.get_guild_queue(self.guild_id)
        if g_queue.voice_client:
            if g_queue.voice_client.is_playing():
                g_queue.voice_client.pause()
                await interaction.response.send_message("⏸️ Paused", ephemeral=True)
            elif g_queue.voice_client.is_paused():
                g_queue.voice_client.resume()
                await interaction.response.send_message("▶️ Resumed", ephemeral=True)
            else:
                await interaction.response.send_message("⚠️ Nothing playing", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Not connected", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="skip")
    async def skip_track(self, interaction: discord.Interaction, button: discord.ui.Button):
        g_queue = self.cog.get_guild_queue(self.guild_id)
        if g_queue.voice_client and (g_queue.voice_client.is_playing() or g_queue.voice_client.is_paused()):
            g_queue.loop_mode = "off"
            g_queue.voice_client.stop()
            await interaction.response.send_message("⏭️ Skipped track", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Queue empty", ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="stop")
    async def stop_track(self, interaction: discord.Interaction, button: discord.ui.Button):
        g_queue = self.cog.get_guild_queue(self.guild_id)
        g_queue.clear()
        if g_queue.voice_client:
            await g_queue.voice_client.disconnect(force=True)
            g_queue.voice_client = None
        await interaction.response.send_message("🛑 Playback stopped & disconnected", ephemeral=True)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, custom_id="queue")
    async def show_queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.show_queue.callback(self.cog, interaction)

STATS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "listening_stats.json")
PLAYLIST_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_playlists.json")

def _load_stats() -> dict:
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_stats(data: dict):
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to save stats: {e}")

def _load_playlists() -> dict:
    if os.path.exists(PLAYLIST_FILE):
        try:
            with open(PLAYLIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_playlists(data: dict):
    try:
        with open(PLAYLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to save playlists: {e}")

def _record_play(guild_id: int, song_title: str, requester: str):
    stats = _load_stats()
    gid = str(guild_id)
    if gid not in stats:
        stats[gid] = {"songs": {}, "users": {}}
    songs = stats[gid]["songs"]
    songs[song_title] = songs.get(song_title, 0) + 1
    users = stats[gid]["users"]
    # strip mention formatting
    clean = requester.replace("<@", "").replace(">", "").replace("!", "")
    users[clean] = users.get(clean, 0) + 1
    _save_stats(stats)

class PlaylistGroup(app_commands.Group):
    """Group for custom playlist commands."""
    
    def __init__(self, cog: 'MusicCog'):
        super().__init__(name="playlist", description="Manage custom server playlists")
        self.cog = cog

    @app_commands.command(name="save", description="Save the current queue as a custom playlist.")
    @app_commands.describe(name="The name of the playlist to save")
    async def save_playlist(self, interaction: discord.Interaction, name: str):
        g_queue = self.cog.get_guild_queue(interaction.guild_id)
        if not g_queue.current and not g_queue.queue:
            return await interaction.response.send_message("⚠️ The queue is empty. Nothing to save.", ephemeral=True)
            
        tracks = []
        if g_queue.current:
            tracks.append(f"{g_queue.current.uploader} - {g_queue.current.title}")
        for song in g_queue.queue:
            tracks.append(f"{song.uploader} - {song.title}")
            
        playlists = _load_playlists()
        gid = str(interaction.guild_id)
        if gid not in playlists:
            playlists[gid] = {}
            
        playlists[gid][name.lower()] = tracks
        _save_playlists(playlists)
        
        embed = discord.Embed(
            title="💾 Playlist Saved",
            description=f"Saved **{len(tracks)}** tracks to playlist `{name}`.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="play", description="Load and play a custom saved playlist.")
    @app_commands.describe(name="The name of the playlist to play")
    async def play_playlist(self, interaction: discord.Interaction, name: str):
        playlists = _load_playlists()
        gid = str(interaction.guild_id)
        
        if gid not in playlists or name.lower() not in playlists[gid]:
            return await interaction.response.send_message(f"⚠️ Playlist `{name}` not found. Did you save it?", ephemeral=True)
            
        queries = playlists[gid][name.lower()]
        if not queries:
            return await interaction.response.send_message(f"⚠️ Playlist `{name}` is empty.", ephemeral=True)
            
        g_queue = self.cog.get_guild_queue(interaction.guild_id)
        g_queue.text_channel = interaction.channel
        
        await interaction.response.defer()
        
        voice_client = await self.cog._ensure_voice_connection(interaction, g_queue)
        if not voice_client:
            return
            
        first_query = queries[0]
        first_song = await self.cog.youtube_service.extract_song(first_query, interaction.user.mention)
        
        if not first_song:
            embed = discord.Embed(
                title="❌ Error",
                description=f"Could not load the first track from the playlist.",
                color=discord.Color.red()
            )
            return await interaction.followup.send(embed=embed)
            
        g_queue.queue.append(first_song)
        start_playing = False
        if not voice_client.is_playing() and not voice_client.is_paused() and g_queue.current is None:
            start_playing = True
            
        embed = discord.Embed(
            title="🎵 Custom Playlist Enqueued",
            description=f"Loading `{name}`: Enqueued 1 of {len(queries)} tracks: **[{first_song.title}]({first_song.webpage_url})**\nResolving remaining tracks in background...",
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed)
        
        if start_playing:
            await self.cog._play_next(interaction.guild_id)
            
        if len(queries) > 1:
            asyncio.create_task(self.cog._process_playlist_background(interaction.guild_id, queries[1:], interaction.user.mention))

class MusicCog(commands.Cog):
    """Cog handling music playback, voice connections, and slash commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.tree.add_command(PlaylistGroup(self))
        self.guild_queues: Dict[int, GuildQueue] = {}
        self.spotify_service = SpotifyService()
        self.youtube_service = YouTubeService()

    def get_guild_queue(self, guild_id: int) -> GuildQueue:
        """Retrieve or create a GuildQueue instance for the given guild."""
        if guild_id not in self.guild_queues:
            self.guild_queues[guild_id] = GuildQueue(guild_id)
        return self.guild_queues[guild_id]

    async def _ensure_voice_connection(
        self, interaction: discord.Interaction, g_queue: GuildQueue
    ) -> Optional[discord.VoiceClient]:
        """Ensure the bot is connected to the caller's voice channel with extended timeout for cloud hosting."""
        if not interaction.user.voice or not interaction.user.voice.channel:
            embed = discord.Embed(
                title="⚠️ Voice Channel Required",
                description="You must be connected to a voice channel to use music commands.",
                color=discord.Color.gold()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
            return None

        user_channel = interaction.user.voice.channel
        voice_client = interaction.guild.voice_client

        try:
            if voice_client is None:
                # Use 60 second extended timeout for cloud container network handshakes
                voice_client = await user_channel.connect(timeout=60.0, reconnect=True, self_deaf=True)
            elif voice_client.channel != user_channel:
                try:
                    await voice_client.move_to(user_channel)
                except Exception:
                    await voice_client.disconnect(force=True)
                    voice_client = await user_channel.connect(timeout=60.0, reconnect=True, self_deaf=True)
            elif not voice_client.is_connected():
                await voice_client.disconnect(force=True)
                voice_client = await user_channel.connect(timeout=60.0, reconnect=True, self_deaf=True)

            g_queue.voice_client = voice_client
            return voice_client

        except (asyncio.TimeoutError, discord.errors.ClientException) as e:
            logger.error(f"Voice connection error in guild {interaction.guild_id}: {e}")
            embed = discord.Embed(
                title="⚠️ Voice Connection Timeout",
                description="Failed to complete voice handshake with Discord servers. Please try running `/play` again.",
                color=discord.Color.red()
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
            return None

    async def _play_next(self, guild_id: int, error: Optional[Exception] = None):
        """Callback to handle playing the next song in the guild queue."""
        if error:
            logger.error(f"Playback error in guild {guild_id}: {error}")

        g_queue = self.get_guild_queue(guild_id)

        async with g_queue.play_lock:
            if not g_queue.voice_client or not g_queue.voice_client.is_connected():
                g_queue.clear()
                return

            old_song = g_queue.current

            # Collapse previous playing message
            if old_song and g_queue.now_playing_message and g_queue.loop_mode != "track":
                try:
                    collapsed = discord.Embed(
                        description=f"✅ **Finished:** [{old_song.title}]({old_song.webpage_url})",
                        color=discord.Color.light_grey()
                    )
                    await g_queue.now_playing_message.edit(embed=collapsed, attachments=[], view=None)
                except Exception:
                    pass
                g_queue.now_playing_message = None

            # Clear dedication after song finishes (unless track looping)
            if g_queue.loop_mode != "track":
                g_queue.dedication = None

            # Handle loop modes
            if g_queue.loop_mode == "track" and g_queue.current:
                song_to_play = g_queue.current
            elif g_queue.loop_mode == "queue" and g_queue.current and not g_queue.queue:
                # Queue loop: re-append the old song and pop from front
                # In queue loop, we re-add the finished song to the end
                g_queue.queue.append(g_queue.current)
                g_queue.current = g_queue.queue.pop(0)
                song_to_play = g_queue.current
            elif g_queue.loop_mode == "queue" and g_queue.current and g_queue.queue:
                # Queue loop with items remaining: re-add current to end, pop next
                g_queue.queue.append(g_queue.current)
                g_queue.current = g_queue.queue.pop(0)
                song_to_play = g_queue.current
            elif g_queue.queue:
                if g_queue.round_robin:
                    g_queue.current = self._pop_round_robin(g_queue)
                else:
                    g_queue.current = g_queue.queue.pop(0)
                song_to_play = g_queue.current
            elif getattr(g_queue, 'autoplay', False) and old_song:
                if g_queue.text_channel:
                    await g_queue.text_channel.send("♾️ *Autoplay finding a related track...*", delete_after=5)

                logger.info(f"Autoplay: triggered for '{old_song.title}' by '{old_song.uploader}'")

                # Track history for dedup (keep last 30)
                g_queue.autoplay_history.append(old_song.title)
                if len(g_queue.autoplay_history) > 30:
                    g_queue.autoplay_history = g_queue.autoplay_history[-30:]

                new_song = None

                import re as _re
                import random

                # Clean YouTube junk to make searches broader
                def _clean_title(t: str) -> str:
                    for pattern in [
                        r'\(Official\s*(Audio|Video|Music\s*Video|Lyric\s*Video|Visualizer)\)',
                        r'\[Official\s*(Audio|Video|Music\s*Video|Lyric\s*Video|Visualizer)\]',
                        r'\(Lyrics?\)', r'\[Lyrics?\]',
                        r'\(Audio\)', r'\[Audio\]',
                        r'\(Explicit\)', r'\[Explicit\]',
                        r'\(Official\)', r'\[Official\]',
                        r'\(HD\)', r'\[HD\]', r'\(HQ\)', r'\[HQ\]',
                        r'official audio', r'official video', r'official music video',
                        r'lyrics', r'lyric video', r'\bHQ\b',
                    ]:
                        t = _re.sub(pattern, '', t, flags=_re.IGNORECASE)
                    return t.strip().strip('-').strip()

                def _clean_artist(a: str) -> str:
                    a = _re.sub(r'VEVO$', '', a, flags=_re.IGNORECASE)
                    a = _re.sub(r'\s*-\s*Topic$', '', a, flags=_re.IGNORECASE)
                    a = _re.sub(r'Official$', '', a, flags=_re.IGNORECASE)
                    return a.strip()

                def _extract_artist_from_title(t: str) -> Optional[str]:
                    if ' - ' in t:
                        return t.split(' - ', 1)[0].strip()
                    return None

                clean_title = _clean_title(old_song.title)
                clean_artist = _clean_artist(old_song.uploader)
                title_artist = _extract_artist_from_title(clean_title)

                if title_artist:
                    search_artist = title_artist
                    search_song = clean_title.split(' - ', 1)[1].strip()
                else:
                    search_artist = clean_artist
                    search_song = clean_title

                logger.info(f"Autoplay: cleaned artist='{search_artist}', song='{search_song}' (raw: '{old_song.title}', '{old_song.uploader}')")

                try:
                    # Strategy: YouTube search with varied keywords for diversity
                    # We avoid putting the exact song name to get different songs by the artist
                    suffixes = [
                        "best songs", "greatest hits", "popular songs", "playlist", "audio", "music", "live"
                    ]
                    suffix = random.choice(suffixes)
                    
                    # Sometimes search just the artist, sometimes artist + song + mix
                    if random.random() > 0.5:
                        fallback_query = f"{search_artist} {suffix}"
                    else:
                        fallback_query = f"{search_artist} {search_song} mix"
                        
                    logger.info(f"Autoplay: YouTube search: '{fallback_query}'")
                    
                    # We pass standard query, not appending 'official audio' here
                    new_song = await self.youtube_service.extract_song(fallback_query, "🤖 Autoplay")
                    
                    # If it returns a dupe, try again one more time with a different approach
                    if new_song and new_song.title in g_queue.autoplay_history:
                        logger.info(f"Autoplay: YouTube returned dupe '{new_song.title}', trying random track")
                        fallback_query = f"{search_artist} audio"
                        # Trick yt-dlp to grab a few results by passing ytsearch5
                        if not fallback_query.startswith("ytsearch"):
                            fallback_query = f"ytsearch5:{fallback_query}"
                            
                        # Need to call yt-dlp differently to get a list, but for now just rely on ytsearch randomness
                        # Actually, our extract_song only returns the *first* result. 
                        # To fix dupes, let's just search something completely different
                        fallback_query = f"top hits {search_artist}"
                        new_song = await self.youtube_service.extract_song(fallback_query, "🤖 Autoplay")
                        
                        if new_song and new_song.title in g_queue.autoplay_history:
                            new_song = None
                            
                    if new_song:
                        logger.info(f"Autoplay: YouTube SUCCESS - playing '{new_song.title}'")
                except Exception as e:
                    logger.error(f"Autoplay: YouTube extraction failed: {e}")

                if new_song:
                    g_queue.current = new_song
                    song_to_play = g_queue.current
                else:
                    logger.warning("Autoplay: all strategies failed, stopping")
                    g_queue.current = None
                    if g_queue.text_channel:
                        embed = discord.Embed(title="🎵 Queue Finished", description="Autoplay couldn't find a related track. Add more songs using `/play`!", color=discord.Color.purple())
                        await g_queue.text_channel.send(embed=embed)
                    return
            else:
                g_queue.current = None
                if g_queue.text_channel:
                    embed = discord.Embed(
                        title="🎵 Queue Finished",
                        description="The music queue is now empty. Add more songs using `/play`!",
                        color=discord.Color.purple()
                    )
                    await g_queue.text_channel.send(embed=embed)
                return

            try:
                # Re-extract if stream URL is older than 3 hours (10,800 seconds)
                if time.time() - song_to_play.added_at > 10800:
                    logger.info(f"Stream URL for '{song_to_play.title}' expired. Re-extracting...")
                    if g_queue.text_channel:
                        await g_queue.text_channel.send(f"🔄 Re-extracting expired URL for **{song_to_play.title}**...", delete_after=3)
                    fresh_song = await self.youtube_service.extract_song(song_to_play.webpage_url, song_to_play.requester)
                    if fresh_song:
                        song_to_play = fresh_song
                        g_queue.current = song_to_play
                    else:
                        logger.error(f"Failed to re-extract '{song_to_play.title}'")
                        if g_queue.text_channel:
                            await g_queue.text_channel.send(f"❌ Failed to play track **{song_to_play.title}**. Skipping...")
                        asyncio.create_task(self._play_next(guild_id))
                        return

                # Construct FFmpeg audio source
                ffmpeg_options = self.youtube_service.FFMPEG_OPTIONS.copy()
                if g_queue.filter_options:
                    ffmpeg_options['options'] = f"{ffmpeg_options.get('options', '-vn')} -af \"{g_queue.filter_options}\""

                source = discord.FFmpegPCMAudio(
                    song_to_play.stream_url,
                    **ffmpeg_options
                )
                transformed_source = discord.PCMVolumeTransformer(source, volume=g_queue.volume)

                def after_callback(err):
                    if err:
                        logger.error(f"Playback error in after_callback: {err}")
                    asyncio.run_coroutine_threadsafe(
                        self._play_next(guild_id, err), self.bot.loop
                    )

                g_queue.voice_client.play(transformed_source, after=after_callback)
                g_queue.play_start_time = time.time()

                # Record stats
                _record_play(guild_id, song_to_play.title, song_to_play.requester)

                # Send Now Playing notification
                if g_queue.text_channel and g_queue.loop_mode != "track":
                    embed = discord.Embed(
                        title="🎶 Now Playing",
                        description=f"**[{song_to_play.title}]({song_to_play.webpage_url})**",
                        color=discord.Color.from_rgb(155, 89, 182)
                    )
                    embed.add_field(name="👤 Uploader", value=song_to_play.uploader, inline=True)
                    embed.add_field(name="⏱️ Duration", value=song_to_play.formatted_duration, inline=True)
                    embed.add_field(name="🎧 Requested By", value=song_to_play.requester, inline=True)
                    embed.add_field(name="🎵 Filters", value=g_queue.filter_options if g_queue.filter_options else "None", inline=True)
                    embed.add_field(name="🔊 Volume", value=f"{int(g_queue.volume * 100)}%", inline=True)
                    loop_display = {"off": "Off", "track": "Track", "queue": "Queue"}
                    embed.add_field(name="🔁 Loop", value=loop_display.get(g_queue.loop_mode, "Off"), inline=True)

                    # Dedication banner
                    if g_queue.dedication:
                        embed.add_field(
                            name="💌 Dedicated",
                            value=f"From {g_queue.dedication['from']} to {g_queue.dedication['to']}",
                            inline=False
                        )
                    
                    if song_to_play.thumbnail:
                        embed.set_thumbnail(url=song_to_play.thumbnail)

                    gif_path = r"C:\Users\howar\Downloads\tumblr_oaku5s68Qn1qf4kz5o1_1280.gif"
                    file = None
                    if os.path.exists(gif_path):
                        try:
                            file = discord.File(gif_path, filename="music.gif")
                            embed.set_image(url="attachment://music.gif")
                        except Exception:
                            pass

                    if file:
                        g_queue.now_playing_message = await g_queue.text_channel.send(embed=embed, file=file, view=MusicPlayerView(self, guild_id))
                    else:
                        g_queue.now_playing_message = await g_queue.text_channel.send(embed=embed, view=MusicPlayerView(self, guild_id))

                # DM notifications
                for user_id in g_queue.notify_users:
                    try:
                        user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                        if user:
                            dm_embed = discord.Embed(
                                title="🔔 Your Song Is Playing!",
                                description=f"**{song_to_play.title}** is now playing in the server.",
                                color=discord.Color.green()
                            )
                            if song_to_play.thumbnail:
                                dm_embed.set_thumbnail(url=song_to_play.thumbnail)
                            await user.send(embed=dm_embed)
                    except Exception:
                        pass

            except Exception as e:
                logger.error(f"Error starting playback for {song_to_play.title}: {e}")
                if g_queue.text_channel:
                    await g_queue.text_channel.send(f"❌ Failed to play track **{song_to_play.title}**. Skipping...")
                asyncio.create_task(self._play_next(guild_id))

    @app_commands.command(name="play", description="Play a song or playlist from Spotify, YouTube, or search query.")
    @app_commands.describe(query="Spotify URL, YouTube URL, or search keywords")
    async def play(self, interaction: discord.Interaction, query: str):
        """Play command handling Spotify URLs, YouTube links, and search queries."""
        g_queue = self.get_guild_queue(interaction.guild_id)
        g_queue.text_channel = interaction.channel

        # Defer response to allow async processing
        await interaction.response.defer()

        # Check voice connection
        voice_client = await self._ensure_voice_connection(interaction, g_queue)
        if not voice_client:
            return

        search_queries: List[str] = []
        is_spotify = SpotifyService.is_spotify_url(query)

        if is_spotify:
            try:
                search_queries = self.spotify_service.parse_url(query)
            except Exception as e:
                embed = discord.Embed(
                    title="❌ Spotify Error",
                    description=str(e),
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return
        else:
            search_queries = [query]

        if not search_queries:
            embed = discord.Embed(
                title="❌ No Tracks Found",
                description="Could not resolve any tracks from your request.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return

        # Extract first song synchronously (so user gets instant feedback and playback starts)
        first_query = search_queries[0]
        first_song = await self.youtube_service.extract_song(first_query, interaction.user.mention)

        if not first_song:
            embed = discord.Embed(
                title="❌ Extraction Error",
                description=f"Could not load audio for query: `{first_query}`",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return

        g_queue.queue.append(first_song)

        # Handle playback start if not currently playing
        start_playing = False
        if not voice_client.is_playing() and not voice_client.is_paused() and g_queue.current is None:
            start_playing = True

        # Send initial confirmation message
        if len(search_queries) == 1:
            embed = discord.Embed(
                title="🎵 Track Enqueued" if not start_playing else "🎶 Starting Playback",
                description=f"**[{first_song.title}]({first_song.webpage_url})**",
                color=discord.Color.green()
            )
            embed.add_field(name="Duration", value=first_song.formatted_duration, inline=True)
            embed.add_field(name="Position in Queue", value=f"#{len(g_queue.queue)}" if not start_playing else "Playing Now", inline=True)
            if first_song.thumbnail:
                embed.set_thumbnail(url=first_song.thumbnail)
            await interaction.followup.send(embed=embed)
        else:
            embed = discord.Embed(
                title="🎵 Spotify Playlist/Album Enqueued",
                description=f"Enqueued 1 of {len(search_queries)} tracks: **[{first_song.title}]({first_song.webpage_url})**\nResolving remaining tracks in background...",
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed)

        # Trigger playback if idle
        if start_playing:
            await self._play_next(interaction.guild_id)

        # Asynchronously extract remaining tracks if a playlist/album was queued
        if len(search_queries) > 1:
            asyncio.create_task(self._process_playlist_background(interaction.guild_id, search_queries[1:], interaction.user.mention))

    async def _process_playlist_background(self, guild_id: int, queries: List[str], requester: str):
        """Process playlist track queries in background without stalling response."""
        g_queue = self.get_guild_queue(guild_id)
        added_count = 0

        for q in queries:
            try:
                song = await self.youtube_service.extract_song(q, requester)
                if song:
                    g_queue.queue.append(song)
                    added_count += 1
            except Exception as e:
                logger.error(f"Background extraction error for '{q}': {e}")
                continue

        if g_queue.text_channel:
            embed = discord.Embed(
                title="✅ Playlist Import Completed",
                description=f"Successfully added **{added_count}** additional tracks to the queue.",
                color=discord.Color.blue()
            )
            await g_queue.text_channel.send(embed=embed)

    @app_commands.command(name="pause", description="Pause the currently playing track.")
    async def pause(self, interaction: discord.Interaction):
        """Pause playback."""
        g_queue = self.get_guild_queue(interaction.guild_id)
        if g_queue.voice_client and g_queue.voice_client.is_playing():
            g_queue.voice_client.pause()
            embed = discord.Embed(title="⏸️ Playback Paused", color=discord.Color.gold())
            await interaction.response.send_message(embed=embed)
        else:
            embed = discord.Embed(title="⚠️ Nothing Playing", description="There is no active audio to pause.", color=discord.Color.gold())
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="resume", description="Resume paused audio playback.")
    async def resume(self, interaction: discord.Interaction):
        """Resume playback."""
        g_queue = self.get_guild_queue(interaction.guild_id)
        if g_queue.voice_client and g_queue.voice_client.is_paused():
            g_queue.voice_client.resume()
            embed = discord.Embed(title="▶️ Playback Resumed", color=discord.Color.green())
            await interaction.response.send_message(embed=embed)
        else:
            embed = discord.Embed(title="⚠️ Playback Not Paused", description="Audio is not currently paused.", color=discord.Color.gold())
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="skip", description="Skip the currently playing track.")
    async def skip(self, interaction: discord.Interaction):
        """Skip current song."""
        g_queue = self.get_guild_queue(interaction.guild_id)
        if g_queue.voice_client and (g_queue.voice_client.is_playing() or g_queue.voice_client.is_paused()):
            skipped_song = g_queue.current
            old_mode = g_queue.loop_mode
            if g_queue.loop_mode == "track":
                g_queue.loop_mode = "off"  # Disable track loop for skip
            g_queue.voice_client.stop()  # Triggers after callback to play next
            title = skipped_song.title if skipped_song else "Current track"
            embed = discord.Embed(title="⏭️ Track Skipped", description=f"Skipped **{title}**", color=discord.Color.blue())
            await interaction.response.send_message(embed=embed)
        else:
            embed = discord.Embed(title="⚠️ Queue Empty", description="There is no track playing to skip.", color=discord.Color.gold())
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="queue", description="Display the current playing song and upcoming queue.")
    async def show_queue(self, interaction: discord.Interaction):
        """Show queue contents in a formatted embed."""
        g_queue = self.get_guild_queue(interaction.guild_id)

        embed = discord.Embed(
            title="📜 Server Music Queue",
            color=discord.Color.purple()
        )

        if g_queue.current:
            now_title = g_queue.current.title[:60] + ('...' if len(g_queue.current.title) > 60 else '')
            embed.add_field(
                name="🎶 Now Playing",
                value=f"**{now_title}** | `{g_queue.current.formatted_duration}` | {g_queue.current.requester}",
                inline=False
            )
        else:
            embed.add_field(name="🎶 Now Playing", value="*Nothing is playing right now.*", inline=False)

        if g_queue.queue:
            queue_lines = []
            char_count = 0
            display_count = min(len(g_queue.queue), 15)
            for idx, song in enumerate(g_queue.queue[:display_count], start=1):
                title_trunc = song.title[:50] + ('...' if len(song.title) > 50 else '')
                line = f"`{idx}.` **{title_trunc}** | `{song.formatted_duration}`"
                if char_count + len(line) + 1 > 950:
                    queue_lines.append(f"*...and {len(g_queue.queue) - idx + 1} more track(s)*")
                    break
                queue_lines.append(line)
                char_count += len(line) + 1
            else:
                if len(g_queue.queue) > display_count:
                    queue_lines.append(f"\n*...and {len(g_queue.queue) - display_count} more track(s)*")

            embed.add_field(name="📋 Upcoming Tracks", value="\n".join(queue_lines), inline=False)
        else:
            embed.add_field(name="📋 Upcoming Tracks", value="*No upcoming tracks in queue.*", inline=False)

        loop_display = {"off": "Off", "track": "Track", "queue": "Queue"}
        embed.set_footer(text=f"Total Queued: {len(g_queue.queue)} tracks | Loop: {loop_display.get(g_queue.loop_mode, 'Off')}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="nowplaying", description="Show detailed information about the currently playing song.")
    async def now_playing(self, interaction: discord.Interaction):
        """Show now playing track details."""
        g_queue = self.get_guild_queue(interaction.guild_id)
        if g_queue.current:
            song = g_queue.current
            embed = discord.Embed(
                title="🎶 Currently Playing",
                description=f"**[{song.title}]({song.webpage_url})**",
                color=discord.Color.from_rgb(155, 89, 182)
            )
            embed.add_field(name="👤 Uploader", value=song.uploader, inline=True)
            embed.add_field(name="⏱️ Duration", value=song.formatted_duration, inline=True)
            embed.add_field(name="🎧 Requested By", value=song.requester, inline=True)
            embed.add_field(name="🎵 Filters", value=g_queue.filter_options if g_queue.filter_options else "None", inline=True)
            embed.add_field(name="🔊 Volume", value=f"{int(g_queue.volume * 100)}%", inline=True)
            
            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)
                
            gif_path = r"C:\Users\howar\Downloads\tumblr_oaku5s68Qn1qf4kz5o1_1280.gif"
            import os
            file = None
            if os.path.exists(gif_path):
                try:
                    file = discord.File(gif_path, filename="music.gif")
                    embed.set_image(url="attachment://music.gif")
                except Exception:
                    pass

            # Send and update the tracked message so it collapses properly when done
            if file:
                await interaction.response.send_message(embed=embed, file=file)
            else:
                await interaction.response.send_message(embed=embed)
            
            # Collapse the old message if it exists so we don't have duplicates
            if g_queue.now_playing_message:
                try:
                    await g_queue.now_playing_message.delete()
                except Exception:
                    pass
            g_queue.now_playing_message = await interaction.original_response()
        else:
            embed = discord.Embed(title="ℹ️ Nothing Playing", description="There is no track currently playing.", color=discord.Color.blue())
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="stop", description="Stop music playback, clear the queue, and disconnect from voice.")
    async def stop(self, interaction: discord.Interaction):
        """Stop music and leave voice channel."""
        g_queue = self.get_guild_queue(interaction.guild_id)
        g_queue.clear()

        if g_queue.voice_client:
            try:
                await g_queue.voice_client.disconnect(force=True)
            except Exception:
                pass
            g_queue.voice_client = None

        embed = discord.Embed(
            title="🛑 Playback Stopped",
            description="Cleared the music queue and disconnected from the voice channel.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leave", description="Disconnect the bot from the voice channel.")
    async def leave(self, interaction: discord.Interaction):
        """Alias for stop command."""
        await self.stop(interaction)

    @app_commands.command(name="volume", description="Change the volume of the bot (1-100).")
    @app_commands.describe(level="Volume level from 1 to 100")
    async def volume(self, interaction: discord.Interaction, level: int):
        """Change the volume."""
        g_queue = self.get_guild_queue(interaction.guild_id)
        if level < 1 or level > 100:
            return await interaction.response.send_message("❌ Volume must be between 1 and 100.", ephemeral=True)
        
        g_queue.volume = level / 100.0
        
        # Apply live if currently playing
        if g_queue.voice_client and g_queue.voice_client.source:
            if isinstance(g_queue.voice_client.source, discord.PCMVolumeTransformer):
                g_queue.voice_client.source.volume = g_queue.volume

        embed = discord.Embed(title="🔊 Volume Changed", description=f"Volume set to **{level}%**", color=discord.Color.green())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="bass", description="Apply a bass boost filter (takes effect on next track).")
    async def bass(self, interaction: discord.Interaction):
        """Apply bass boost filter."""
        g_queue = self.get_guild_queue(interaction.guild_id)
        g_queue.filter_options = "bass=g=15:f=110:w=0.3"
        embed = discord.Embed(title="🎸 Bass Boost Enabled", description="Bass boost will be applied starting from the **next track**.", color=discord.Color.purple())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ultrabass", description="Apply an extreme bass boost/ear rape filter (takes effect on next track).")
    async def ultrabass(self, interaction: discord.Interaction):
        """Apply extreme bass/ear rape filter."""
        g_queue = self.get_guild_queue(interaction.guild_id)
        g_queue.filter_options = "bass=g=20:f=110:w=0.3, volume=5"
        embed = discord.Embed(title="💥 ULTRABASS Enabled", description="Extreme bass/ear rape will be applied starting from the **next track**. RIP headphone users.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="clearfilters", description="Remove all audio filters (takes effect on next track).")
    async def clearfilters(self, interaction: discord.Interaction):
        """Clear audio filters."""
        g_queue = self.get_guild_queue(interaction.guild_id)
        g_queue.filter_options = ""
        embed = discord.Embed(title="🧹 Filters Cleared", description="All audio filters have been removed. Normal playback will resume on the **next track**.", color=discord.Color.green())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="nightcore", description="Apply Nightcore filter (speed up + pitch up).")
    async def nightcore(self, interaction: discord.Interaction):
        g_queue = self.get_guild_queue(interaction.guild_id)
        g_queue.filter_options = "atempo=1.2,asetrate=44100*1.25"
        embed = discord.Embed(title="✨ Nightcore Enabled", description="Nightcore filter will be applied starting from the **next track**.", color=discord.Color.magenta())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="vaporwave", description="Apply Vaporwave filter (slow down + pitch down + reverb).")
    async def vaporwave(self, interaction: discord.Interaction):
        g_queue = self.get_guild_queue(interaction.guild_id)
        g_queue.filter_options = "atempo=0.8,asetrate=44100*0.8,aecho=0.8:0.9:1000:0.3"
        embed = discord.Embed(title="🌴 Vaporwave Enabled", description="Vaporwave filter will be applied starting from the **next track**.", color=discord.Color.teal())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="8d", description="Apply 8D Audio panning filter.")
    async def eight_d(self, interaction: discord.Interaction):
        g_queue = self.get_guild_queue(interaction.guild_id)
        g_queue.filter_options = "apulsator=hz=0.125"
        embed = discord.Embed(title="🎧 8D Audio Enabled", description="8D Audio panning will be applied starting from the **next track**.", color=discord.Color.blue())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="karaoke", description="Apply Karaoke filter (attempts to remove vocals).")
    async def karaoke(self, interaction: discord.Interaction):
        g_queue = self.get_guild_queue(interaction.guild_id)
        g_queue.filter_options = "pan=stereo|c0=c0-c1|c1=c1-c0"
        embed = discord.Embed(title="🎤 Karaoke Enabled", description="Vocal removal will be attempted starting from the **next track**.", color=discord.Color.gold())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="shuffle", description="Shuffle the upcoming queue.")
    async def shuffle(self, interaction: discord.Interaction):
        g_queue = self.get_guild_queue(interaction.guild_id)
        if len(g_queue.queue) > 1:
            import random
            random.shuffle(g_queue.queue)
            await interaction.response.send_message("🔀 The queue has been shuffled!", ephemeral=False)
        else:
            await interaction.response.send_message("⚠️ Not enough songs in the queue to shuffle.", ephemeral=True)

    @app_commands.command(name="remove", description="Remove a specific song from the queue.")
    @app_commands.describe(index="The queue number of the song to remove")
    async def remove(self, interaction: discord.Interaction, index: int):
        g_queue = self.get_guild_queue(interaction.guild_id)
        if 1 <= index <= len(g_queue.queue):
            removed = g_queue.queue.pop(index - 1)
            await interaction.response.send_message(f"🗑️ Removed **{removed.title}** from the queue.")
        else:
            await interaction.response.send_message("⚠️ Invalid queue number.", ephemeral=True)

    @app_commands.command(name="clear", description="Clear all upcoming songs from the queue.")
    async def clear_queue(self, interaction: discord.Interaction):
        g_queue = self.get_guild_queue(interaction.guild_id)
        g_queue.queue.clear()
        await interaction.response.send_message("🧹 The queue has been cleared.")

    @app_commands.command(name="autoplay", description="Toggle Autoplay mode (endless radio).")
    async def autoplay(self, interaction: discord.Interaction):
        g_queue = self.get_guild_queue(interaction.guild_id)
        g_queue.autoplay = not getattr(g_queue, 'autoplay', False)
        state = "Enabled" if g_queue.autoplay else "Disabled"
        await interaction.response.send_message(f"♾️ Autoplay is now **{state}**.")

    @app_commands.command(name="lyrics", description="Fetch lyrics for the currently playing song or a specific search.")
    @app_commands.describe(query="Leave blank for current song, or type a song name to search.")
    async def lyrics(self, interaction: discord.Interaction, query: Optional[str] = None):
        """Fetch lyrics via lrclib.net API."""
        await interaction.response.defer()
        
        search_term = query
        if not search_term:
            g_queue = self.get_guild_queue(interaction.guild_id)
            if g_queue.current:
                search_term = f"{g_queue.current.title} {g_queue.current.uploader}"
            else:
                return await interaction.followup.send("⚠️ Nothing is playing right now. Please provide a search query.")
                
        import aiohttp
        from urllib.parse import quote
        
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://lrclib.net/api/search?q={quote(search_term)}"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and len(data) > 0:
                            track = data[0]
                            lyrics_text = track.get("plainLyrics") or track.get("syncedLyrics")
                            
                            if not lyrics_text:
                                return await interaction.followup.send("❌ Lyrics found but they are empty.")
                                
                            # Discord limits descriptions to 4096 chars
                            trimmed_lyrics = lyrics_text[:4090] + "..." if len(lyrics_text) > 4096 else lyrics_text
                            
                            embed = discord.Embed(
                                title=f"🎤 Lyrics: {track.get('trackName', 'Unknown')} by {track.get('artistName', 'Unknown')}",
                                description=trimmed_lyrics,
                                color=discord.Color.blue()
                            )
                            embed.set_footer(text="Powered by LRCLIB")
                            return await interaction.followup.send(embed=embed)
                        else:
                            return await interaction.followup.send(f"❌ No lyrics found for `{search_term}`.")
                    else:
                        return await interaction.followup.send("❌ Lyrics API is currently unavailable.")
        except Exception as e:
            return await interaction.followup.send(f"❌ Error fetching lyrics: {e}")

    @app_commands.command(name="loop", description="Cycle loop mode: Off -> Track -> Queue.")
    async def loop(self, interaction: discord.Interaction):
        g_queue = self.get_guild_queue(interaction.guild_id)
        cycle = {"off": "track", "track": "queue", "queue": "off"}
        g_queue.loop_mode = cycle.get(g_queue.loop_mode, "off")
        icons = {"off": "➡️", "track": "🔂", "queue": "🔁"}
        labels = {"off": "Off", "track": "Track Loop", "queue": "Queue Loop"}
        await interaction.response.send_message(
            f"{icons[g_queue.loop_mode]} Loop mode set to **{labels[g_queue.loop_mode]}**."
        )

    @app_commands.command(name="seek", description="Jump to a specific timestamp in the current track.")
    @app_commands.describe(timestamp="Timestamp to jump to (e.g. 1:30 or 90)")
    async def seek(self, interaction: discord.Interaction, timestamp: str):
        """Seek to a position in the current track by restarting FFmpeg with -ss."""
        g_queue = self.get_guild_queue(interaction.guild_id)
        if not g_queue.current or not g_queue.voice_client:
            return await interaction.response.send_message("⚠️ Nothing is playing.", ephemeral=True)

        # Parse timestamp
        parts = timestamp.strip().split(":")
        try:
            if len(parts) == 1:
                seconds = int(parts[0])
            elif len(parts) == 2:
                seconds = int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            else:
                raise ValueError()
        except (ValueError, IndexError):
            return await interaction.response.send_message("⚠️ Invalid timestamp. Use format like `1:30` or `90`.", ephemeral=True)

        if seconds < 0 or (g_queue.current.duration and seconds > g_queue.current.duration):
            return await interaction.response.send_message("⚠️ Timestamp is out of range.", ephemeral=True)

        await interaction.response.defer()

        song = g_queue.current
        # Build new FFmpeg source with -ss seek
        ffmpeg_options = self.youtube_service.FFMPEG_OPTIONS.copy()
        before_opts = ffmpeg_options.get('before_options', '')
        before_opts = f"{before_opts} -ss {seconds}"
        ffmpeg_options['before_options'] = before_opts
        if g_queue.filter_options:
            ffmpeg_options['options'] = f"{ffmpeg_options.get('options', '-vn')} -af \"{g_queue.filter_options}\""

        source = discord.FFmpegPCMAudio(song.stream_url, **ffmpeg_options)
        transformed = discord.PCMVolumeTransformer(source, volume=g_queue.volume)

        # Stop current playback without triggering _play_next
        # We do this by temporarily setting loop_mode to track so _play_next replays
        old_loop = g_queue.loop_mode
        g_queue.loop_mode = "track"  # prevent queue advancement

        if g_queue.voice_client.is_playing() or g_queue.voice_client.is_paused():
            g_queue.voice_client.stop()
            # Wait a moment for the stop to process
            await asyncio.sleep(0.5)

        g_queue.loop_mode = old_loop  # restore
        g_queue.play_start_time = time.time() - seconds

        def after_callback(err):
            fut = asyncio.run_coroutine_threadsafe(
                self._play_next(interaction.guild_id, err), self.bot.loop
            )
            try:
                fut.result()
            except Exception as e:
                logger.error(f"Error in seek after_callback: {e}")

        g_queue.voice_client.play(transformed, after=after_callback)

        mins, secs = divmod(seconds, 60)
        await interaction.followup.send(f"⏩ Jumped to **{mins}:{secs:02d}**")

    @app_commands.command(name="stats", description="View listening stats for this server.")
    async def stats(self, interaction: discord.Interaction):
        stats = _load_stats()
        gid = str(interaction.guild_id)
        if gid not in stats or not stats[gid]["songs"]:
            return await interaction.response.send_message("📊 No listening data recorded yet. Play some music!")

        guild_stats = stats[gid]
        # Top 10 songs
        sorted_songs = sorted(guild_stats["songs"].items(), key=lambda x: x[1], reverse=True)[:10]
        song_list = "\n".join([f"`{i+1}.` **{name}** — {count} plays" for i, (name, count) in enumerate(sorted_songs)])

        # Top 5 users
        sorted_users = sorted(guild_stats["users"].items(), key=lambda x: x[1], reverse=True)[:5]
        user_list = "\n".join([f"`{i+1}.` <@{uid}> — {count} songs queued" for i, (uid, count) in enumerate(sorted_users)])

        total_plays = sum(guild_stats["songs"].values())

        embed = discord.Embed(
            title="📊 Server Listening Stats",
            color=discord.Color.gold()
        )
        embed.add_field(name="🏆 Most Played Songs", value=song_list or "None", inline=False)
        embed.add_field(name="🎧 Top DJs", value=user_list or "None", inline=False)
        embed.set_footer(text=f"Total plays recorded: {total_plays}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="notify", description="Toggle DM notifications when your requested song starts playing.")
    async def notify(self, interaction: discord.Interaction):
        g_queue = self.get_guild_queue(interaction.guild_id)
        uid = interaction.user.id
        if uid in g_queue.notify_users:
            g_queue.notify_users.discard(uid)
            await interaction.response.send_message("🔕 DM notifications **disabled**. You won't be notified when your songs play.", ephemeral=True)
        else:
            g_queue.notify_users.add(uid)
            await interaction.response.send_message("🔔 DM notifications **enabled**! You'll get a DM when your requested song starts playing.", ephemeral=True)

    @app_commands.command(name="dedicate", description="Dedicate the currently playing song to someone.")
    @app_commands.describe(user="The person to dedicate the song to")
    async def dedicate(self, interaction: discord.Interaction, user: discord.Member):
        g_queue = self.get_guild_queue(interaction.guild_id)
        if not g_queue.current:
            return await interaction.response.send_message("⚠️ Nothing is playing right now.", ephemeral=True)

        g_queue.dedication = {
            "from": interaction.user.mention,
            "to": user.mention
        }

        embed = discord.Embed(
            title="💌 Song Dedication",
            description=f"{interaction.user.mention} dedicated **{g_queue.current.title}** to {user.mention}!",
            color=discord.Color.from_rgb(255, 105, 180)
        )
        if g_queue.current.thumbnail:
            embed.set_thumbnail(url=g_queue.current.thumbnail)
        await interaction.response.send_message(embed=embed)

        # Also update the now playing embed if it exists
        if g_queue.now_playing_message:
            try:
                old_embed = g_queue.now_playing_message.embeds[0] if g_queue.now_playing_message.embeds else None
                if old_embed:
                    old_embed.add_field(
                        name="💌 Dedicated",
                        value=f"From {interaction.user.mention} to {user.mention}",
                        inline=False
                    )
                    await g_queue.now_playing_message.edit(embed=old_embed)
            except Exception:
                pass

        # DM the recipient
        try:
            dm_embed = discord.Embed(
                title="💌 Someone Dedicated a Song to You!",
                description=f"**{interaction.user.display_name}** dedicated **{g_queue.current.title}** to you in **{interaction.guild.name}**!",
                color=discord.Color.from_rgb(255, 105, 180)
            )
            if g_queue.current.thumbnail:
                dm_embed.set_thumbnail(url=g_queue.current.thumbnail)
            await user.send(embed=dm_embed)
        except Exception:
            pass

    # ── Round Robin ──────────────────────────────────────────────────────────

    def _pop_round_robin(self, g_queue: GuildQueue) -> Song:
        """Pop the next song using round-robin rotation between requesters.
        
        Cycles through unique requesters so no single user dominates the queue.
        If the next user in rotation has no songs left, skip to the next user who does.
        """
        # Build ordered list of unique requesters who still have songs in queue
        requesters_in_queue = []
        seen = set()
        for song in g_queue.queue:
            if song.requester not in seen:
                seen.add(song.requester)
                requesters_in_queue.append(song.requester)

        if not requesters_in_queue:
            return g_queue.queue.pop(0)  # fallback

        # Find who played last, pick the next person in rotation
        last = g_queue._rr_last_requester
        if last in requesters_in_queue:
            idx = (requesters_in_queue.index(last) + 1) % len(requesters_in_queue)
        else:
            idx = 0
        next_requester = requesters_in_queue[idx]

        # Find that requester's first song in the queue and pop it
        for i, song in enumerate(g_queue.queue):
            if song.requester == next_requester:
                g_queue._rr_last_requester = next_requester
                return g_queue.queue.pop(i)

        # Shouldn't happen, but fallback
        return g_queue.queue.pop(0)

    @app_commands.command(name="roundrobin", description="Toggle fair queue mode. Alternates songs between users instead of first-come-first-served.")
    async def roundrobin(self, interaction: discord.Interaction):
        """Toggle round-robin fair queue mode."""
        g_queue = self.get_guild_queue(interaction.guild_id)
        g_queue.round_robin = not g_queue.round_robin
        g_queue._rr_last_requester = None  # reset rotation

        if g_queue.round_robin:
            embed = discord.Embed(
                title="⚖️ Fair Queue Enabled",
                description="The queue will now alternate between users so everyone gets a turn to DJ.",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="⚖️ Fair Queue Disabled",
                description="Queue is back to first-come-first-served.",
                color=discord.Color.gold()
            )
        await interaction.response.send_message(embed=embed)

    # ── Song Quiz ────────────────────────────────────────────────────────────

    # Default quiz songs if the server has no listening history
    DEFAULT_QUIZ_SONGS = [
        "Bohemian Rhapsody", "Hotel California", "Stairway to Heaven",
        "Smells Like Teen Spirit", "Billie Jean", "Hey Jude",
        "Wonderwall", "Sweet Child O Mine", "Losing My Religion",
        "Under the Bridge", "Creep", "Mr Brightside",
        "Somebody That I Used To Know", "Rolling in the Deep",
        "Blinding Lights", "Shape of You", "Uptown Funk",
        "Old Town Road", "Bad Guy", "Sunflower",
        "Stressed Out", "Radioactive", "Thunder",
        "Happier", "Circles", "Peaches", "Levitating",
        "drivers license", "Stay", "Watermelon Sugar",
        "Bohemian Like You", "Take Me Out", "Seven Nation Army",
        "Feel Good Inc", "Clint Eastwood", "Hysteria",
        "In The End", "Numb", "Crawling",
        "Chop Suey", "Toxicity", "B.Y.O.B.",
        "Enter Sandman", "Nothing Else Matters", "One",
        "Lose Yourself", "Stan", "Without Me",
        "HUMBLE", "Alright", "DNA",
    ]

    @staticmethod
    def _fuzzy_match(guess: str, answer: str) -> bool:
        """Check if a guess is close enough to the answer.
        
        Strips punctuation, lowercases, handles 'Artist - Song' format,
        and checks for substring containment or high word overlap.
        """
        def clean(s: str) -> str:
            s = s.lower().strip()
            s = _re_module.sub(r'[^a-z0-9\s]', '', s)
            s = _re_module.sub(r'\s+', ' ', s)
            return s.strip()

        clean_guess = clean(guess)
        
        # Build a list of possible correct answers from the raw answer
        # e.g. "Dreamville - Heaven's EP (with J. Cole)" produces:
        #   - the full cleaned string
        #   - just the song part after " - "
        #   - just the artist part before " - "
        possible_answers = [clean(answer)]
        if ' - ' in answer:
            parts = answer.split(' - ', 1)
            possible_answers.append(clean(parts[0]))  # artist
            possible_answers.append(clean(parts[1]))  # song title
        # Also try stripping featured artist tags like (with X), (feat. X), (ft. X)
        stripped = _re_module.sub(r'\s*[\(\[](?:with|feat\.?|ft\.?).*?[\)\]]', '', answer, flags=_re_module.IGNORECASE)
        if clean(stripped) not in possible_answers:
            possible_answers.append(clean(stripped))
        if ' - ' in stripped:
            song_part = stripped.split(' - ', 1)[1]
            if clean(song_part) not in possible_answers:
                possible_answers.append(clean(song_part))

        if not clean_guess:
            return False

        for clean_answer in possible_answers:
            if not clean_answer:
                continue

            # Exact match
            if clean_guess == clean_answer:
                return True

            # Substring: if the answer is fully contained in the guess or vice versa
            if clean_answer in clean_guess or clean_guess in clean_answer:
                return True

            # Word overlap: if 50%+ of the answer's words appear in the guess
            answer_words = set(clean_answer.split())
            guess_words = set(clean_guess.split())
            if len(answer_words) > 0:
                overlap = len(answer_words & guess_words) / len(answer_words)
                if overlap >= 0.5:
                    return True

        return False

    @app_commands.command(name="quiz", description="Start a song guessing game! The bot plays clips and you guess the song.")
    @app_commands.describe(rounds="Number of rounds to play (default: 5, max: 15)")
    async def quiz(self, interaction: discord.Interaction, rounds: Optional[int] = 5):
        """Song quiz mini-game. Plays short clips, users guess in chat."""
        g_queue = self.get_guild_queue(interaction.guild_id)

        if g_queue.quiz_active:
            return await interaction.response.send_message("⚠️ A quiz is already running! Wait for it to finish.", ephemeral=True)

        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message("⚠️ You need to be in a voice channel to start a quiz.", ephemeral=True)

        rounds = max(1, min(rounds or 5, 15))

        await interaction.response.defer()

        # Connect to voice if needed
        voice_client = await self._ensure_voice_connection(interaction, g_queue)
        if not voice_client:
            return

        g_queue.quiz_active = True
        g_queue.quiz_scores = {}
        g_queue.text_channel = interaction.channel

        # Build song pool: prefer server listening history, fall back to defaults
        stats = _load_stats()
        gid = str(interaction.guild_id)
        song_pool = []
        if gid in stats and stats[gid].get("songs"):
            song_pool = list(stats[gid]["songs"].keys())
        if len(song_pool) < rounds:
            song_pool.extend(self.DEFAULT_QUIZ_SONGS)

        # Remove dupes and shuffle
        seen_titles = set()
        unique_pool = []
        for s in song_pool:
            if s.lower() not in seen_titles:
                seen_titles.add(s.lower())
                unique_pool.append(s)
        random.shuffle(unique_pool)
        quiz_songs = unique_pool[:rounds]

        embed = discord.Embed(
            title="🏆 Song Quiz Starting!",
            description=f"**{rounds} rounds** — I'll play a 20-second clip and you type the song name in chat.\nFirst correct guess gets the point!",
            color=discord.Color.gold()
        )
        await interaction.followup.send(embed=embed)
        await asyncio.sleep(3)

        for round_num, song_query in enumerate(quiz_songs, start=1):
            if not g_queue.quiz_active:
                break  # quiz was cancelled

            # Announce round
            round_embed = discord.Embed(
                title=f"🎵 Round {round_num}/{rounds}",
                description="Listen to the clip and type your guess!",
                color=discord.Color.blue()
            )
            await g_queue.text_channel.send(embed=round_embed)

            # Extract the song
            try:
                song = await self.youtube_service.extract_song(song_query, "🏆 Quiz")
            except Exception:
                song = None

            if not song:
                await g_queue.text_channel.send(f"*Skipping round {round_num} — couldn't load the track.*")
                continue

            # Set the answer (clean the title for matching)
            # Strip common YouTube junk from the answer
            answer_raw = song.title
            for pattern in [
                r'\(Official.*?\)', r'\[Official.*?\]',
                r'\(Lyrics?\)', r'\[Lyrics?\]',
                r'\(Audio\)', r'\[Audio\]',
                r'\(Explicit\)', r'\[Explicit\]',
                r'\(HD\)', r'\[HD\]', r'\(HQ\)', r'\[HQ\]',
                r'official audio', r'official video', r'official music video',
                r'lyric video', r'\bHQ\b', r'\bHD\b',
            ]:
                answer_raw = _re_module.sub(pattern, '', answer_raw, flags=_re_module.IGNORECASE)
            g_queue.quiz_answer = answer_raw.strip().strip('-').strip()

            # Play a 20-second clip starting partway into the song
            ffmpeg_options = self.youtube_service.FFMPEG_OPTIONS.copy()
            # Start between 20-60 seconds in (or 10s if short song)
            start_at = random.randint(10, max(10, min(60, (song.duration or 120) - 30)))
            ffmpeg_options['before_options'] = f"{ffmpeg_options.get('before_options', '')} -ss {start_at}"
            ffmpeg_options['options'] = f"{ffmpeg_options.get('options', '-vn')} -t 20"

            source = discord.FFmpegPCMAudio(song.stream_url, **ffmpeg_options)
            transformed = discord.PCMVolumeTransformer(source, volume=g_queue.volume)

            # Stop any current playback
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()
                await asyncio.sleep(0.3)

            voice_client.play(transformed)

            # Wait for guesses (30 seconds max)
            round_winner = None
            try:
                def check(msg):
                    if msg.channel.id != g_queue.text_channel.id:
                        return False
                    if msg.author.bot:
                        return False
                    return self._fuzzy_match(msg.content, g_queue.quiz_answer)

                winner_msg = await self.bot.wait_for('message', check=check, timeout=30.0)
                round_winner = winner_msg.author
            except asyncio.TimeoutError:
                pass

            # Stop the clip if still playing
            if voice_client.is_playing():
                voice_client.stop()

            # Announce result
            if round_winner:
                g_queue.quiz_scores[round_winner.id] = g_queue.quiz_scores.get(round_winner.id, 0) + 1
                result_embed = discord.Embed(
                    title="✅ Correct!",
                    description=f"**{round_winner.display_name}** got it! The song was **{g_queue.quiz_answer}**",
                    color=discord.Color.green()
                )
            else:
                result_embed = discord.Embed(
                    title="⏰ Time's Up!",
                    description=f"Nobody got it. The song was **{g_queue.quiz_answer}**",
                    color=discord.Color.red()
                )
            await g_queue.text_channel.send(embed=result_embed)
            await asyncio.sleep(3)

        # Quiz over — show scoreboard
        g_queue.quiz_active = False
        g_queue.quiz_answer = None

        if g_queue.quiz_scores:
            sorted_scores = sorted(g_queue.quiz_scores.items(), key=lambda x: x[1], reverse=True)
            scoreboard = "\n".join(
                [f"`{i+1}.` <@{uid}> — **{score}** point{'s' if score != 1 else ''}" for i, (uid, score) in enumerate(sorted_scores)]
            )
            winner_id = sorted_scores[0][0]
            final_embed = discord.Embed(
                title="🏆 Quiz Over!",
                description=f"**<@{winner_id}> wins!**\n\n{scoreboard}",
                color=discord.Color.gold()
            )
        else:
            final_embed = discord.Embed(
                title="🏆 Quiz Over!",
                description="Nobody scored any points! Better luck next time.",
                color=discord.Color.gold()
            )
        await g_queue.text_channel.send(embed=final_embed)

    @app_commands.command(name="features", description="View all the features and commands of Bifrost Music.")
    async def features(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎧 Bifrost Music - Features & Commands",
            description="A premium, high-fidelity music bot capable of playing from Spotify and YouTube with zero audio degradation. Built to deliver an immersive and interactive listening experience.",
            color=discord.Color.purple()
        )
        if self.bot.user and self.bot.user.display_avatar:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
            
        embed.add_field(
            name="🎵 Playback Commands",
            value="`/play` - Play a song or Spotify playlist\n`/pause` - Pause music\n`/resume` - Resume music\n`/skip` - Skip track\n`/stop` - Stop and disconnect\n`/nowplaying` - View the current track details\n`/seek <time>` - Jump to a timestamp\n`/loop` - Cycle loop mode (Off/Track/Queue)",
            inline=False
        )
        embed.add_field(
            name="🎛️ Audio Controls",
            value="`/volume <0-100>` - Adjust volume\n`/bass` - Enable standard bass boost\n`/ultrabass` - Enable extreme bass boost\n`/nightcore` - Speed & pitch up\n`/vaporwave` - Slow, pitch down, reverb\n`/8d` - 3D audio panning\n`/karaoke` - Remove center vocals\n`/clearfilters` - Remove all audio filters",
            inline=False
        )
        embed.add_field(
            name="📜 Queue Management",
            value="`/queue` - View upcoming songs\n`/shuffle` - Randomize the queue\n`/remove <index>` - Remove a specific song\n`/clear` - Wipe the entire queue\n`/roundrobin` - Toggle fair queue (alternates between users)",
            inline=False
        )
        embed.add_field(
            name="✨ Premium Features",
            value="♾️ `/autoplay` - Endless radio mode\n🎤 `/lyrics` - Fetch song lyrics\n📊 `/stats` - Server listening leaderboard\n🔔 `/notify` - DM when your song plays\n💌 `/dedicate @user` - Dedicate a song\n🏆 `/quiz` - Song guessing game\n💽 `/playlist save|play` - Save and load custom queues\n🔘 **Interactive UI** - Buttons on Now Playing embed",
            inline=False
        )
        embed.set_footer(text="Bifrost Music • Created for the best listening experience")
        
        await interaction.response.send_message(embed=embed)

async def setup(bot: commands.Bot):
    """Add MusicCog to bot."""
    await bot.add_cog(MusicCog(bot))
