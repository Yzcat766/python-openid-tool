"""mitmdump -s addon.py; all configuration is passed by the CLI via environment."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import atomic_write, extract_openid


class OpenIdCapture:
    def __init__(self):
        self.saved = False

    def running(self):
        atomic_write(os.environ["OPENID_READY_FILE"], "ready\n")

    def response(self, flow):
        if self.saved or flow.response is None or not 200 <= flow.response.status_code < 300:
            return
        try:
            openid = extract_openid(
                flow.request.pretty_url,
                flow.response.get_text(strict=False),
                os.environ["OPENID_TARGET_HOST"],
                os.environ["OPENID_TARGET_PATH"],
            )
        except (ValueError, UnicodeError):
            return
        if not openid:
            return
        parsed = urlsplit(flow.request.pretty_url)
        # Do not save jsCode, full responses, headers or unrelated requests.
        record = {
            "openid": openid,
            "endpoint": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write(os.environ["OPENID_RESULT_FILE"], json.dumps(record, ensure_ascii=False) + "\n")
        self.saved = True


addons = [OpenIdCapture()]
