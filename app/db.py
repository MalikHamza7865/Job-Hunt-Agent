from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client

# Always load .env from the project root regardless of CWD
ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def get_client(env: dict | None = None) -> Client:
    """Build and return a Supabase client from environment variables."""
    import os

    url = (env or os.environ).get("SUPABASE_URL", "").strip().rstrip("/")
    key = (env or os.environ).get("SUPABASE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "Missing SUPABASE_URL / SUPABASE_KEY. "
            "Copy .env.example to .env and fill in your credentials."
        )
    return create_client(url, key)