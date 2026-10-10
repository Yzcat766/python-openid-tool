"""Verify zip privacy/hashes and run EXEs with no Python available on PATH."""
import hashlib
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    output = ROOT / "verification"
    output.mkdir(exist_ok=True)
    releases = ROOT / "releases"
    checks = []
    forbidden = {".state", ".demo-state", ".venv", "output", "demo-output", "verification", "__pycache__"}
    for name in ("python-openid-tool-source.zip", "python-openid-tool-windows-x64.zip"):
        with zipfile.ZipFile(releases / name) as archive:
            for item in archive.namelist():
                path = Path(item)
                assert not forbidden.intersection(path.parts), item
                assert path.name not in ("openid.json", "openid.txt", "restore.json", "capture.json"), item
                if path.suffix.lower() in (".pem", ".key", ".pfx", ".p12"):
                    data = archive.read(item)
                    assert b"PRIVATE KEY-----" not in data, item
        checks.append(name + ": no capture data, recovery records or private keys")
    for line in (releases / "SHA256SUMS.txt").read_text().splitlines():
        digest, filename = line.split("  ", 1)
        assert hashlib.sha256((releases / filename).read_bytes()).hexdigest() == digest
    checks.append("SHA256SUMS verified")
    env = os.environ.copy()
    env["PATH"] = str(Path(env["SYSTEMROOT"]) / "System32")
    for name in ("PYTHONHOME", "PYTHONPATH", "TCL_LIBRARY", "TK_LIBRARY", "CONDA_PREFIX"):
        env.pop(name, None)
    def verify(executable, label):
        directory = output / label
        report = directory / "verification-report.json"
        report.unlink(missing_ok=True)
        subprocess.run([str(executable), "--demo", "--verify-demo", "--demo-dir", str(directory)], env=env, check=True, timeout=40, capture_output=True)
        result = json.loads(report.read_text(encoding="utf-8"))
        assert result["passed"], result
        checks.append(label + ": actual GUI success/timeout/error/close passed without Python on PATH")
    verify(releases / "OpenIDTool" / "OpenIDTool.exe", "exe-demo")
    with tempfile.TemporaryDirectory(dir=output, prefix="portable-中文路径-") as temporary:
        folder = Path(temporary).resolve()
        assert folder.is_relative_to(output.resolve())
        with zipfile.ZipFile(releases / "python-openid-tool-windows-x64.zip") as archive:
            archive.extractall(folder)
        verify(folder / "OpenIDTool" / "OpenIDTool.exe", "relocated-demo")
        result = subprocess.run([str(folder / "OpenIDTool" / "runtime" / "mitmdump" / "mitmdump.exe"), "--version"], env=env, check=True, capture_output=True, text=True, timeout=15)
        assert "12.2.3" in result.stdout
        checks.append("relocated bundled proxy runs without Python on PATH")
    (output / "release-report.json").write_text(json.dumps({"passed": True, "checks": checks, "real_proxy_or_trust_store_changes": False}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Release verification passed: " + str(output / "release-report.json"))


if __name__ == "__main__":
    main()
