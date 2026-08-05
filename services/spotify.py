import os
import re
import logging
from typing import List, Optional
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOauthError

logger = logging.getLogger("BifrostMusic.Spotify")

class SpotifyService:
    """Service to handle Spotify API interactions and convert Spotify URLs to search queries."""

    # Regex patterns for Spotify URLs
    TRACK_PATTERN = re.compile(r"open\.spotify\.com/(?:.*/)?track/([a-zA-Z0-9]+)|spotify:track:([a-zA-Z0-9]+)")
    ALBUM_PATTERN = re.compile(r"open\.spotify\.com/(?:.*/)?album/([a-zA-Z0-9]+)|spotify:album:([a-zA-Z0-9]+)")
    PLAYLIST_PATTERN = re.compile(r"open\.spotify\.com/(?:.*/)?playlist/([a-zA-Z0-9]+)|spotify:playlist:([a-zA-Z0-9]+)")

    def __init__(self, client_id: Optional[str] = None, client_secret: Optional[str] = None):
        self.client_id = client_id or os.getenv("SPOTIFY_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("SPOTIFY_CLIENT_SECRET")
        self.sp: Optional[spotipy.Spotify] = None
        self._initialize_client()

    def _initialize_client(self) -> None:
        """Initialize Spotipy client with credentials if provided."""
        if self.client_id and self.client_secret:
            try:
                auth_manager = SpotifyClientCredentials(
                    client_id=self.client_id,
                    client_secret=self.client_secret
                )
                self.sp = spotipy.Spotify(auth_manager=auth_manager)
                logger.info("Spotify API service initialized successfully.")
            except Exception as e:
                logger.error(f"Failed to initialize Spotify client: {e}")
                self.sp = None
        else:
            logger.warning("Spotify Client ID or Client Secret missing. Spotify links will not be supported.")

    @classmethod
    def is_spotify_url(cls, url: str) -> bool:
        """Check if a given string matches any Spotify URL pattern."""
        if not isinstance(url, str):
            return False
        return bool(
            cls.TRACK_PATTERN.search(url) or
            cls.ALBUM_PATTERN.search(url) or
            cls.PLAYLIST_PATTERN.search(url)
        )

    def parse_url(self, url: str) -> List[str]:
        """
        Extract search queries (Artist - Track Title) from a Spotify URL.
        Returns a list of search query strings.
        """
        if not self.sp:
            raise ValueError("Spotify service is not configured. Please provide SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.")

        # Check Track
        track_match = self.TRACK_PATTERN.search(url)
        if track_match:
            track_id = track_match.group(1) or track_match.group(2)
            return [self._get_track_query(track_id)]

        # Check Album
        album_match = self.ALBUM_PATTERN.search(url)
        if album_match:
            album_id = album_match.group(1) or album_match.group(2)
            return self._get_album_queries(album_id)

        # Check Playlist
        playlist_match = self.PLAYLIST_PATTERN.search(url)
        if playlist_match:
            playlist_id = playlist_match.group(1) or playlist_match.group(2)
            return self._get_playlist_queries(playlist_id)

        raise ValueError("Invalid or unsupported Spotify URL pattern.")

    def _get_track_query(self, track_id: str) -> str:
        """Fetch track details and format as 'Artist - Title'."""
        try:
            track = self.sp.track(track_id)
            artists = ", ".join([artist["name"] for artist in track["artists"]])
            track_name = track["name"]
            return f"{artists} - {track_name}"
        except SpotifyOauthError as e:
            logger.error(f"Spotify authentication error: {e}")
            raise ValueError("Spotify API authentication failed.")
        except Exception as e:
            logger.error(f"Error fetching Spotify track {track_id}: {e}")
            raise ValueError(f"Could not retrieve Spotify track information.")

    def _get_album_queries(self, album_id: str) -> List[str]:
        """Fetch all tracks in an album and format each as 'Artist - Title'."""
        queries = []
        try:
            results = self.sp.album_tracks(album_id)
            tracks = results["items"]
            
            # Pagination handling
            while results.get("next"):
                results = self.sp.next(results)
                tracks.extend(results["items"])

            for item in tracks:
                artists = ", ".join([artist["name"] for artist in item["artists"]])
                track_name = item["name"]
                queries.append(f"{artists} - {track_name}")

            return queries
        except Exception as e:
            logger.error(f"Error fetching Spotify album {album_id}: {e}")
            raise ValueError("Could not retrieve Spotify album information.")

    def _get_playlist_queries(self, playlist_id: str) -> List[str]:
        """Fetch all tracks in a playlist and format each as 'Artist - Title'."""
        queries = []
        try:
            results = self.sp.playlist_items(playlist_id, fields="items.track(name,artists),next")
            tracks = results["items"]

            # Pagination handling
            while results.get("next"):
                results = self.sp.next(results)
                tracks.extend(results["items"])

            for item in tracks:
                track = item.get("track")
                if not track:
                    continue
                artists = ", ".join([artist["name"] for artist in track["artists"]])
                track_name = track["name"]
                queries.append(f"{artists} - {track_name}")

            return queries
        except Exception as e:
            logger.error(f"Error fetching Spotify playlist {playlist_id}: {e}")
            raise ValueError("Could not retrieve Spotify playlist information.")
