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
        'format': 'bestaudio/best',
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
        def _extract():
            try:
                # If not a URL, use ytsearch
                search_target = query_or_url
                if not (query_or_url.startswith("http://") or query_or_url.startswith("https://")):
                    search_target = f"ytsearch:{query_or_url}"

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
            duration=int(info.get('duration') or 0),
            thumbnail=info.get('thumbnail', ''),
            uploader=info.get('uploader', 'Unknown Artist'),
            requester=requester
        )
