"""Read-only Atlas readiness check; never print credentials or create records."""
import os
from .config import load_config


def main():
    sources = load_config() or {}
    names = ("DAYONE_MONGODB_URI", "DAYONE_ENCRYPTION_KEY", "DAYONE_API_TOKEN")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        print("Atlas not configured. Missing: " + ", ".join(missing))
        print("Run python -m dayone.setup to save your private configuration.")
        return 1
    print("Configuration: " + ", ".join(
        name + " from " + sources.get(name, "environment")
        for name in (*names, "DAYONE_MONGODB_DATABASE")
        if name in os.environ))
    print("Existing environment variables override .env; .env.example is not loaded.")
    uri = os.environ["DAYONE_MONGODB_URI"].strip()
    if not uri.startswith("mongodb+srv://"):
        print("DAYONE_MONGODB_URI must be an Atlas mongodb+srv connection string.")
        return 1
    client = None
    operation = "encryption-key validation"
    try:
        from .security import Cipher
        cipher = Cipher()
        from pymongo import MongoClient
        operation = "MongoClient initialization"
        client = MongoClient(uri, tls=True, serverSelectionTimeoutMS=5000)
        operation = 'admin.command("ping")'
        client.admin.command("ping")
        database = client[os.environ.get("DAYONE_MONGODB_DATABASE") or "dayone"]
        for collection in ("patients", "documents", "visits", "media_blobs"):
            operation = collection + ".find_one({})"
            item = database[collection].find_one({})
            if item is not None:
                operation = collection + " payload decryption"
                cipher.decrypt(item["payload"])
        print("Atlas connection and database read access work.")
        print("Existing payloads checked with the configured key. No records written.")
        print("For full write/transaction validation, use the dedicated LiveAtlasTest.")
        return 0
    except ImportError:
        print("Install dependencies: python -m pip install -r requirements.txt")
        return 1
    except Exception as exc:
        # Never print str(exc), details, URI, username, host, password or key.
        code = getattr(exc, "code", None)
        print("Failed operation: " + operation)
        if type(code) is int:
            print("MongoDB error code: " + str(code))
        explanation = {
            18: "Database-user authentication was rejected. Check database credentials and authSource.",
            13: "The database user lacks permission for this operation.",
        }.get(code, "Connection or configuration check failed; no data was changed.")
        if code == 8000:
            # Atlas uses this code for multiple errors. Inspect privately, never echo.
            message = str(exc).lower()
            explanation = ("Atlas rejected database-user authentication. Check the database user's password, URI encoding and authSource."
                           if any(marker in message for marker in ("bad auth", "authentication failed", "auth failed"))
                           else "Atlas rejected this operation; no data was changed.")
        if type(exc).__name__ == "InvalidToken":
            explanation = "An encrypted payload could not be read with this key. Restore the existing key; do not regenerate it."
        print(explanation)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
