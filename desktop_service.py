"""Thread/queue boundary: workers never access Tk widgets."""
import queue
import json
import threading
from pathlib import Path

import capture_openid as engine
from windows_proxy import exclusive_capture


def actionable_error(error):
    if isinstance(error, json.JSONDecodeError):
        return f"恢复记录或临时结果的 JSON 格式损坏（第 {error.lineno} 行、第 {error.colno} 列）。请保留备份，检查文件是否被其它程序改写，修复后重试“恢复代理”。"
    if isinstance(error, TimeoutError):
        return str(error)
    if isinstance(error, PermissionError):
        return "访问被拒绝。请将工具解压到当前用户可写的目录；若证书策略受限，请联系管理员。恢复记录会保留，请重试“恢复代理”。"
    if isinstance(error, OSError):
        return f"系统操作失败（错误码 {getattr(error, 'winerror', None) or error.errno}）。请检查目录权限、磁盘空间和程序路径，再重试；如有恢复记录，请先恢复代理。"
    # Engine errors contain controlled explanations, never response bodies.
    message = str(error)
    if isinstance(error, RuntimeError):
        return message
    return message + "。请检查配置和依赖；如有恢复记录，请先点击“恢复代理”。"


class CaptureController:
    def __init__(self, backend=None, check_before_capture=False):
        self.backend = backend or engine
        self.check_before_capture = check_before_capture
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.thread = None
        self.busy = False

    def emit(self, event):
        self.events.put(event)

    def start(self, operation, args):
        if self.busy:
            return False
        self.busy = True
        self.cancel_event = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(operation, args), daemon=False)
        self.thread.start()
        return True

    def cancel(self):
        self.cancel_event.set()

    def _run(self, operation, args):
        outcome, result, message = "success", None, "操作完成。"
        try:
            if operation == "capture":
                with exclusive_capture():
                    if self.check_before_capture:
                        self.backend.doctor(args, self.emit)
                        engine.checkpoint(self.cancel_event)
                    result = self.backend.capture(args, self.emit, self.cancel_event)
                message = "捕获成功，环境已恢复。"
            elif operation == "restore":
                with exclusive_capture():
                    restored = self.backend.restore_state(Path(args.state_dir) / "restore.json", self.emit)
                message = "代理和证书已恢复，遗留进程已停止。" if restored else "没有待恢复的记录。"
            elif operation == "install":
                with exclusive_capture():
                    self.backend.install(args, self.emit, self.cancel_event)
                message = "依赖安装完成。"
            elif operation == "doctor":
                self.backend.doctor(args, self.emit)
                message = "环境检查通过，可以开始捕获。"
            else:
                raise ValueError("未知操作。")
        except engine.CaptureCancelled:
            outcome, message = "cancelled", "已取消，清理完成。"
        except TimeoutError as error:
            outcome, message = "timeout", actionable_error(error)
        except Exception as error:
            outcome, message = "error", actionable_error(error)
        finally:
            self.emit({"stage": "done", "operation": operation, "outcome": outcome, "message": message, "result": result})

    def drain(self):
        """Call only on the UI thread; terminal event follows cleanup completion."""
        events = []
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event["stage"] == "done":
                self.busy = False
            events.append(event)
        return events
