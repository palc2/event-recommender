"""Capture a Google OAuth refresh token for the delivery agent.

This is a one-time setup. After running it once, the GOOGLE_REFRESH_TOKEN in
your .env will be valid for as long as you don't revoke access — the delivery
agent uses it to mint short-lived access tokens automatically.

What you need to do *before* running this script
------------------------------------------------
1. Go to https://console.cloud.google.com
2. Create a project (or select an existing one).
3. APIs & Services → Library → search "Google Calendar API" → Enable.
4. APIs & Services → OAuth consent screen:
   - User Type: **External**
   - App name: anything (e.g. "Event Recommender")
   - User support email + Developer email: your own
   - On the "Test users" page, **add the Google account whose calendar you
     want invites written to** (e.g. you@gmail.com). Without this,
     consent will fail with "Access blocked".
5. APIs & Services → Credentials → Create Credentials → OAuth client ID:
   - Application type: **Desktop app**
   - Name: anything
6. Copy the Client ID and Client Secret from the dialog (or download the
   JSON — you only need those two fields).
7. Put them in .env as GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET, OR pass them
   to this script with --client-id / --client-secret.

Then run:  uv run oauth-setup
A browser tab opens. Sign in as the calendar owner. The script writes the
refresh_token (plus the client id / secret if not already there) to .env.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from event_scheduler.config import settings

# Read/write the user's primary calendar. If you want secondary calendars
# too, swap to https://www.googleapis.com/auth/calendar.
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def run_consent_flow(client_id: str, client_secret: str) -> str:
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    # access_type=offline + prompt=consent forces Google to issue a
    # refresh_token even if this account previously consented.
    creds = flow.run_local_server(
        port=0,
        prompt="consent",
        access_type="offline",
        authorization_prompt_message=(
            "\nOpening your browser to grant calendar access. Sign in as the "
            "account whose calendar should receive invites…\n"
        ),
        success_message=(
            "You're good — refresh token captured. You can close this tab and "
            "return to the terminal."
        ),
    )
    if not creds.refresh_token:
        raise RuntimeError(
            "Google did not return a refresh_token. Common causes:\n"
            "  • You've already granted offline access to this app before — "
            "revoke at https://myaccount.google.com/permissions and retry.\n"
            "  • You denied 'offline access' on the consent screen — accept all scopes."
        )
    return creds.refresh_token


def update_env_file(env_path: Path, updates: dict[str, str]) -> None:
    """Rewrite .env in place: replace existing keys, append missing ones,
    preserve unrelated lines and ordering."""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.lstrip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture a Google OAuth refresh token for the delivery agent.",
    )
    parser.add_argument(
        "--client-id",
        default=settings.google_client_id or None,
        help="GOOGLE_CLIENT_ID (defaults to whatever's in .env)",
    )
    parser.add_argument(
        "--client-secret",
        default=settings.google_client_secret or None,
        help="GOOGLE_CLIENT_SECRET (defaults to whatever's in .env)",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="Path to .env to update (default: ./.env in cwd)",
    )
    args = parser.parse_args()

    client_id = args.client_id
    client_secret = args.client_secret
    if not client_id:
        client_id = input("Paste GOOGLE_CLIENT_ID: ").strip()
    if not client_secret:
        client_secret = input("Paste GOOGLE_CLIENT_SECRET: ").strip()
    if not client_id or not client_secret:
        print("ERROR: both client_id and client_secret are required.", file=sys.stderr)
        return 2

    refresh_token = run_consent_flow(client_id, client_secret)
    print(f"\nGot refresh_token (first 12 chars): {refresh_token[:12]}…")

    env_path = Path(args.env).resolve()
    update_env_file(
        env_path,
        {
            "GOOGLE_CLIENT_ID": client_id,
            "GOOGLE_CLIENT_SECRET": client_secret,
            "GOOGLE_REFRESH_TOKEN": refresh_token,
        },
    )
    print(f"Wrote GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN to {env_path}")
    print("\nDone. Restart event-web (or event-scheduler) — delivery will now create calendar invites.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
