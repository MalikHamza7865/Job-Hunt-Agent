#!/usr/bin/env python3
"""One-time interactive login for Indeed (OAuth 2.1 via official GraphQL API).

Indeed's OAuth server has no device-code grant, so the first (and only)
login is interactive in a browser. This script:

  1. starts a tiny local callback server on http://127.0.0.1:19876/callback
     (the redirect URI declared by the pre-whitelisted client metadata doc)
  2. tries to open Indeed's authorization page in your browser
     (if no GUI browser is available - e.g. WSL without a browser - the URL is
     printed and you open it manually; the script keeps waiting either way)
  3. after you sign in, exchanges the code for tokens and saves them to the
     INDEED_TOKEN_FILE (default ./.indeed_tokens.json)
  4. verifies by running a real GraphQL job search

Every later run (including the GitHub Actions cron) refreshes these tokens
silently, so this is a one-time step.

Usage:
    python app/auth_indeed.py [--token-file .indeed_tokens.json] [--force]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.callback_server import start_callback_server, stop_callback_server  # noqa: E402
from app.indeed_client import IndeedClient, REDIRECT_PORT, REDIRECT_URL  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="One-time Indeed OAuth login.")
    parser.add_argument("--token-file", type=Path, default=".indeed_tokens.json")
    parser.add_argument("--force", action="store_true", help="Discard saved tokens first")
    parser.add_argument("--test-query", default="python developer",
                        help="Keyword used to verify the authenticated session")
    parser.add_argument("--test-location", default="",
                        help="Location used to verify the authenticated session")
    args = parser.parse_args()

    if args.force and args.token_file.exists():
        args.token_file.unlink()
        print("Discarded existing tokens.")

    # Bind the callback listener BEFORE anything that might open a browser.
    start_callback_server(REDIRECT_PORT)
    print(f"Callback server listening on {REDIRECT_URL}")

    client = IndeedClient(token_file=args.token_file, interactive=True, timeout=180.0)
    try:
        print("Authenticating with Indeed... (a browser window will open if possible)")
        jobs = client.search(args.test_query, args.test_location, limit=5)
    except Exception as exc:
        print(f"\nAuthentication/search failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        stop_callback_server()

    print(f"\nOK - tokens saved to {args.token_file}")
    print(f"Sample of search(\"{args.test_query}\", \"{args.test_location}\"):")
    for job in jobs:
        print(f"  - {job['title']} @ {job['company']} | {job['location']} | {job['url']}")


if __name__ == "__main__":
    main()