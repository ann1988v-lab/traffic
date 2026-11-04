"""One-time local setup. Run this file in PyCharm before main.py."""
from getpass import getpass
from pathlib import Path
import re

from cryptography.fernet import Fernet
from dotenv import dotenv_values


def main():
    path = Path(__file__).resolve().parent / ".env"
    old = dotenv_values(path) if path.exists() else {}
    print("Настройка управляющего бота. Секреты сохраняются только в локальный .env.")
    token = getpass("Токен бота от @BotFather (ввод скрыт): ").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ValueError("Токен имеет неподходящий формат")
    owner = input("Твой Telegram ID (или 0, чтобы сначала узнать его через /id): ").strip()
    if not owner.isdigit():
        raise ValueError("ID должен быть числом")
    api_id = input("API ID от my.telegram.org (Enter, если будешь загружать JSON с API): ").strip() or "0"
    api_hash = getpass("API Hash (Enter, если будешь загружать JSON с API): ").strip()
    if not api_id.isdigit() or (api_hash and not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash)):
        raise ValueError("Некорректные API ID/API Hash")
    # Reconfiguration must never silently replace the encryption key.
    key = old.get("ENCRYPTION_KEY") or Fernet.generate_key().decode()
    path.write_text(f"BOT_TOKEN={token}\nOWNER_ID={owner}\n"
                    f"TELEGRAM_API_ID={api_id}\nTELEGRAM_API_HASH={api_hash}\n"
                    f"ENCRYPTION_KEY={key}\nDATA_DIR=./data\n", encoding="utf-8")
    print("Настройки сохранены. Теперь запускай main.py без параметров.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyboardInterrupt) as error:
        print("Настройка не завершена:", str(error))
