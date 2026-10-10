"""Explicit offline EXE smoke test driven through the actual Tk event loop."""
import json
import time
import sys
from pathlib import Path


def run(app):
    report = {"mode": "offline-only", "checks": [], "errors": []}
    destination = app.base_dir / "verification-report.json"
    phase = 0
    deadline = time.monotonic() + 30
    def check(value, label):
        if not value:
            raise AssertionError(label)
        report["checks"].append(label)
    def save():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    original_destroy = app.destroy
    def verified_destroy():
        try:
            if phase == 6:
                check(not (app.state_dir / "restore.json").exists(), "关闭后恢复记录清理")
                check(not app.controller.backend.trusted, "模拟证书已移除")
                check(any(a[0] == "stop_process" for a in app.controller.backend.actions), "代理子进程停止")
                report["passed"] = True
                save()
        except Exception as error:
            report["errors"].append(str(error))
            report["passed"] = False
            save()
        finally:
            original_destroy()
    app.destroy = verified_destroy
    def tick():
        nonlocal phase
        try:
            if time.monotonic() > deadline:
                raise TimeoutError("offline verification deadline")
            if phase == 0:
                if getattr(sys, "frozen", False):
                    import capture_openid as engine
                    from runtime_paths import RESOURCE_ROOT
                    check(Path(engine.locate_mitmdump()).is_file(), "内置代理组件可定位")
                    check((RESOURCE_ROOT / "addon.py").is_file() and (RESOURCE_ROOT / "core.py").is_file(), "内置捕获资源齐全")
                    check(app.install_button is None, "发行版不提供安装依赖入口")
                    check(app.doctor_button is None, "发行版不提供检查环境入口")
                    check(app.controller.check_before_capture, "开始时自动检查环境")
                    # Real bundled component, read-only version/port checks only.
                    engine.doctor(app.arguments("capture"), lambda event: None)
                    check(True, "内置组件自动检查通过（不修改系统设置）")
                app.controller.backend.delay = 0.3
                app.start("capture")
                check(str(app.start_button["state"]) == "disabled", "捕获期间禁用启动")
                phase = 1
            elif phase == 1 and not app.controller.busy:
                check(app.openid == "offline_demo_openid", "离线捕获与结果保存")
                check(app.openid not in app.openid_text.get(), "默认隐藏 OpenID")
                check(app.openid not in app.log.get("1.0", "end"), "日志不包含 OpenID")
                check(not (app.state_dir / "restore.json").exists(), "成功后清理恢复记录")
                app.reveal.set(True)
                app.update_openid()
                check(app.openid_text.get() == app.openid, "显示切换")
                app.reveal.set(False)
                app.update_openid()
                app.advanced.set(True)
                app.toggle_advanced()
                app.root.geometry("720x790")
                phase = 2
            elif phase == 2:
                for widget in (app.start_button, app.cancel_button, app.restore_button, app.doctor_button, app.install_button, app.log, app.folder_button, app.privacy_label):
                    if widget is None:
                        continue
                    check(widget.winfo_viewable() and widget.winfo_rooty() + widget.winfo_height() <= app.root.winfo_rooty() + app.root.winfo_height(), "最小窗口可见：" + widget.winfo_class())
                app.controller.backend.outcome = "timeout"
                app.timeout.set("0.4")
                app.start("capture")
                phase = 3
            elif phase == 3 and not app.controller.busy:
                check("超时" in app.status.get(), "超时状态与清理")
                check(not (app.state_dir / "restore.json").exists(), "超时恢复记录清理")
                app.controller.backend.outcome = "error"
                app.timeout.set("3")
                app.start("capture")
                phase = 4
            elif phase == 4 and not app.controller.busy:
                check("失败" in app.status.get(), "进程错误提示")
                check(not (app.state_dir / "restore.json").exists(), "错误后恢复记录清理")
                app.controller.backend.outcome = "timeout"
                app.controller.backend.delay = 30
                app.timeout.set("30")
                app.start("capture")
                phase = 5
            elif phase == 5 and app.stage == "waiting":
                phase = 6
                app.on_close()
                check(app.close_requested and not app.closed, "关闭窗口先取消并等待")
            if app.closed:
                check(not (app.state_dir / "restore.json").exists(), "关闭后恢复记录清理")
                check(not app.controller.backend.trusted, "模拟证书已移除")
                check(any(a[0] == "stop_process" for a in app.controller.backend.actions), "代理子进程停止")
                report["passed"] = True
                save()
                return
            app.root.after(80, tick)
        except Exception as error:
            report["errors"].append(str(error))
            report["passed"] = False
            save()
            app.on_close()
    app.root.after(100, tick)
