import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
import yt_dlp

logger = logging.getLogger("BifrostMusic.YouTube")

@dataclass
class Song:
    """Dataclass holding song metadata and stream URL."""
    title: str
    stream_url: str
    webpage_url: str
    video_id: str
    duration: int
    thumbnail: str
    uploader: str
    requester: str

    @property
    def formatted_duration(self) -> str:
        """Return duration formatted as HH:MM:SS or MM:SS."""
        if not self.duration:
            return "Live Stream"
        minutes, seconds = divmod(self.duration, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

class YouTubeService:
    """Service to handle audio extraction via yt-dlp."""

    YTDL_OPTIONS = {
        'format': 'bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'ytsearch',
        'source_address': '0.0.0.0',  # Bind to IPv4
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'android', 'web'],
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        },
    }

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    }

    def __init__(self):
        self.ytdl = yt_dlp.YoutubeDL(self.YTDL_OPTIONS)

    async def extract_song(self, query_or_url: str, requester: str) -> Optional[Song]:
        """
        Extract audio stream URL and metadata for a single track.
        Can take a YouTube URL or a search query string.
        """
        loop = asyncio.get_event_loop()

        # Run extraction in executor to avoid blocking the asyncio event loop
        # Set default_search to YouTube Music in the class definition.
        def _extract():
            try:
                # Pass the raw query or URL; yt-dlp will use the default_search parameter natively
                search_target = query_or_url
                # Append "official audio" to searches to prevent music videos with dialogue from playing
                if not (search_target.startswith("http://") or search_target.startswith("https://")):
                    if "official audio" not in search_target.lower():
                        search_target = f"{search_target} official audio"
                        
                info = self.ytdl.extract_info(search_target, download=False)
                
                # Handle search result list
                if 'entries' in info:
                    if not info['entries']:
                        return None
                    info = info['entries'][0]

                return info
            except Exception as e:
                logger.error(f"yt-dlp extraction error for '{query_or_url}': {e}")
                return None

        info = await loop.run_in_executor(None, _extract)
        if not info:
            return None

        # Build Song object
        stream_url = info.get('url')
        if not stream_url:
            logger.error(f"No stream URL found in extracted info for {query_or_url}")
            return None

        return Song(
            title=info.get('title', 'Unknown Title'),
            stream_url=stream_url,
            webpage_url=info.get('webpage_url', query_or_url),
            video_id=info.get('id', ''),
            duration=int(info.get('duration') or 0),
            thumbnail=info.get('thumbnail', ''),
            uploader=info.get('uploader', 'Unknown Artist'),
            requester=requester
        )
