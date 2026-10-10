import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import addon
import capture_openid as cli
from core import TARGET_HOST, TARGET_PATH, extract_openid, save_env
from windows_proxy import capture_proxy


class ParsingTests(unittest.TestCase):
    def test_expected_response(self):
        url = f"https://{TARGET_HOST}{TARGET_PATH}?jsCode=secret"
        self.assertEqual(extract_openid(url, '{"data":"  sample_openid  "}'), "sample_openid")

    def test_other_host_and_similar_path_rejected(self):
        for url in (f"https://evil.test/{TARGET_PATH}?host={TARGET_HOST}", f"https://{TARGET_HOST}{TARGET_PATH}/extra", f"https://{TARGET_HOST}/other?x=getOpenidByJsCode"):
            with self.subTest(url=url):
                self.assertIsNone(extract_openid(url, '{"data":"sample_openid"}'))

    def test_invalid_payloads_ignored(self):
        for payload in ("", "not json", "[]", "null", '{"data":null}', '{"data":{}}', '{"data":123}', '{"data":"  "}', '{"data":"a,b"}', '{"data":"a\\nPORT=1"}'):
            self.assertIsNone(extract_openid(f"https://{TARGET_HOST}{TARGET_PATH}", payload))

    def test_account_append_and_bom(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text('# comment\nPORT=3001\nopenid="old,old"\n', encoding="utf-8-sig")
            save_env(env, "new")
            save_env(env, "new")
            self.assertEqual(env.read_text(encoding="utf-8"), '# comment\nPORT=3001\nopenid=old,new\n')

    def test_addon_only_records_success_and_strips_query(self):
        with tempfile.TemporaryDirectory() as folder:
            result = Path(folder) / "result.json"
            ready = Path(folder) / "ready"
            with patch.dict("os.environ", {"OPENID_TARGET_HOST": TARGET_HOST, "OPENID_TARGET_PATH": TARGET_PATH, "OPENID_RESULT_FILE": str(result), "OPENID_READY_FILE": str(ready)}):
                capture = addon.OpenIdCapture()
                capture.running()
                self.assertTrue(ready.exists())
                response = SimpleNamespace(status_code=500, get_text=lambda strict: '{"data":"fake"}')
                flow = SimpleNamespace(request=SimpleNamespace(pretty_url=f"https://{TARGET_HOST}{TARGET_PATH}?jsCode=secret"), response=response)
                capture.response(flow)
                self.assertFalse(result.exists())
                response.status_code = 200
                capture.response(flow)
                record = json.loads(result.read_text(encoding="utf-8"))
                self.assertEqual(record["openid"], "fake")
                self.assertNotIn("secret", result.read_text(encoding="utf-8"))
                response.get_text = lambda strict: '{"data":"second"}'
                capture.response(flow)
                self.assertEqual(json.loads(result.read_text(encoding="utf-8"))["openid"], "fake")


class RecoveryTests(unittest.TestCase):
    def test_capture_settings_do_not_mutate_original(self):
        original = {"ProxyOverride": {"value": "*.intranet;localhost", "kind": 1}, "AutoConfigURL": {"value": "https://pac.test/a", "kind": 1}}
        settings = capture_proxy(original, 18866)
        self.assertEqual(settings["ProxyServer"]["value"], "127.0.0.1:18866")
        self.assertIsNone(settings["AutoConfigURL"])
        self.assertEqual(settings["AutoDetect"]["value"], 0)
        self.assertIn("*.intranet", settings["ProxyOverride"]["value"])
        self.assertEqual(original["AutoConfigURL"]["value"], "https://pac.test/a")

    def test_restore_exact_snapshot_and_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "restore.json"
            original = {"ProxyEnable": None, "AutoConfigURL": {"value": "https://pac.test", "kind": 1}}
            state.write_text(json.dumps({"proxy": original, "new_certificate": "test-thumb", "process": {"pid": 1}}), encoding="utf-8")
            with patch.object(cli, "write_proxy") as write, patch.object(cli, "cert_is_trusted", return_value=True), patch.object(cli, "certutil") as cert, patch.object(cli, "stop_saved_process") as stop:
                self.assertTrue(cli.restore_state(state))
                write.assert_called_once_with(original)
                cert.assert_called_once_with("-delstore", "Root", "test-thumb")
                stop.assert_called_once_with({"pid": 1})
                self.assertFalse(state.exists())

    def test_already_removed_certificate_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "restore.json"
            state.write_text(json.dumps({"proxy": None, "new_certificate": "gone"}), encoding="utf-8")
            with patch.object(cli, "cert_is_trusted", return_value=False), patch.object(cli, "certutil") as cert:
                cli.restore_state(state)
                cert.assert_not_called()

    def test_failed_proxy_restore_keeps_backup_and_still_cleans_process(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "restore.json"
            state.write_text(json.dumps({"proxy": {}, "new_certificate": None}), encoding="utf-8")
            with patch.object(cli, "write_proxy", side_effect=OSError("simulated")), patch.object(cli, "stop_saved_process") as stop, patch.object(cli, "certutil") as cert:
                with self.assertRaisesRegex(RuntimeError, "备份已保留"):
                    cli.restore_state(state)
                self.assertTrue(state.exists())
                stop.assert_called_once_with(None)
                cert.assert_not_called()

    def test_invalid_timeout_rejected(self):
        for value in ("0", "-1", "nan", "inf"):
            with self.assertRaises(Exception):
                cli.positive_number(value)


if __name__ == "__main__":
    unittest.main()
