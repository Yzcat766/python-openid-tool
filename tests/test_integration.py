"""Real local mitmproxy tests; never set system proxy or install root certificates."""
import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import capture_openid as cli
from core import TARGET_PATH

try:
    MITMDUMP = cli.locate_mitmdump()
except RuntimeError:
    MITMDUMP = None


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_server_certificate(folder):
    # cryptography is a mitmproxy dependency. Use the installed tool environment.
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    source = '''
import ipaddress, sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
folder = Path(sys.argv[1])
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Local OpenID test')])
now = datetime.now(timezone.utc)
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
    .public_key(key.public_key()).serial_number(x509.random_serial_number())
    .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1))
    .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
    .sign(key, hashes.SHA256()))
(folder/'server.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
(folder/'server.key').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
'''
    subprocess.run([str(python), "-c", source, str(folder)], check=True, capture_output=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = json.dumps({"data": "integration_openid", "extra": "not-saved"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@unittest.skipUnless(MITMDUMP, "先安装 mitmproxy 后运行真实代理测试")
class ProxyIntegrationTests(unittest.TestCase):
    command_prefix = [sys.executable, str(ROOT / "capture_openid.py")]
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)
        self.port = free_port()
        self.state = self.folder / "state"
        self.output = self.folder / "output.json"
        self.process = None
        self.log = (self.folder / "cli.log").open("w", encoding="utf-8")

    def tearDown(self):
        if self.process and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)
        self.log.close()
        state_file = self.state / "restore.json"
        if state_file.exists():
            cli.restore_state(state_file)
        self.directory.cleanup()

    def launch(self, timeout=15, extra=()):
        self.process = subprocess.Popen([*self.command_prefix, "capture", "--manual-proxy", "--skip-cert-install", "--mitmdump", MITMDUMP, "--port", str(self.port), "--timeout", str(timeout), "--state-dir", str(self.state), "--output", str(self.output), "--target-host", "127.0.0.1", *extra], stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if (self.state / "ready").exists():
                return
            if self.process.poll() is not None:
                self.fail((self.folder / "cli.log").read_text(encoding="utf-8"))
            time.sleep(0.1)
        self.fail("test proxy did not start")

    def assert_clean(self):
        self.assertFalse((self.state / "restore.json").exists())
        self.assertFalse((self.state / "capture.json").exists())
        with socket.socket() as sock:
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", self.port)), 0, "proxy still listening after cleanup")

    def test_real_http_proxy_captures_only_target(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            self.launch()
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            connection.request("GET", f"http://127.0.0.1:{server.server_port}/unrelated")
            self.assertEqual(connection.getresponse().read(), b'{"data": "integration_openid", "extra": "not-saved"}')
            self.assertFalse(self.output.exists())
            connection.request("GET", f"http://127.0.0.1:{server.server_port}{TARGET_PATH}?jsCode=do-not-save")
            self.assertEqual(connection.getresponse().status, 200)
            connection.close()
            self.assertEqual(self.process.wait(timeout=15), 0, (self.folder / "cli.log").read_text(encoding="utf-8"))
            record = json.loads(self.output.read_text(encoding="utf-8"))
            self.assertEqual(record["openid"], "integration_openid")
            self.assertNotIn("do-not-save", self.output.read_text(encoding="utf-8"))
            self.assertNotIn("extra", record)
            self.assertEqual(self.output.with_suffix(".txt").read_text(encoding="utf-8"), "integration_openid\n")
            self.assert_clean()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

    def test_timeout_stops_proxy(self):
        self.launch(timeout=0.5)
        self.assertEqual(self.process.wait(timeout=10), 1)
        self.assertFalse(self.output.exists())
        self.assert_clean()

    @unittest.skipUnless(os.name == "nt", "Windows process recovery")
    def test_recovery_stops_orphan_after_forced_parent_exit(self):
        self.launch(timeout=60)
        self.process.kill()
        self.process.wait(timeout=10)
        self.assertTrue((self.state / "restore.json").exists())
        restore = subprocess.run([sys.executable, str(ROOT / "capture_openid.py"), "restore", "--state-dir", str(self.state)], capture_output=True, timeout=15)
        self.assertEqual(restore.returncode, 0, restore.stderr.decode("utf-8"))
        self.assert_clean()

    def test_real_https_proxy_with_ca_verification(self):
        make_server_certificate(self.folder)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.folder / "server.pem"), str(self.folder / "server.key"))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            self.launch(extra=("--upstream-ca", str(self.folder / "server.pem")))
            # Trust the proxy CA on this test connection only, never in the OS store.
            client_context = ssl.create_default_context(cafile=str(self.state / "ca" / "mitmproxy-ca-cert.pem"))
            connection = http.client.HTTPSConnection("127.0.0.1", self.port, context=client_context, timeout=10)
            connection.set_tunnel("127.0.0.1", server.server_port)
            connection.request("GET", f"{TARGET_PATH}?jsCode=local-test-code")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
            self.assertEqual(self.process.wait(timeout=15), 0, (self.folder / "cli.log").read_text(encoding="utf-8"))
            self.assertEqual(json.loads(self.output.read_text(encoding="utf-8"))["openid"], "integration_openid")
            self.assert_clean()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
