"""Offline lifecycle/UI regression tests; no real registry or certificate mutation."""
import json
import io
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capture_openid as engine
from core import TARGET_HOST, TARGET_PATH
from desktop import DesktopApp
from desktop_service import CaptureController
from offline_demo import OfflineBackend


def arguments(folder, timeout=1):
    return SimpleNamespace(output=folder / "result.json", state_dir=folder / "state", port=8866, timeout=timeout, mitmdump=None, manual_proxy=False, skip_cert_install=False, upstream=None, upstream_ca=None, env_file=None, target_host=TARGET_HOST, target_path=TARGET_PATH)


def settle(controller, events, seconds=5):
    deadline = time.monotonic() + seconds
    while controller.busy and time.monotonic() < deadline:
        events.extend(controller.drain())
        time.sleep(0.01)
    if controller.thread:
        controller.thread.join(timeout=1)
    events.extend(controller.drain())
    if controller.busy:
        raise AssertionError("worker did not finish")


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def assert_clean(self, backend, args):
        self.assertFalse((args.state_dir / "restore.json").exists())
        self.assertFalse((args.state_dir / "capture.json").exists())
        self.assertFalse((args.state_dir / "ready").exists())
        # A failed child has already exited; recovery still checks its identity.
        self.assertIn(("recover_process", None), backend.actions)
        self.assertFalse(backend.trusted)

    def test_success_states_countdown_privacy_and_cleanup(self):
        backend = OfflineBackend(delay=0.3)
        controller = CaptureController(backend)
        args, events = arguments(self.folder), []
        self.assertTrue(controller.start("capture", args))
        self.assertFalse(controller.start("capture", args))
        settle(controller, events)
        stages = [item["stage"] for item in events]
        for stage in ("preparing", "starting", "waiting", "captured", "cleanup", "cleaned", "success", "done"):
            self.assertIn(stage, stages)
        self.assertLess(stages.index("cleanup"), stages.index("success"))
        self.assertTrue(any("remaining" in item for item in events))
        messages = "\n".join(item.get("message", "") for item in events)
        for secret in ("offline_demo_openid", "offline-secret", "do-not-log", "PRIVATE KEY"):
            self.assertNotIn(secret, messages)
        self.assertEqual(events[-1]["result"]["openid"], "offline_demo_openid")
        self.assertEqual(args.output.with_suffix(".txt").read_text().strip(), "offline_demo_openid")
        self.assertNotIn("offline-secret", args.output.read_text())
        self.assertIn(("certificate", ("-delstore", "Root", "OFFLINE-CA")), backend.actions)
        self.assert_clean(backend, args)

    def test_timeout_and_process_error_both_restore(self):
        for outcome in ("timeout", "error"):
            with self.subTest(outcome=outcome):
                backend = OfflineBackend(outcome, delay=0.05)
                controller, events = CaptureController(backend), []
                args = arguments(self.folder, timeout=0.3)
                controller.start("capture", args)
                settle(controller, events)
                self.assertEqual(events[-1]["outcome"], outcome)
                if outcome == "timeout":
                    self.assertIn("微信", events[-1]["message"])
                self.assert_clean(backend, args)
                self.assertFalse(args.output.exists())

    def test_cancel_during_startup_and_waiting(self):
        for phase in ("starting", "waiting"):
            backend = OfflineBackend(delay=30, startup_delay=30 if phase == "starting" else 0)
            controller, events = CaptureController(backend), []
            args = arguments(self.folder, timeout=30)
            controller.start("capture", args)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                events.extend(controller.drain())
                if any(e["stage"] == phase for e in events):
                    break
                time.sleep(0.01)
            self.assertTrue(any(e["stage"] == phase for e in events))
            controller.cancel()
            settle(controller, events)
            self.assertEqual(events[-1]["outcome"], "cancelled")
            self.assert_clean(backend, args)

    def test_old_backup_not_overwritten(self):
        args = arguments(self.folder)
        args.state_dir.mkdir()
        state = args.state_dir / "restore.json"
        sentinel = '{"proxy": null, "sentinel": "old"}'
        state.write_text(sentinel)
        backend = OfflineBackend(delay=0.01)
        with self.assertRaisesRegex(RuntimeError, "未恢复"):
            backend.capture(args, lambda e: None, threading.Event())
        self.assertEqual(state.read_text(), sentinel)
        self.assertEqual(backend.actions, [])

    def test_observer_failure_does_not_interrupt_cleanup(self):
        backend = OfflineBackend(delay=0.01)
        args = arguments(self.folder)
        def broken(event):
            raise RuntimeError("observer fault")
        result = backend.capture(args, broken, threading.Event())
        self.assertEqual(result["openid"], "offline_demo_openid")
        self.assert_clean(backend, args)

    def test_cleaning_failure_keeps_backup_and_retry_restores(self):
        backend = OfflineBackend(delay=0.01)
        args = arguments(self.folder)
        with patch.object(engine, "restore_state", side_effect=RuntimeError("模拟恢复失败，备份保留")):
            with self.assertRaisesRegex(RuntimeError, "恢复失败"):
                backend.capture(args, lambda e: None, threading.Event())
        self.assertTrue((args.state_dir / "restore.json").exists())
        self.assertIn(("stop_process", None), backend.actions)
        backend.restore_state(args.state_dir / "restore.json", lambda e: None)
        self.assertFalse((args.state_dir / "restore.json").exists())
        self.assertFalse(backend.trusted)

    @unittest.skipUnless(sys.platform == "win32", "Windows named mutex")
    def test_windows_mutex_rejects_other_worker(self):
        from windows_proxy import exclusive_capture
        ready = threading.Event()
        release = threading.Event()
        def holder():
            with exclusive_capture():
                ready.set()
                release.wait(5)
        thread = threading.Thread(target=holder)
        thread.start()
        try:
            self.assertTrue(ready.wait(2))
            controller, events = CaptureController(OfflineBackend()), []
            controller.start("capture", arguments(self.folder))
            settle(controller, events)
            self.assertEqual(events[-1]["outcome"], "error")
            self.assertIn("另一份", events[-1]["message"])
        finally:
            release.set()
            thread.join(2)

    def test_install_progress_and_cancellation(self):
        controller, events = CaptureController(OfflineBackend()), []
        controller.start("install", SimpleNamespace())
        time.sleep(0.2)
        controller.cancel()
        settle(controller, events)
        self.assertTrue(any(e["stage"] == "installing" for e in events))
        self.assertEqual(events[-1]["outcome"], "cancelled")

    def test_cli_doctor_keeps_original_arguments(self):
        with patch.object(engine, "doctor") as doctor:
            self.assertEqual(engine.main(["doctor", "--port", "8867", "--mitmdump", "existing.exe"]), 0)
            args = doctor.call_args.args[0]
            self.assertEqual(args.port, 8867)
            self.assertEqual(args.mitmdump, "existing.exe")

    def test_output_cannot_overwrite_recovery_journal(self):
        args = arguments(self.folder)
        args.output = args.state_dir / "restore.json"
        backend = OfflineBackend(delay=0.01)
        with self.assertRaisesRegex(RuntimeError, "恢复状态目录"):
            backend.capture(args, lambda e: None, threading.Event())
        self.assertEqual(backend.actions, [])

    def test_cancel_during_automatic_check_does_not_start_proxy(self):
        backend = OfflineBackend()
        controller = CaptureController(backend, check_before_capture=True)
        events = []
        def check(args, callback):
            callback({"stage": "checking", "message": "自动检查环境。"})
            controller.cancel()
        with patch.object(backend, "doctor", side_effect=check), patch.object(backend, "capture") as capture:
            controller.start("capture", arguments(self.folder))
            settle(controller, events)
            capture.assert_not_called()
        self.assertEqual(events[-1]["outcome"], "cancelled")
        self.assertEqual(backend.actions, [])


class DependencyTests(unittest.TestCase):
    """Exercise the production installer, replacing only external processes."""
    def fake_process(self, text, code=0):
        process = SimpleNamespace(stdout=io.StringIO(text), returncode=code)
        process.poll = lambda: process.returncode
        process.terminate = lambda: setattr(process, "returncode", 0)
        process.kill = process.terminate
        process.wait = lambda timeout=None: process.returncode
        return process

    def test_streamed_progress_and_https_source(self):
        events = []
        processes = [self.fake_process("environment ready\n"), self.fake_process("Downloading mitmproxy\nSuccessfully installed\n")]
        with tempfile.TemporaryDirectory() as folder, patch.object(engine, "ROOT", Path(folder)), patch.object(engine, "external_python", return_value=["offline-python"]), patch.object(engine.subprocess, "Popen", side_effect=processes) as popen:
            engine.install(SimpleNamespace(index_url="https://example.test/simple"), events.append, threading.Event())
            command = popen.call_args_list[-1].args[0]
            self.assertIn("--index-url", command)
            self.assertNotIn("--trusted-host", command)
        self.assertTrue(any(e["message"] == "Downloading mitmproxy" for e in events))
        self.assertEqual(events[-1]["stage"], "installed")

    def test_install_failure_is_actionable_and_redacts_urls(self):
        events = []
        processes = [self.fake_process("environment ready\n"), self.fake_process("ERROR TLS failure https://user:password@example.test/file\n", 1)]
        with tempfile.TemporaryDirectory() as folder, patch.object(engine, "ROOT", Path(folder)), patch.object(engine, "external_python", return_value=["offline-python"]), patch.object(engine.subprocess, "Popen", side_effect=processes):
            with self.assertRaisesRegex(RuntimeError, "HTTPS pip 源") as error:
                engine.install(SimpleNamespace(index_url=None), events.append, threading.Event())
        self.assertNotIn("password", str(error.exception))
        self.assertNotIn("password", "\n".join(e["message"] for e in events))

    def test_install_cancel_stops_child_process(self):
        cancel = threading.Event()
        process = self.fake_process("streaming\n", None)
        def observer(event):
            if event["message"] == "streaming":
                cancel.set()
        with tempfile.TemporaryDirectory() as folder, patch.object(engine, "ROOT", Path(folder)), patch.object(engine, "external_python", return_value=["offline-python"]), patch.object(engine.subprocess, "Popen", return_value=process):
            with self.assertRaises(engine.CaptureCancelled):
                engine.install(SimpleNamespace(index_url=None), observer, cancel)
        self.assertEqual(process.returncode, 0)


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.root = tk.Tk()
        self.root.withdraw()
        self.backend = OfflineBackend(delay=0.25)
        self.app = DesktopApp(self.root, backend=self.backend, base_dir=self.folder, demo=True)

    def tearDown(self):
        if not self.app.closed:
            self.app.controller.cancel()
            self.until(lambda: not self.app.controller.busy)
            self.app.destroy()
        self.temp.cleanup()

    def until(self, predicate, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            if not self.app.closed:
                self.root.update()
            time.sleep(0.015)
        self.fail("UI event was not delivered")

    def test_success_mask_reveal_and_logs(self):
        self.app.start("capture")
        self.assertEqual(str(self.app.start_button["state"]), "disabled")
        self.root.update()
        self.until(lambda: not self.app.controller.busy)
        self.assertEqual(self.app.openid, "offline_demo_openid")
        self.assertNotIn(self.app.openid, self.app.openid_text.get())
        self.app.reveal.set(True)
        self.app.update_openid()
        self.assertEqual(self.app.openid_text.get(), self.app.openid)
        logs = self.app.log.get("1.0", "end")
        self.assertNotIn(self.app.openid, logs)
        self.assertNotIn("offline-secret", logs)
        with patch.object(self.root, "clipboard_clear"), patch.object(self.root, "clipboard_append") as copy:
            self.app.copy_openid()
            copy.assert_called_once_with(self.app.openid)

    def test_close_waits_for_cleanup(self):
        self.backend.delay = 30
        self.app.start("capture")
        self.until(lambda: self.app.stage == "waiting")
        self.app.on_close()
        self.assertTrue(self.app.close_requested)
        self.assertFalse(self.app.closed)
        self.until(lambda: self.app.closed)
        self.assertFalse((self.app.state_dir / "restore.json").exists())
        self.assertFalse(self.backend.trusted)
        self.assertIn(("stop_process", None), self.backend.actions)

    def test_close_cleanup_failure_keeps_window_open(self):
        self.backend.delay = 30
        self.app.start("capture")
        self.until(lambda: self.app.stage == "waiting")
        with patch.object(engine, "restore_state", side_effect=RuntimeError("模拟恢复失败")):
            self.app.on_close()
            self.until(lambda: not self.app.controller.busy)
        self.assertFalse(self.app.closed)
        self.assertFalse(self.app.close_requested)
        self.assertIn("清理失败", self.app.notice.get())
        self.app.start("restore")
        self.until(lambda: not self.app.controller.busy)
        self.assertFalse((self.app.state_dir / "restore.json").exists())

    def test_recovery_works_with_invalid_settings(self):
        self.app.state_dir.mkdir()
        state = self.app.state_dir / "restore.json"
        state.write_text(json.dumps({"proxy": None, "new_certificate": None}))
        self.app.refresh_recovery()
        self.assertEqual(str(self.app.start_button["state"]), "disabled")
        self.app.port.set("bad-port")
        self.app.start("restore")
        self.until(lambda: not self.app.controller.busy)
        self.assertFalse(state.exists())

    def test_resizing_keeps_controls_visible(self):
        self.root.deiconify()
        for advanced in (False, True):
            self.app.advanced.set(advanced)
            self.app.toggle_advanced()
            for width, height in ((720, 790 if advanced else 640), (1080, 1000)):
                self.root.geometry(f"{width}x{height}")
                self.root.update()
                for widget in (self.app.start_button, self.app.cancel_button, self.app.restore_button, self.app.doctor_button, self.app.install_button, self.app.log, self.app.result_entry, self.app.copy_button, self.app.folder_button, self.app.privacy_label):
                    if widget in (self.app.install_button, self.app.doctor_button) and not advanced:
                        continue
                    self.assertTrue(widget.winfo_viewable())
                    right = widget.winfo_rootx() - self.root.winfo_rootx() + widget.winfo_width()
                    bottom = widget.winfo_rooty() - self.root.winfo_rooty() + widget.winfo_height()
                    self.assertLessEqual(right, self.root.winfo_width())
                    self.assertLessEqual(bottom, self.root.winfo_height())
                self.assertGreaterEqual(self.app.log.winfo_height(), 70)

    def test_source_install_entry_only_in_advanced_settings(self):
        self.root.deiconify()
        self.root.update()
        self.assertIsNotNone(self.app.install_button)
        self.assertIsNotNone(self.app.doctor_button)
        self.assertFalse(self.app.install_button.winfo_viewable())
        self.assertFalse(self.app.doctor_button.winfo_viewable())
        self.app.advanced.set(True)
        self.app.toggle_advanced()
        self.root.update()
        self.assertTrue(self.app.install_button.winfo_viewable())
        self.assertTrue(self.app.doctor_button.winfo_viewable())

    def make_packaged(self):
        self.app.destroy()
        self.root = tk.Tk()
        self.root.withdraw()
        with patch.object(sys, "frozen", True, create=True):
            self.app = DesktopApp(self.root, backend=self.backend, base_dir=self.folder, demo=True)

    def test_packaged_ui_has_no_install_entry_and_rejects_install(self):
        self.make_packaged()
        self.assertIsNone(self.app.install_button)
        self.assertIsNone(self.app.doctor_button)
        self.root.deiconify()
        self.root.update()
        self.assertFalse(self.app.port_entry.winfo_viewable())
        self.assertFalse(self.app.timeout_entry.winfo_viewable())
        self.app.advanced.set(True)
        self.app.toggle_advanced()
        self.root.update()
        self.assertTrue(self.app.port_entry.winfo_viewable())
        self.assertTrue(self.app.timeout_entry.winfo_viewable())
        self.app.start("install")
        self.assertFalse(self.app.controller.busy)
        self.assertIn("发行版已内置依赖", self.app.notice.get())
        self.app.start("doctor")
        self.assertFalse(self.app.controller.busy)
        self.assertIn("自动检查", self.app.notice.get())
        self.app.start("capture")
        self.until(lambda: not self.app.controller.busy)
        self.assertEqual(self.app.openid, "offline_demo_openid")

    def test_packaged_automatic_check_failure_blocks_proxy_start(self):
        self.make_packaged()
        with patch.object(self.backend, "doctor", side_effect=RuntimeError("运行组件缺失，请重新完整解压发行包。")), patch.object(self.backend, "capture") as capture:
            self.app.start("capture")
            self.until(lambda: not self.app.controller.busy)
            capture.assert_not_called()
        self.assertIn("重新完整解压", self.app.notice.get())
        self.assertFalse(self.app.state_dir.exists())
        self.assertEqual(self.backend.actions, [])


if __name__ == "__main__":
    unittest.main()
