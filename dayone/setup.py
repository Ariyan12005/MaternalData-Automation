"""Interactive private Atlas setup: python -m dayone.setup."""
import getpass
import os
import secrets
from .config import ROOT, read_config


def main():
    from cryptography.fernet import Fernet
    path = ROOT / ".env"
    values = read_config(path)
    for name in ("DAYONE_MONGODB_URI", "DAYONE_ENCRYPTION_KEY", "DAYONE_API_TOKEN"):
        if os.environ.get(name):
            values[name] = os.environ[name]
    if not values.get("DAYONE_MONGODB_URI"):
        uri = getpass.getpass("Paste Atlas mongodb+srv connection string (hidden): ").strip()
        if not uri.startswith("mongodb+srv://") or any(marker in uri for marker in ("<", ">", "USER:PASSWORD", "@CLUSTER")):
            print("A real Atlas SRV connection string is required. Nothing saved.")
            return
        values["DAYONE_MONGODB_URI"] = uri
    if not values.get("DAYONE_ENCRYPTION_KEY"):
        key = getpass.getpass("Paste the existing encryption key, or type NEW for an empty database (hidden): ").strip()
        if key == "NEW":
            key = Fernet.generate_key().decode()
        try:
            Fernet(key.encode())
        except (ValueError, TypeError):
            print("A valid existing key or NEW for an empty database is required. Nothing saved.")
            return
        values["DAYONE_ENCRYPTION_KEY"] = key
    values.setdefault("DAYONE_MONGODB_DATABASE", "dayone")
    if not values["DAYONE_MONGODB_DATABASE"]:
        values["DAYONE_MONGODB_DATABASE"] = "dayone"
    if not values.get("DAYONE_API_TOKEN"):
        values["DAYONE_API_TOKEN"] = secrets.token_urlsafe(32)
    values["DAYONE_STORAGE"] = "mongodb"
    values["DAYONE_EXTRACTOR"] = "external"
    if any("\n" in value or "\r" in value for value in values.values()):
        print("Configuration cannot contain line breaks. Nothing saved.")
        return
    import tempfile
    fd, temporary = tempfile.mkstemp(dir=ROOT, prefix=".env-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("# Private local configuration. Keep this file out of Git and back up securely.\n")
            stream.write("\n".join(name + "=" + value for name, value in values.items()) + "\n")
        os.replace(temporary, path)
    finally:
        from pathlib import Path
        Path(temporary).unlink(missing_ok=True)
    print("Saved private .env. Existing keys retained. Run: python -m dayone")
    print("Browser username: dayone. Password: DAYONE_API_TOKEN from your local .env.")
    print("Back up .env securely. Live Atlas connectivity has not yet been tested.")


if __name__ == "__main__":
    main()
