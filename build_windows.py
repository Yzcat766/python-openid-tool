"""Reproducible portable Windows build; source/result/CA state are kept separate."""
import hashlib
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(arguments):
    subprocess.run([sys.executable, *arguments], cwd=ROOT, check=True)


def archive(folder, destination):
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as package:
        for file in sorted(folder.rglob("*")):
            relative = file.relative_to(folder)
            if file.is_file() and not any(part in (".state", ".demo-state", "output", "demo-output", "__pycache__") for part in relative.parts):
                package.write(file, folder.name + "/" + relative.as_posix())


def bundle_native_dependencies(app_root):
    # Conda keeps native dependencies outside its Python DLL directory.
    # Resolve them recursively rather than relying on the build machine's PATH.
    conda_bin = Path(sys.base_prefix) / "Library" / "bin"
    if not conda_bin.is_dir():
        return
    from PyInstaller.depend.bindepend import binary_dependency_analysis
    internal = app_root / "_internal"
    binaries = [(str(file.relative_to(internal)), str(file), "BINARY")
                for file in internal.rglob("*") if file.suffix.lower() in (".dll", ".pyd")]
    for name, source, kind in binary_dependency_analysis(
            binaries, search_paths=[str(internal), str(conda_bin), sys.base_prefix]):
        target = internal / name
        if kind == "BINARY" and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def verify_proxy_runtime(proxy_root):
    env = os.environ.copy()
    env["PATH"] = str(Path(env["SYSTEMROOT"]) / "System32")
    for name in ("PYTHONHOME", "PYTHONPATH", "CONDA_PREFIX"):
        env.pop(name, None)
    result = subprocess.run([str(proxy_root / "mitmdump.exe"), "--version"],
                            env=env, capture_output=True, timeout=20)
    if result.returncode or b"12.2.3" not in result.stdout:
        raise RuntimeError("Bundled proxy failed its standalone version check: "
                           + result.stderr.decode("utf-8", errors="replace"))


def main():
    parser = argparse.ArgumentParser(description="Build or repackage the Windows release.")
    parser.add_argument("--package-only", action="store_true", help="Package existing build/dist artifacts without rebuilding EXEs")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("Please build Windows EXEs on Windows.")
    if not args.package_only:
        run(["-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console", "--name", "mitmdump", "--distpath", "build/dist", "--workpath", "build/work-proxy", "--specpath", "build", "--collect-all", "mitmproxy", "--collect-all", "mitmproxy_rs", "--collect-all", "pydivert", "--copy-metadata", "mitmproxy", "mitmdump_entry.py"])
        run(["-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed", "--name", "OpenIDTool", "--distpath", "build/dist", "--workpath", "build/work-gui", "--specpath", "build", "--add-data", str(ROOT / "addon.py") + ";resources", "--add-data", str(ROOT / "core.py") + ";resources", "--add-data", str(ROOT / "requirements.txt") + ";resources", "desktop.py"])
    releases = ROOT / "releases"
    releases.mkdir(exist_ok=True)
    portable = releases / "OpenIDTool"
    # Package a fresh staging tree, never a user's already-used portable folder.
    with tempfile.TemporaryDirectory(dir=ROOT / "build", prefix="package-") as staging:
        staging_path = Path(staging).resolve()
        assert staging_path.is_relative_to((ROOT / "build").resolve())
        package_root = staging_path / "OpenIDTool"
        shutil.copytree(ROOT / "build" / "dist" / "OpenIDTool", package_root)
        shutil.copytree(ROOT / "build" / "dist" / "mitmdump", package_root / "runtime" / "mitmdump")
        bundle_native_dependencies(package_root)
        bundle_native_dependencies(package_root / "runtime" / "mitmdump")
        verify_proxy_runtime(package_root / "runtime" / "mitmdump")
        for name in ("README.md", "LICENSE", "NOTICE.md", "CHANGELOG.md", "VERSION"):
            shutil.copy2(ROOT / name, package_root / name)
        if (ROOT / "docs").is_dir():
            shutil.copytree(ROOT / "docs", package_root / "docs")
        from importlib.metadata import distributions
        versions = {dist.metadata["Name"]: dist.version for dist in distributions()}
        (package_root / "BUILD-INFO.json").write_text(json.dumps({"version": (ROOT / "VERSION").read_text().strip(), "python": sys.version, "architecture": "Windows x64", "packages": versions}, ensure_ascii=False, indent=2), encoding="utf-8")
        archive(package_root, releases / "python-openid-tool-windows-x64.zip")
        shutil.copytree(package_root, portable, dirs_exist_ok=True)
    sources = [*ROOT.glob("*.py"), *ROOT.glob("*.cmd"), *(ROOT / name for name in ("README.md", "LICENSE", "NOTICE.md", "CHANGELOG.md", "VERSION", "requirements.txt", "requirements-build.txt", ".gitignore")), *sorted((ROOT / "tests").glob("*.py")), *sorted((ROOT / "docs" / "screenshots").glob("*.png"))]
    with zipfile.ZipFile(releases / "python-openid-tool-source.zip", "w", zipfile.ZIP_DEFLATED) as package:
        for file in sources:
            package.write(file, "python-openid-tool/" + file.relative_to(ROOT).as_posix())
    hashes = []
    for file in sorted(releases.glob("*.zip")):
        hashes.append(hashlib.sha256(file.read_bytes()).hexdigest() + "  " + file.name)
    (releases / "SHA256SUMS.txt").write_text("\n".join(hashes) + "\n", encoding="ascii")
    print("Built: " + str(portable / "OpenIDTool.exe"))


if __name__ == "__main__":
    main()
