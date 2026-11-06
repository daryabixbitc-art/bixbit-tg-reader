"""
Run ONCE on your own computer. Logs in to the reader Telegram account and prints a
session string. Put that string into the GitHub secret TG_SESSION_STRING.
The string gives full access to the account: keep it like a password, never post it in chats.

    pip install telethon
    python3 make_session.py
"""
import getpass

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("api_id: ").strip())
api_hash = getpass.getpass("api_hash (ввод не отображается): ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    # asks for the phone number, the code from Telegram and the 2FA password if it is on
    print("\nГотово. Скопируйте строку ниже целиком в секрет TG_SESSION_STRING:\n")
    print(client.session.save())
    print()
