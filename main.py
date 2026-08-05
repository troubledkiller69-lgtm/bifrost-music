import os
import sys
import logging
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
from aiohttp import web

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("BifrostMusic.Main")

# Load environment variables
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")

if not DISCORD_TOKEN:
    logger.critical("FATAL: 'DISCORD_TOKEN' environment variable is missing! Please set it in your .env file.")
    sys.exit(1)

class BifrostMusicBot(commands.Bot):
    """Custom Bot class managing intents, cogs, slash command synchronization, and health check endpoint."""

    def __init__(self):
        # Configure required Intents
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None
        )

    async def _start_healthcheck_server(self):
        """Start a lightweight HTTP health check server for Render free web service hosting."""
        try:
            app = web.Application()
            app.router.add_get("/", lambda r: web.Response(text="Bifrost Music Bot is operational!"))
            app.router.add_get("/health", lambda r: web.Response(text="OK"))
            
            runner = web.AppRunner(app)
            await runner.setup()
            
            port = int(os.getenv("PORT", 8080))
            site = web.TCPSite(runner, "0.0.0.0", port)
            await site.start()
            logger.info(f"Health check HTTP server listening on port {port}.")
        except Exception as e:
            logger.error(f"Failed to start health check server: {e}")

    async def setup_hook(self):
        """Async initialization hook before the bot connects to Discord."""
        # Start health check endpoint for cloud hosters (Render/Koyeb)
        await self._start_healthcheck_server()

        logger.info("Loading extensions...")
        try:
            await self.load_extension("cogs.music")
            logger.info("Extension 'cogs.music' loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to load extension 'cogs.music': {e}")
            raise e

        # Synchronize Slash Commands
        logger.info("Synchronizing Slash Command tree...")
        try:
            if DEV_GUILD_ID:
                guild = discord.Object(id=int(DEV_GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info(f"Synced {len(synced)} slash commands to development guild (ID: {DEV_GUILD_ID}).")
            else:
                synced = await self.tree.sync()
                logger.info(f"Synced {len(synced)} slash commands globally.")
        except Exception as e:
            logger.error(f"Failed to sync slash commands: {e}")

    async def on_ready(self):
        """Event triggered when the bot is connected and ready."""
        logger.info(f"==================================================")
        logger.info(f"  Bot Online: {self.user.name}#{self.user.discriminator} (ID: {self.user.id})")
        logger.info(f"  Connected Guilds: {len(self.guilds)}")
        logger.info(f"==================================================")

        # Set Activity Status
        activity = discord.Activity(
            type=discord.ActivityType.listening,
            name="/play | Bifrost Music"
        )
        await self.change_presence(activity=activity)

def main():
    """Main entrypoint for running Bifrost Music Bot."""
    bot = BifrostMusicBot()
    try:
        bot.run(DISCORD_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        logger.info("Bot execution interrupted by user.")
    except Exception as e:
        logger.critical(f"Bot exited with unhandled error: {e}")

if __name__ == "__main__":
    main()
