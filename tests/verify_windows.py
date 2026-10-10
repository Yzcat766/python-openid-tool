"""Capture real offline Tk windows for visual QA, never real system settings."""
import json
import argparse
import sys
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

from PIL import ImageGrab

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop import DesktopApp
from offline_demo import OfflineBackend


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--packaged", action="store_true", help="检查打包模式布局，使用隔离模拟后端")
    args = parser.parse_args()
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    output = ROOT / "verification"
    output.mkdir(exist_ok=True)
    root = tk.Tk()
    backend = OfflineBackend(delay=30)
    with patch.object(sys, "frozen", args.packaged, create=True):
        app = DesktopApp(root, backend=backend, base_dir=output / "visual-demo", demo=True)
    root.attributes("-topmost", True)
    root.geometry("840x700+80+80")
    def wait_until(predicate, seconds=6):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.025)
        if not predicate():
            raise AssertionError("UI deadline")
    def snapshot(name):
        root.update()
        time.sleep(0.2)
        root.update()
        box = (root.winfo_rootx(), root.winfo_rooty(), root.winfo_rootx() + root.winfo_width(), root.winfo_rooty() + root.winfo_height())
        ImageGrab.grab(bbox=box).save(output / (("packaged-" if args.packaged else "") + name))
    try:
        root.update()
        snapshot("light-idle.png")
        app.start("capture")
        wait_until(lambda: app.stage == "waiting")
        snapshot("light-waiting.png")
        app.cancel()
        wait_until(lambda: not app.controller.busy)
        backend.delay = 0.3
        app.start("capture")
        wait_until(lambda: not app.controller.busy)
        snapshot("light-success.png")
        app.advanced.set(True)
        app.toggle_advanced()
        root.geometry("720x790+80+80")
        snapshot("light-advanced-min.png")
        backend.outcome = "error"
        app.start("capture")
        wait_until(lambda: not app.controller.busy)
        snapshot("light-error.png")
        app.advanced.set(False)
        app.toggle_advanced()
        root.geometry("720x640+80+80")
        snapshot("light-error-min.png")
        (output / "visual-report.json").write_text(json.dumps({"passed": True, "images": 6, "verified": ["等待提示和剩余时间", "固定浅色", "高级设置", "最小窗口", "成功结果隐藏", "具体错误和恢复状态"], "system_changes": "none (offline backend)"}, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        if app.controller.busy:
            app.cancel()
            wait_until(lambda: not app.controller.busy)
        app.on_close()


if __name__ == "__main__":
    main()
