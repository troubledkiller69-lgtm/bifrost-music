# 🎧 Bifrost Music - Discord Music Bot

A production-ready, feature-rich Discord Music Bot built with **Python 3.11+**, `discord.py` (with Slash Commands via `app_commands`), `spotipy`, `yt-dlp`, `PyNaCl`, and `FFmpeg`.

Designed for modularity, per-guild queue management, Spotify track/album/playlist resolution, and seamless **zero-cost cloud deployment** (Render / Koyeb).

---

## ✨ Features

- 🟢 **Spotify Web API Integration**: Directly paste Spotify URLs for **Tracks**, **Albums**, and **Playlists** (`open.spotify.com/track/...`, `/album/...`, `/playlist/...`). Automatically resolves tracks into search queries.
- ▶️ **YouTube & Direct Search Fallback**: Paste YouTube URLs or plain text keywords directly into the `/play` command.
- 💬 **Discord Slash Commands (`app_commands`)**: Full interaction-based slash command suite with deferral handling for fast, non-blocking UI responses.
- 🏰 **Per-Guild State Engine**: Isolated queues, current track tracking, and audio playback per server to prevent cross-talk.
- 📡 **Stutter-Free Audio Streaming**: Customized FFmpeg flags (`-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5`) to prevent buffer stalling and early track cutoffs.
- 🐳 **Containerized & Cloud Ready**: Pre-configured `Dockerfile`, `render.yaml`, and `Procfile` for 24/7 deployment on Render or Koyeb free tiers.

---

## 📁 Project Structure

```text
bifrost_music/
├── cogs/
│   └── music.py          # Discord Cog handling voice connections, queue state, and slash commands
├── services/
│   ├── spotify.py        # Spotipy wrapper parsing Spotify track, album, and playlist URLs
│   └── youtube.py        # yt-dlp audio stream extraction & song metadata dataclass
├── .env.example          # Environment variable template
├── Dockerfile            # Multi-stage Docker container setup with system FFmpeg
├── main.py               # Bot entrypoint, client setup, and command tree sync
├── Procfile              # Process manifest for Koyeb/Heroku/Railway deployment
├── render.yaml           # Render Blueprint manifest for Background Worker deployment
├── requirements.txt      # Python dependencies
└── README.md             # Project documentation & deployment guide
```

---

## 🔑 Prerequisite Setup & Credentials

### 1. Spotify Developer Credentials
1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and log in.
2. Click **Create an App**.
3. Fill in **App Name** (e.g., `Bifrost Music`) and **App Description**.
4. Set the Redirect URI to `http://localhost:8888/callback` (not strictly needed for Client Credentials, but required by Spotify).
5. Open your newly created app settings and copy the **Client ID** and **Client Secret**.

### 2. Discord Bot Token & Application Setup
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application**, name it `Bifrost Music`, and click **Create**.
3. Go to the **Bot** tab on the left menu:
   - Click **Reset Token** or **Add Bot** and copy your **Bot Token**.
   - Enable **Message Content Intent** under **Privileged Gateway Intents**.
4. Go to **OAuth2 -> URL Generator**:
   - Select **Scopes**: `bot`, `applications.commands`.
   - Select **Bot Permissions**: `Connect`, `Speak`, `Send Messages`, `Embed Links`, `Use Slash Commands`.
   - Copy the generated URL and paste it into your browser to invite **Bifrost Music** to your server.

---

## 🛠️ Local Development & Testing

### System Requirements
- **Python**: 3.10 or higher
- **FFmpeg**: Must be installed on your operating system and added to your system `PATH`.
  - *Windows*: Download from [Gyan.dev](https://www.gyan.dev/ffmpeg/builds/) or install via `winget install ffmpeg`.
  - *macOS*: `brew install ffmpeg`
  - *Linux*: `sudo apt install ffmpeg`

### Step-by-Step Local Run
1. Clone or download this repository.
2. Navigate to the project directory:
   ```bash
   cd bifrost_music
   ```
3. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   # On Windows:
   venv\Scripts\activate
   # On macOS/Linux:
   source venv/bin/activate
   ```
4. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
5. Create your `.env` configuration file:
   ```bash
   cp .env.example .env
   ```
6. Open `.env` in a text editor and fill in your credentials:
   ```env
   DISCORD_TOKEN=your_discord_bot_token_here
   SPOTIFY_CLIENT_ID=your_spotify_client_id_here
   SPOTIFY_CLIENT_SECRET=your_spotify_client_secret_here
   DEV_GUILD_ID=your_optional_test_server_id
   ```
   *(Note: Setting `DEV_GUILD_ID` allows instant slash command registration on your test server without waiting for global Discord caching).*
7. Run the bot:
   ```bash
   python main.py
   ```

---

## ☁️ Zero-Cost Cloud Deployment (Not Self-Hosted)

### Option A: Deploying to Render (Recommended Free Worker)
Render allows running 1 free Background Worker container.

1. Push your `bifrost_music` code repository to **GitHub**.
2. Log in to [Render.com](https://render.com/).
3. Click **New +** -> **Blueprint**.
4. Connect your GitHub repository. Render will automatically detect `render.yaml`.
5. Under **Environment Variables**, provide values for:
   - `DISCORD_TOKEN`
   - `SPOTIFY_CLIENT_ID`
   - `SPOTIFY_CLIENT_SECRET`
6. Click **Apply**. Render will automatically build the `Dockerfile` with system `ffmpeg` installed and launch **Bifrost Music**.

---

### Option B: Deploying to Koyeb (Alternative Free Container Host)
Koyeb provides free micro-instance containers.

1. Log in to [Koyeb.com](https://www.koyeb.com/).
2. Click **Create Service**.
3. Select **GitHub** as the deployment method and choose your `bifrost_music` repository.
4. Set the Builder type to **Docker** (it will automatically use the `Dockerfile`).
5. Under **Environment Variables**, add:
   - `DISCORD_TOKEN`
   - `SPOTIFY_CLIENT_ID`
   - `SPOTIFY_CLIENT_SECRET`
6. Click **Deploy**.

---

### Option C: Deploying to a Linux VPS (Ubuntu/Debian)
If you purchased a traditional VPS (like DigitalOcean, Hetzner, or AWS EC2), the easiest and cleanest way to run the bot 24/7 is using Docker.

1. SSH into your VPS and install Git and Docker:
   ```bash
   sudo apt update
   sudo apt install git docker.io -y
   ```
2. Clone your repository:
   ```bash
   git clone https://github.com/YOUR_USERNAME/bifrost_music.git
   cd bifrost_music
   ```
3. Create your `.env` file and add your tokens (Docker will read from this):
   ```bash
   nano .env
   # Add your DISCORD_TOKEN, SPOTIFY_CLIENT_ID, etc. here, then save (Ctrl+O, Enter, Ctrl+X)
   ```
4. Build and run the Docker container in the background (detached mode):
   ```bash
   sudo docker build -t bifrost-music .
   sudo docker run -d --env-file .env --name bifrost-bot bifrost-music
   ```
   *(To view the live console logs later, run: `sudo docker logs -f bifrost-bot`)*

---

## 🎮 Slash Command Reference

| Command | Arguments | Description |
|---|---|---|
| `/play` | `query`: Spotify URL, YouTube URL, or Search Terms | Joins your voice channel, parses Spotify links or search keywords, and starts or queues playback. |
| `/pause` | None | Pauses active audio playback. |
| `/resume` | None | Resumes paused audio playback. |
| `/skip` | None | Skips the currently playing track and starts the next in queue. |
| `/queue` | None | Displays an embed showing the currently playing track and upcoming queue. |
| `/nowplaying` | None | Shows detailed information (duration, uploader, requester) for the current song. |
| `/stop` | None | Stops playback, clears the server queue, and disconnects from voice. |
| `/leave` | None | Alias for `/stop`. |

---

## 🛡️ License & Troubleshooting

- **Voice Connection Errors (`PyNaCl missing`)**: Ensure `PyNaCl` is installed (`pip install PyNaCl`).
- **No Sound / FFmpeg Executable Not Found**: Verify `ffmpeg -version` works in your terminal shell. In Docker, `apt-get install -y ffmpeg` handles this automatically.
- **Slash Commands Not Showing Up**: Global slash command sync can take up to 1 hour on Discord. Set `DEV_GUILD_ID` in `.env` during development for instant registration.
