"""Publish only real Tk windows backed by fictional offline data."""
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
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    destination = ROOT / "docs" / "screenshots"
    destination.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    backend = OfflineBackend(delay=30)
    with patch.object(sys, "frozen", True, create=True):
        app = DesktopApp(root, backend=backend,
                         base_dir=ROOT / "verification" / "public-screenshot-demo", demo=True)
    root.attributes("-topmost", True)
    root.geometry("840x700+80+80")

    def wait_until(predicate):
        deadline = time.monotonic() + 8
        while not predicate() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.025)
        assert predicate(), "Offline screenshot deadline"

    def snapshot(name, messages):
        # Use declared demo log lines, never publish local output paths.
        app.log.configure(state="normal")
        app.log.delete("1.0", "end")
        app.log.configure(state="disabled")
        for message in messages:
            app.append_log(message)
        root.update()
        time.sleep(0.2)
        root.update()
        box = (root.winfo_rootx(), root.winfo_rooty(),
               root.winfo_rootx() + root.winfo_width(), root.winfo_rooty() + root.winfo_height())
        ImageGrab.grab(bbox=box).save(destination / name)

    try:
        root.update()
        app.start("capture")
        wait_until(lambda: app.stage == "waiting")
        snapshot("waiting.png", ["离线演示：使用虚构数据，不修改系统代理或证书库。",
                                 "准备环境完成，代理已就绪。", "正在监听 127.0.0.1:8866，等待微信登录。"])
        app.cancel()
        wait_until(lambda: not app.controller.busy)
        backend.delay = 0.3
        app.start("capture")
        wait_until(lambda: not app.controller.busy)
        assert app.openid == "offline_demo_openid" and not app.reveal.get()
        snapshot("success.png", ["离线演示：使用虚构数据，不修改系统代理或证书库。",
                                 "捕获成功，示例结果已保存到 demo-output/openid.json。",
                                 "已恢复代理、移除本次新增证书并停止代理进程。",
                                 "OpenID 已隐藏，可点击“复制 OpenID”。"])
    finally:
        if app.controller.busy:
            app.cancel()
            wait_until(lambda: not app.controller.busy)
        app.on_close()


if __name__ == "__main__":
    main()
