"""Read-only Atlas readiness check; never print credentials or create records."""
import os
from .config import load_config


def main():
    load_config()
    names = ("DAYONE_MONGODB_URI", "DAYONE_ENCRYPTION_KEY", "DAYONE_API_TOKEN")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        print("Atlas not configured. Missing: " + ", ".join(missing))
        print("Run python -m dayone.setup to save your private configuration.")
        return 1
    uri = os.environ["DAYONE_MONGODB_URI"].strip()
    if not uri.startswith("mongodb+srv://"):
        print("DAYONE_MONGODB_URI must be an Atlas mongodb+srv connection string.")
        return 1
    client = None
    try:
        from .security import Cipher
        cipher = Cipher()
        from pymongo import MongoClient
        client = MongoClient(uri, tls=True, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        database = client[os.environ.get("DAYONE_MONGODB_DATABASE") or "dayone"]
        for collection in ("patients", "documents", "visits", "media_blobs"):
            item = database[collection].find_one({})
            if item is not None:
                cipher.decrypt(item["payload"])
        print("Atlas connection and database read access work.")
        print("Existing payloads checked with the configured key. No records written.")
        print("For full write/transaction validation, use the dedicated LiveAtlasTest.")
        return 0
    except ImportError:
        print("Install dependencies: python -m pip install -r requirements.txt")
        return 1
    except Exception as exc:
        # Only the exception type is safe to print: messages may contain credentials.
        print("Atlas check failed (" + type(exc).__name__ + ").")
        print("Check credentials, Atlas IP access, database permissions and the saved encryption key.")
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
