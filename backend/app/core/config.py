"""
App configuration. DATABASE_URL must come from the environment (or the
project's secret manager, injected as an env var at deploy time) --
never hardcode credentials in source.

For local development, values are loaded from a gitignored .env file
(see .env.example) via python-dotenv. In production, the real
environment should already have these set by the deployment platform,
so load_dotenv() there is a harmless no-op if no .env file exists.
"""

import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    database_url: str = os.environ["DATABASE_URL"]  # raises loudly if unset
    db_pool_min_size: int = int(os.environ.get("DB_POOL_MIN_SIZE", "1"))
    db_pool_max_size: int = int(os.environ.get("DB_POOL_MAX_SIZE", "10"))


settings = Settings()
