# Bifrost Music

We built a production-ready Discord music bot. It streams audio directly from Spotify and YouTube using `discord.py`, `spotipy`, and `yt-dlp`. 

## Architecture & Features

### Audio Engine
- We enforce strict FFmpeg stream buffering. 
- `-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5`
- This prevents standard track cutoffs and buffer stalling on slow networks.
- The state engine is isolated per-guild. Cross-talk isn't possible.

### Query Resolution
- `https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M`
- Paste raw Spotify URLs directly into the play command. The bot maps the Spotify metadata to raw YouTube audio streams automatically. We append "official audio" to the backend query to avoid downloading music videos with dialogue.

### Command Execution
- `/autoplay`
- When your queue runs dry, the bot queries YouTube Music for related tracks based on the last played song's artist. It acts as an endless radio.
- The UI uses `discord.ui.View` components. You get direct play, pause, and skip buttons injected right into the now-playing embed.

## Installation

You need Python 3.10+ and system-level FFmpeg installed. 

Clone the repository and install dependencies.
```bash
git clone https://github.com/YOUR_USERNAME/bifrost_music.git
cd bifrost_music
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Set up your environment variables.
```bash
cp .env.example .env
```
Open `.env` and configure your credentials. You need a Discord bot token and Spotify developer API keys. Set `DEV_GUILD_ID` if you want to bypass the 1-hour global slash command sync delay during testing.

Run the bot.
```bash
python main.py
```

## Cloud Deployment (Docker/VPS)

We run this on a standard Linux VPS to avoid local network DDoS vectors. You can use Oracle Cloud or Google Cloud for a free VM tier. 

Install Docker on your server. Clone the repo and configure your `.env` file just like local development.

Build and deploy the container.
```bash
sudo docker build -t bifrost-music .
sudo docker run -d --env-file .env --name bifrost-bot bifrost-music
```
The bot will run in the background. Check the stream state via `sudo docker logs -f bifrost-bot`.

## Known Limitations

Global slash command sync on Discord takes up to an hour. We can't bypass this without defining a specific test server ID. The lyrics scraper relies on the public LRCLIB API. If their service goes down, the `/lyrics` command fails with an error embed.
