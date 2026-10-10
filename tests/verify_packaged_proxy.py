"""Repeat original loopback integration tests using the released proxy EXE."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_integration

if __name__ == "__main__":
    proxy = ROOT / "releases" / "OpenIDTool" / "runtime" / "mitmdump" / "mitmdump.exe"
    if not proxy.is_file():
        raise SystemExit("Build the Windows release first.")
    test_integration.MITMDUMP = str(proxy)
    resources = ROOT / "releases" / "OpenIDTool" / "_internal" / "resources"
    # The same addon/core files that the frozen GUI passes to its proxy child.
    code = "import sys; from pathlib import Path; import runtime_paths; runtime_paths.RESOURCE_ROOT = Path(sys.argv.pop(1)); import capture_openid; sys.exit(capture_openid.main(sys.argv[1:]))"
    test_integration.ProxyIntegrationTests.command_prefix = [sys.executable, "-c", code, str(resources)]
    test_integration.ProxyIntegrationTests.__unittest_skip__ = False
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(test_integration.ProxyIntegrationTests))
    raise SystemExit(0 if result.wasSuccessful() else 1)
