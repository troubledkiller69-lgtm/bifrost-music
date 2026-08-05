import asyncio
import logging
from typing import Dict, Optional, List
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
        self.loop: bool = False
        self.volume: float = 0.8
        self.voice_client: Optional[discord.VoiceClient] = None
        self.text_channel: Optional[discord.TextChannel] = None
        self.play_lock = asyncio.Lock()

    def clear(self):
        """Reset queue state."""
        self.queue.clear()
        self.current = None

class MusicCog(commands.Cog):
    """Cog handling music playback, voice connections, and slash commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.spotify_service = SpotifyService()
        self.youtube_service = YouTubeService()
        self.guild_queues: Dict[int, GuildQueue] = {}

    def get_guild_queue(self, guild_id: int) -> GuildQueue:
        """Retrieve or create a GuildQueue instance for the given guild."""
        if guild_id not in self.guild_queues:
            self.guild_queues[guild_id] = GuildQueue(guild_id)
        return self.guild_queues[guild_id]

    async def _ensure_voice_connection(
        self, interaction: discord.Interaction, g_queue: GuildQueue
    ) -> Optional[discord.VoiceClient]:
        """Ensure the bot is connected to the caller's voice channel."""
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

        if voice_client is None:
            voice_client = await user_channel.connect(reconnect=True, self_deaf=True)
            g_queue.voice_client = voice_client
        elif voice_client.channel != user_channel:
            await voice_client.move_to(user_channel)
            g_queue.voice_client = voice_client

        g_queue.voice_client = voice_client
        return voice_client

    async def _play_next(self, guild_id: int, error: Optional[Exception] = None):
        """Callback to handle playing the next song in the guild queue."""
        if error:
            logger.error(f"Playback error in guild {guild_id}: {error}")

        g_queue = self.get_guild_queue(guild_id)

        async with g_queue.play_lock:
            if not g_queue.voice_client or not g_queue.voice_client.is_connected():
                g_queue.clear()
                return

            # Handle looping
            if g_queue.loop and g_queue.current:
                song_to_play = g_queue.current
            elif g_queue.queue:
                g_queue.current = g_queue.queue.pop(0)
                song_to_play = g_queue.current
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
                # Construct FFmpeg audio source
                source = discord.FFmpegPCMAudio(
                    song_to_play.stream_url,
                    **self.youtube_service.FFMPEG_OPTIONS
                )
                transformed_source = discord.PCMVolumeTransformer(source, volume=g_queue.volume)

                def after_callback(err):
                    fut = asyncio.run_coroutine_threadsafe(
                        self._play_next(guild_id, err), self.bot.loop
                    )
                    try:
                        fut.result()
                    except Exception as e:
                        logger.error(f"Error in after_callback result: {e}")

                g_queue.voice_client.play(transformed_source, after=after_callback)

                # Send Now Playing notification
                if g_queue.text_channel and not g_queue.loop:
                    embed = discord.Embed(
                        title="🎶 Now Playing",
                        description=f"**[{song_to_play.title}]({song_to_play.webpage_url})**",
                        color=discord.Color.from_rgb(155, 89, 182)
                    )
                    embed.add_field(name="Uploader", value=song_to_play.uploader, inline=True)
                    embed.add_field(name="Duration", value=song_to_play.formatted_duration, inline=True)
                    embed.add_field(name="Requested By", value=song_to_play.requester, inline=True)
                    if song_to_play.thumbnail:
                        embed.set_thumbnail(url=song_to_play.thumbnail)
                    await g_queue.text_channel.send(embed=embed)

            except Exception as e:
                logger.error(f"Error starting playback for {song_to_play.title}: {e}")
                if g_queue.text_channel:
                    await g_queue.text_channel.send(f"❌ Failed to play track **{song_to_play.title}**. Skipping...")
                await self._play_next(guild_id)

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
            g_queue.loop = False  # Disable loop temporarily for manually skipped track
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
            embed.add_field(
                name="🔊 Now Playing",
                value=f"**[{g_queue.current.title}]({g_queue.current.webpage_url})** | `{g_queue.current.formatted_duration}` | Requested by {g_queue.current.requester}",
                inline=False
            )
        else:
            embed.add_field(name="🔊 Now Playing", value="*Nothing is playing right now.*", inline=False)

        if g_queue.queue:
            queue_description = ""
            for idx, song in enumerate(g_queue.queue[:10], start=1):
                queue_description += f"`{idx}.` **[{song.title}]({song.webpage_url})** | `{song.formatted_duration}`\n"
            
            if len(g_queue.queue) > 10:
                queue_description += f"\n*...and {len(g_queue.queue) - 10} more track(s)*"

            embed.add_field(name="📋 Upcoming Tracks", value=queue_description, inline=False)
        else:
            embed.add_field(name="📋 Upcoming Tracks", value="*No upcoming tracks in queue.*", inline=False)

        embed.set_footer(text=f"Total Queued: {len(g_queue.queue)} tracks | Loop: {'On' if g_queue.loop else 'Off'}")
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
            embed.add_field(name="Uploader", value=song.uploader, inline=True)
            embed.add_field(name="Duration", value=song.formatted_duration, inline=True)
            embed.add_field(name="Requested By", value=song.requester, inline=True)
            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)
            await interaction.response.send_message(embed=embed)
        else:
            embed = discord.Embed(title="ℹ️ Nothing Playing", description="There is no track currently playing.", color=discord.Color.blue())
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="stop", description="Stop music playback, clear the queue, and disconnect from voice.")
    async def stop(self, interaction: discord.Interaction):
        """Stop music and leave voice channel."""
        g_queue = self.get_guild_queue(interaction.guild_id)
        g_queue.clear()

        if g_queue.voice_client:
            await g_queue.voice_client.disconnect()
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

async def setup(bot: commands.Bot):
    """Add MusicCog to bot."""
    await bot.add_cog(MusicCog(bot))
