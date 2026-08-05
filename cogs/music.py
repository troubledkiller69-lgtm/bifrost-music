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
        self.autoplay: bool = False
        self.volume: float = 0.8
        self.filter_options: str = ""
        self.voice_client: Optional[discord.VoiceClient] = None
        self.text_channel: Optional[discord.TextChannel] = None
        self.now_playing_message: Optional[discord.Message] = None
        self.play_lock = asyncio.Lock()

    def clear(self):
        """Reset queue state."""
        self.queue.clear()
        self.current = None
        self.now_playing_message = None

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
            g_queue.loop = False
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
            if old_song and g_queue.now_playing_message and not g_queue.loop:
                try:
                    collapsed = discord.Embed(
                        description=f"✅ **Finished:** [{old_song.title}]({old_song.webpage_url})",
                        color=discord.Color.light_grey()
                    )
                    await g_queue.now_playing_message.edit(embed=collapsed, attachments=[])
                except Exception:
                    pass
                g_queue.now_playing_message = None

            # Handle looping
            if g_queue.loop and g_queue.current:
                song_to_play = g_queue.current
            elif g_queue.queue:
                g_queue.current = g_queue.queue.pop(0)
                song_to_play = g_queue.current
            elif getattr(g_queue, 'autoplay', False) and old_song:
                if g_queue.text_channel:
                    await g_queue.text_channel.send("♾️ *Autoplay searching for next track...*", delete_after=5)
                try:
                    new_song = await self.youtube_service.extract_song(f"{old_song.uploader} official audio", "🤖 Autoplay")
                    if new_song:
                        g_queue.current = new_song
                        song_to_play = g_queue.current
                    else:
                        raise Exception("No related track found")
                except Exception:
                    g_queue.current = None
                    if g_queue.text_channel:
                        embed = discord.Embed(title="🎵 Queue Finished", description="Autoplay failed to find a related track. Add more songs using `/play`!", color=discord.Color.purple())
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
                    embed.add_field(name="👤 Uploader", value=song_to_play.uploader, inline=True)
                    embed.add_field(name="⏱️ Duration", value=song_to_play.formatted_duration, inline=True)
                    embed.add_field(name="🎧 Requested By", value=song_to_play.requester, inline=True)
                    embed.add_field(name="🎵 Filters", value=g_queue.filter_options if g_queue.filter_options else "None", inline=True)
                    embed.add_field(name="🔊 Volume", value=f"{int(g_queue.volume * 100)}%", inline=True)
                    
                    if song_to_play.thumbnail:
                        embed.set_thumbnail(url=song_to_play.thumbnail)

                    gif_path = r"C:\Users\howar\Downloads\tumblr_oaku5s68Qn1qf4kz5o1_1280.gif"
                    import os
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
            value="`/play` - Play a song or Spotify playlist\n`/pause` - Pause music\n`/resume` - Resume music\n`/skip` - Skip track\n`/stop` - Stop and disconnect\n`/nowplaying` - View the current track details",
            inline=False
        )
        embed.add_field(
            name="🎛️ Audio Controls",
            value="`/volume <0-200>` - Adjust volume\n`/bass` - Enable standard bass boost\n`/ultrabass` - Enable extreme bass boost\n`/clearfilters` - Remove all audio filters",
            inline=False
        )
        embed.add_field(
            name="📜 Queue Management",
            value="`/queue` - View upcoming songs\n`/shuffle` - Randomize the queue\n`/remove <index>` - Remove a specific song\n`/clear` - Wipe the entire queue",
            inline=False
        )
        embed.add_field(
            name="✨ Premium Features",
            value="♾️ **Autoplay**: Toggle endless radio mode with `/autoplay`\n🎤 **Lyrics**: Fetch lyrics with `/lyrics`\n🔘 **Interactive UI**: Control playback directly from the Now Playing embed buttons",
            inline=False
        )
        embed.set_footer(text="Bifrost Music • Created for the best listening experience")
        
        await interaction.response.send_message(embed=embed)

async def setup(bot: commands.Bot):
    """Add MusicCog to bot."""
    await bot.add_cog(MusicCog(bot))
