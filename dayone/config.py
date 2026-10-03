"""Load local private configuration without overriding explicit environment settings."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_config(path=None):
    path = Path(path) if path else ROOT / ".env"
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if separator and name.startswith("DAYONE_"):
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            values[name] = value
    return values


def load_config(path=None):
    for name, value in read_config(path).items():
        if value:
            os.environ.setdefault(name, value)
