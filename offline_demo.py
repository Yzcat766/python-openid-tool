"""Exercise the real capture engine/addon with simulated OS and proxy interfaces.

No network, registry or trust-store access. State always lives in an isolated
demo directory. This backend is selected only by an explicit --demo flag.
"""
import json
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import addon
import capture_openid as engine
from core import TARGET_HOST, TARGET_PATH, atomic_write


class OfflineBackend:
    def __init__(self, outcome="success", delay=3, startup_delay=0):
        self.outcome = outcome
        self.delay = delay
        self.startup_delay = startup_delay
        self.actions = []
        self.trusted = False

    def certutil(self, *args):
        self.actions.append(("certificate", args))
        self.trusted = args[0] == "-addstore"

    def _patches(self):
        stack = ExitStack()
        original = {"ProxyEnable": {"value": 0, "kind": 4}}
        replacements = {
            "require_windows": lambda: None,
            "locate_mitmdump": lambda explicit=None: "offline-mitmdump.exe",
            "port_available": lambda port: None,
            "read_proxy": lambda: original,
            "write_proxy": lambda value: self.actions.append(("proxy", value)),
            "cert_thumbprint": lambda path: "OFFLINE-CA",
            "cert_is_trusted": lambda value: self.trusted,
            "certutil": self.certutil,
            "process_identity": lambda pid: None,
            "stop_saved_process": lambda identity: self.actions.append(("recover_process", identity)),
        }
        for name, replacement in replacements.items():
            stack.enter_context(patch.object(engine, name, replacement))
        return stack

    def capture(self, args, callback, cancel_event):
        backend = self
        class SimulatedProxy:
            pid = 0
            returncode = None
            def __init__(self, command, env, **kwargs):
                self.env = env
                self.started = time.monotonic()
                self.sent = False
                self.ready = False
                if not backend.startup_delay:
                    self.make_ready()
            def make_ready(self):
                self.ready = True
                state = Path(self.env["OPENID_READY_FILE"]).parent
                atomic_write(state / "ca" / "mitmproxy-ca-cert.pem", "SIMULATED PUBLIC CERTIFICATE")
                atomic_write(Path(self.env["OPENID_READY_FILE"]), "ready")
            def poll(self):
                elapsed = time.monotonic() - self.started
                if not self.ready and elapsed >= backend.startup_delay:
                    self.make_ready()
                if not self.sent and elapsed >= backend.startup_delay + backend.delay:
                    self.sent = True
                    if backend.outcome == "success":
                        # Same parsing hook as real mitmproxy, stripped query and response.
                        flow = SimpleNamespace(request=SimpleNamespace(pretty_url=f"https://{TARGET_HOST}{TARGET_PATH}?jsCode=offline-secret"), response=SimpleNamespace(status_code=200, get_text=lambda strict: json.dumps({"data": "offline_demo_openid", "private": "do-not-log"})))
                        with patch.dict("os.environ", self.env):
                            addon.OpenIdCapture().response(flow)
                    elif backend.outcome == "error":
                        self.returncode = 7
                return self.returncode
            def terminate(self):
                backend.actions.append(("stop_process", None))
                self.returncode = 0
            kill = terminate
            def wait(self, timeout=None):
                return self.returncode
        with self._patches(), patch.object(engine.subprocess, "Popen", SimulatedProxy):
            return engine.capture(args, callback, cancel_event)

    def restore_state(self, path, callback=None):
        with self._patches():
            return engine.restore_state(path, callback)

    def doctor(self, args, callback):
        engine.emit(callback, "checking", "离线模拟环境可用；不连接微信，不修改真实代理或证书库。")

    def install(self, args, callback, cancel_event):
        for step in range(5):
            engine.pause(cancel_event, 0.15)
            engine.emit(callback, "installing", f"离线模拟安装进度：{(step + 1) * 20}%")
