"""Request parsing shared by the CLI, mitmproxy addon and offline tests."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

TARGET_HOST = "simp.csuft.edu.cn"
TARGET_PATH = "/api/flySource-base/openApi/getOpenidByJsCode"


def extract_openid(url, text, host=TARGET_HOST, endpoint=TARGET_PATH):
    parsed = urlsplit(url)
    if parsed.hostname != host.lower() or parsed.path != endpoint:
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    value = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    # Reject delimiters that could inject another account or .env setting.
    return None if any(character in value for character in "\r\n,") else value


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def save_env(path, openid):
    """Add an account without overwriting existing accounts or other settings."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    accounts, other_lines = [], []
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "openid":
            accounts.extend(item.strip() for item in value.strip().strip('"\'').split(",") if item.strip())
        else:
            other_lines.append(line)
    accounts.append(openid)
    other_lines.append("openid=" + ",".join(dict.fromkeys(accounts)))
    atomic_write(path, "\n".join(other_lines) + "\n")
