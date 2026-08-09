import os
import sys
import pathlib

# Must be set before `main`/`config` are ever imported by any test module -
# pydantic-settings reads these at import time, and importing main.py pulls
# in the whole app (Discord signature verification, Supabase client setup,
# etc). Placeholder values only - nothing here talks to a real service.
os.environ.setdefault("SUPABASE_URL", "https://ci-placeholder.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "ci-placeholder")
os.environ.setdefault("DISCORD_BOT_TOKEN", "ci-placeholder")
os.environ.setdefault("DISCORD_PUBLIC_KEY", "ci-placeholder")
os.environ.setdefault("DISCORD_CLIENT_ID", "ci-placeholder")
os.environ.setdefault("DISCORD_GUILD_ID", "ci-placeholder")
os.environ.setdefault("CRON_SECRET", "ci-placeholder")
os.environ.setdefault("NFT_CRON_SECRET", "ci-placeholder")

# So `import main` / `import config` / `import pnl_card` work regardless of
# which directory pytest is invoked from.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
