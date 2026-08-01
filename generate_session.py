"""
Run this script ONCE to generate your Pyrogram user session string.

Usage:
    python generate_session.py

It will ask for your phone number, send a Telegram login code,
then print the SESSION_STRING to paste into your .env file.

Requirements: same API_ID / API_HASH as the bot.
"""

import asyncio
from pyrogram import Client
from config import API_ID, API_HASH


async def main():
    print("=" * 60)
    print("  AniDL Premium Session String Generator")
    print("=" * 60)
    print()
    print("This will log in to your PERSONAL Telegram account (not the bot).")
    print("The account must have Telegram Premium for >2 GB uploads.")
    print()

    async with Client(":memory:", api_id=API_ID, api_hash=API_HASH) as client:
        session_string = await client.export_session_string()

    print()
    print("=" * 60)
    print("  ✅ Session string generated successfully!")
    print("=" * 60)
    print()
    print("Add the following line to your .env file:")
    print()
    print(f"USER_SESSION_STRING={session_string}")
    print()
    print("Keep this string SECRET – it gives full access to your account.")


if __name__ == "__main__":
    asyncio.run(main())
