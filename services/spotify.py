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
            try:
                while results.get("next"):
                    results = self.sp.next(results)
                    tracks.extend(results["items"])
            except Exception as e:
                logger.warning(f"Could not fetch full album (Spotify API restricted pagination): {e}")

            for item in tracks:
                artists = ", ".join([artist["name"] for artist in item["artists"]])
                track_name = item["name"]
                queries.append(f"{artists} - {track_name}")

            return queries
        except Exception as e:
            logger.error(f"Error fetching Spotify album {album_id}: {e}")
            raise ValueError("Could not retrieve Spotify album information.")

    def _get_playlist_queries(self, playlist_id: str) -> List[str]:
        """Fetch tracks in a playlist by scraping the public Spotify embed widget.
        
        The official Spotify API (GET /playlists/{id}) now permanently returns an empty 
        'items' array for Client Credentials tokens. Scraping the embed page bypasses this
        and successfully retrieves up to 100 tracks without requiring user OAuth login.
        """
        import urllib.request
        import re
        import json
        
        queries = []
        try:
            url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
            # A full, real user-agent is REQUIRED to prevent Spotify from instantly blocking the request.
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'
            }
            req = urllib.request.Request(url, headers=headers)
            
            with urllib.request.urlopen(req, timeout=10) as response:
                html = response.read().decode('utf-8')
            
            # Extract the raw JSON state from the embed page
            match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
            if not match:
                logger.warning(f"Could not find JSON data in Spotify embed for playlist {playlist_id}.")
                return queries
                
            data = json.loads(match.group(1))
            tracks = data.get('props', {}).get('pageProps', {}).get('state', {}).get('data', {}).get('entity', {}).get('trackList', [])
            
            for track in tracks:
                artist = track.get('subtitle', '').strip()
                title = track.get('title', '').strip()
                if artist and title:
                    queries.append(f"{artist} - {title}")
                
            if not queries:
                logger.warning(f"No tracks found in playlist embed for {playlist_id}. The playlist may be empty or private.")
                
            return queries
        except Exception as e:
            logger.error(f"Error scraping Spotify playlist {playlist_id}: {e}")
            raise ValueError("Could not retrieve Spotify playlist information. Make sure the playlist is public.")

    def get_recommendations(self, song_title: str, artist_name: str, history: List[str] = None, limit: int = 5) -> List[str]:
        """
        Find related tracks using Spotify's Related Artists + Top Tracks endpoints.
        The /recommendations endpoint was deprecated, so we use:
          1. Search for the seed track
          2. Get the artist's related artists
          3. Pull top tracks from random related artists
          4. Filter out already-played tracks
        """
        if not self.sp:
            logger.warning("Spotify client not initialized, can't get recommendations")
            return []

        import re as _re
        import random

        # Clean YouTube junk from title and artist
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
            """If the YouTube title is 'Artist - Song', extract the artist."""
            if ' - ' in t:
                return t.split(' - ', 1)[0].strip()
            return None

        raw_title = song_title
        clean_title = _clean_title(song_title)
        clean_artist = _clean_artist(artist_name)

        # YouTube often puts the real artist in the title like "Alicia Keys - If I Ain't Got You"
        title_artist = _extract_artist_from_title(clean_title)
        if title_artist:
            # Use the artist from the title, and the song name after the dash
            search_artist = title_artist
            search_song = clean_title.split(' - ', 1)[1].strip()
        else:
            search_artist = clean_artist
            search_song = clean_title

        logger.info(f"Autoplay: cleaned artist='{search_artist}', song='{search_song}' (raw: '{raw_title}', '{artist_name}')")

        try:
            # Step 1: Search Spotify for the seed track
            search_query = f"track:{search_song} artist:{search_artist}"
            logger.info(f"Autoplay: Spotify search: '{search_query}'")
            results = self.sp.search(q=search_query, type="track", limit=1)
            tracks = results.get("tracks", {}).get("items", [])

            if not tracks:
                fallback_query = f"{search_artist} {search_song}"
                logger.info(f"Autoplay: strict search failed, trying: '{fallback_query}'")
                results = self.sp.search(q=fallback_query, type="track", limit=1)
                tracks = results.get("tracks", {}).get("items", [])

            if not tracks:
                logger.warning(f"Autoplay: Spotify found no seed track for: '{search_song}' by '{search_artist}'")
                return []

            seed_track = tracks[0]
            seed_artist_id = seed_track["artists"][0]["id"]
            seed_artist_name = seed_track["artists"][0]["name"]
            logger.info(f"Autoplay: seed found: '{seed_track['name']}' by '{seed_artist_name}'")

            # Step 2: Get related artists
            related = self.sp.artist_related_artists(seed_artist_id)
            related_artists = related.get("artists", [])

            if not related_artists:
                logger.warning(f"Autoplay: no related artists found for '{seed_artist_name}'")
                return []

            # Shuffle and pick a few related artists for variety
            random.shuffle(related_artists)
            selected_artists = related_artists[:3]
            logger.info(f"Autoplay: selected related artists: {[a['name'] for a in selected_artists]}")

            # Step 3: Get top tracks from each related artist
            recommendations = []
            history_lower = set((h.lower() for h in history)) if history else set()

            for artist in selected_artists:
                try:
                    top = self.sp.artist_top_tracks(artist["id"], country="US")
                    top_tracks = top.get("tracks", [])
                    random.shuffle(top_tracks)

                    for track in top_tracks:
                        artists_str = ", ".join([a["name"] for a in track["artists"]])
                        track_name = track["name"]
                        query = f"{artists_str} - {track_name}"

                        if query.lower() in history_lower or track_name.lower() in history_lower:
                            continue

                        recommendations.append(query)
                        if len(recommendations) >= limit:
                            break
                except Exception as e:
                    logger.warning(f"Autoplay: failed to get top tracks for '{artist['name']}': {e}")
                    continue

                if len(recommendations) >= limit:
                    break

            logger.info(f"Autoplay: {len(recommendations)} recommendations: {recommendations}")
            return recommendations

        except Exception as e:
            logger.error(f"Autoplay: Spotify error: {e}", exc_info=True)
            return []


