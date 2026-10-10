"""Audit the Git index and release ZIPs without printing secret contents."""
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {".git", ".venv", ".state", ".demo-state", "output", "demo-output",
             "verification", "build", "releases", "__pycache__", ".idea", ".vscode"}
PRIVATE_FILENAMES = {"openid.json", "openid.txt", "restore.json", "capture.json", ".env"}
PATTERNS = {
    "private key": rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----",
    "GitHub credential": rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})",
    "OpenID-shaped string literal": rb"[\"']o[A-Za-z0-9_-]{27}[\"']",
    "bearer credential": rb"(?i)bearer\s+[A-Za-z0-9_.-]{24,}",
    "JWT": rb"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\b",
}


def main():
    findings = []
    checked = 0
    # Compare to local identifiers without printing them or touching their files.
    identifiers = set()
    for folder in (ROOT / "output", ROOT / ".state"):
        for file in folder.glob("*.json"):
            try:
                value = json.loads(file.read_text(encoding="utf-8-sig")).get("openid")
                if isinstance(value, str) and len(value) >= 8:
                    identifiers.add(value.encode())
            except (OSError, ValueError, AttributeError):
                pass
    # These derive from this machine, and are not written to audit reports.
    private_paths = [str(ROOT).encode(), str(ROOT).replace("\\", "/").encode()]
    profile = os.environ.get("USERPROFILE")
    if profile:
        private_paths.extend([profile.encode(), profile.replace("\\", "/").encode()])

    def inspect(name, data, archive=False):
        nonlocal checked
        checked += 1
        path = Path(name)
        forbidden = FORBIDDEN - {"releases"} if archive else FORBIDDEN
        if forbidden.intersection(path.parts) or path.name in PRIVATE_FILENAMES:
            findings.append({"file": name, "category": "private file or directory"})
        if path.suffix.lower() in (".log", ".tmp", ".bak", ".env", ".pfx", ".p12", ".key"):
            findings.append({"file": name, "category": "local data extension"})
        for label, pattern in PATTERNS.items():
            if re.search(pattern, data):
                findings.append({"file": name, "category": label})
        for value in private_paths:
            variants = (value, value.replace(b"\\", b"\\\\"), value.decode().encode("utf-16le"))
            if any(variant.lower() in data.lower() for variant in variants):
                findings.append({"file": name, "category": "private local path"})
                break
        if any(value in data for value in identifiers):
            findings.append({"file": name, "category": "local captured identifier"})
        # Tests use one declared fictional URL to verify log redaction.
        for match in re.finditer(rb"https?://[^\s/\"']+:[^\s/\"']+@([^/\s\"']+)", data):
            fictional_test = match.group(1) == b"example.test"
            tcl_example = b"http://" + b"jschmoe:xyzzy@" + b"www.bogus.net:8000"
            tcl_documentation_example = (name.endswith("tcl8/8.6/http-2.9.8.tm")
                                         and match.group(0) == tcl_example)
            if not fictional_test and not tcl_documentation_example:
                findings.append({"file": name, "category": "credential-bearing URL"})

    staged = subprocess.check_output(["git", "ls-files", "--cached", "-z"], cwd=ROOT).split(b"\0")
    for entry in staged:
        if entry:
            name = entry.decode("utf-8")
            data = subprocess.check_output(["git", "show", ":" + name], cwd=ROOT)
            inspect(name, data)
    for file in sorted((ROOT / "releases").glob("*.zip")):
        with zipfile.ZipFile(file) as archive:
            for entry in archive.infolist():
                if not entry.is_dir():
                    inspect(file.name + "/" + entry.filename, archive.read(entry), archive=True)
    result = {"passed": not findings, "checked_files": checked, "findings": findings,
              "local_data_modified": False}
    output = ROOT / "verification" / "publication-audit.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    if findings:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
