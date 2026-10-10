"""Chinese Tk/ttk desktop, queue-driven capture and cooperative window close."""
import argparse
import math
import os
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk
from types import SimpleNamespace
from urllib.parse import urlsplit

from capture_openid import positive_number, valid_port
from core import TARGET_HOST, TARGET_PATH
from desktop_service import CaptureController
from runtime_paths import APP_ROOT

STAGES = {
    "preparing": "准备环境", "starting": "启动代理", "waiting": "等待微信登录",
    "captured": "已捕获，正在清理", "success": "捕获成功", "timeout": "捕获超时",
    "cancelled": "已取消", "cleanup": "恢复与清理", "cleaned": "清理完成",
    "error": "操作失败", "checking": "检查环境", "installing": "安装依赖", "installed": "安装完成",
}
INSTRUCTIONS = "在电脑版微信打开“中南林业科技大学学生工作部”小程序，进入“我的”，退出后重新登录。"
COLORS = {"bg": "#f7f7f7", "card": "#f7f7f7", "text": "#262626", "muted": "#737373", "accent": "#357b75", "soft": "#eeeeee", "log": "#ffffff", "border": "#d8d8d8", "hover": "#e4e4e4", "error": "#a43832"}


class DesktopApp:
    def __init__(self, root, backend=None, base_dir=None, demo=False):
        self.root = root
        self.base_dir = Path(base_dir or APP_ROOT)
        self.state_dir = self.base_dir / (".demo-state" if demo else ".state")
        self.demo = demo
        self.packaged = bool(getattr(sys, "frozen", False))
        self.controller = CaptureController(backend, check_before_capture=self.packaged)
        self.close_requested = False
        self.closed = False
        self.openid = ""
        self.result_dir = None
        self.stage = "idle"
        self.operation = None
        self.port = tk.StringVar(value="8866")
        self.timeout = tk.StringVar(value="120")
        self.mitmdump = tk.StringVar()
        self.upstream = tk.StringVar()
        self.output = tk.StringVar(value=str(self.base_dir / ("demo-output" if demo else "output")))
        self.reveal = tk.BooleanVar()
        self.advanced = tk.BooleanVar()
        self.status = tk.StringVar(value="就绪 · 离线模拟" if demo else "就绪")
        self.remaining = tk.StringVar()
        self.notice = tk.StringVar()
        self.openid_text = tk.StringVar(value="尚未捕获")
        self.root.title("林科大 OpenID 捕获工具" + (" · 离线模拟" if demo else ""))
        self.root.geometry("840x700")
        self.root.minsize(720, 640)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self._build()
        self.apply_style()
        self.notice.trace_add("write", self.update_notice_visibility)
        self.update_notice_visibility()
        self.refresh_recovery()
        self.root.after(60, self.pump)

    def _build(self):
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)
        head = ttk.Frame(root, padding=(20, 16, 20, 10))
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text="林科大 OpenID 捕获工具" + (" · 离线模拟" if self.demo else ""), style="Title.TLabel").grid(row=0, column=0, sticky="w")

        status_card = ttk.Frame(root, padding=(0, 8), style="Card.TFrame")
        status_card.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 8))
        status_card.columnconfigure(0, weight=1)
        ttk.Label(status_card, textvariable=self.status, style="Status.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(status_card, textvariable=self.remaining, style="Card.TLabel").grid(row=0, column=1, sticky="e")
        self.instruction_label = ttk.Label(status_card, text=INSTRUCTIONS, style="Card.TLabel", wraplength=860)
        self.instruction_label.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.instruction_label.grid_remove()

        settings = ttk.Frame(root, padding=(20, 0))
        settings.grid(row=2, column=0, sticky="ew")
        settings.columnconfigure(0, weight=1)
        basic = ttk.Frame(settings)
        basic.grid(row=0, column=0, sticky="ew")
        self.advanced_frame = ttk.Frame(settings, padding=(0, 10, 0, 0))
        self.advanced_frame.grid(row=1, column=0, sticky="ew")
        self.advanced_frame.columnconfigure(1, weight=1)
        self.parameters = ttk.Frame(self.advanced_frame if self.packaged else basic)
        if self.packaged:
            self.parameters.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        else:
            self.parameters.pack(side="left")
        ttk.Label(self.parameters, text="监听端口").pack(side="left")
        self.port_entry = ttk.Entry(self.parameters, textvariable=self.port, width=8)
        self.port_entry.pack(side="left", padx=(8, 24))
        ttk.Label(self.parameters, text="捕获超时（秒）").pack(side="left")
        self.timeout_entry = ttk.Entry(self.parameters, textvariable=self.timeout, width=8)
        self.timeout_entry.pack(side="left", padx=8)
        ttk.Checkbutton(basic, text="设置" if self.packaged else "高级设置", variable=self.advanced, command=self.toggle_advanced).pack(side="right")
        self.inputs = [self.port_entry, self.timeout_entry]
        self.browse_buttons = []
        first_row = 1 if self.packaged else 0
        for row, (title, variable, kind) in enumerate((("mitmdump 路径", self.mitmdump, "exe"), ("上游代理", self.upstream, None), ("输出目录", self.output, "dir")), start=first_row):
            ttk.Label(self.advanced_frame, text=title, width=15).grid(row=row, column=0, sticky="w", pady=3)
            entry = ttk.Entry(self.advanced_frame, textvariable=variable)
            entry.grid(row=row, column=1, sticky="ew", padx=(0, 8), pady=3)
            self.inputs.append(entry)
            if kind:
                button = ttk.Button(self.advanced_frame, text="浏览…", width=8, command=lambda v=variable, k=kind: self.browse(v, k))
                button.grid(row=row, column=2, pady=3)
                self.browse_buttons.append(button)
        self.install_button = None
        self.doctor_button = None
        if not self.packaged:
            diagnostics = ttk.Frame(self.advanced_frame)
            diagnostics.grid(row=first_row + 3, column=0, columnspan=3, sticky="w", pady=(8, 0))
            self.doctor_button = ttk.Button(diagnostics, text="检查环境", command=lambda: self.start("doctor"))
            self.doctor_button.pack(side="left", padx=(0, 8))
            self.install_button = ttk.Button(diagnostics, text="安装依赖", command=lambda: self.start("install"))
            self.install_button.pack(side="left")
        self.advanced_frame.grid_remove()

        actions = ttk.Frame(root, padding=(20, 12, 20, 8))
        actions.grid(row=3, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        buttons = ttk.Frame(actions)
        buttons.grid(row=0, column=0, sticky="w")
        self.start_button = ttk.Button(buttons, text="开始捕获", style="Accent.TButton", command=lambda: self.start("capture"))
        self.cancel_button = ttk.Button(buttons, text="取消捕获", command=self.cancel, state="disabled")
        self.restore_button = ttk.Button(buttons, text="恢复代理", command=lambda: self.start("restore"))
        for column, button in enumerate((self.start_button, self.cancel_button, self.restore_button)):
            button.grid(row=0, column=column, padx=(0, 8))
        self.notice_label = ttk.Label(actions, textvariable=self.notice, style="Notice.TLabel", wraplength=860)
        self.notice_label.grid(row=1, column=0, sticky="ew", pady=(10, 0))

        log_card = ttk.Frame(root, padding=(0, 4), style="Card.TFrame")
        log_card.grid(row=4, column=0, sticky="nsew", padx=20, pady=(0, 8))
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(1, weight=1)
        ttk.Label(log_card, text="运行日志", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.log = tk.Text(log_card, height=5, wrap="word", relief="flat", borderwidth=0, highlightthickness=1, font=("Microsoft YaHei UI", 10), state="disabled", padx=8, pady=8)
        self.log.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_card, command=self.log.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

        result = ttk.Frame(root, padding=(0, 10, 0, 0), style="Card.TFrame")
        result.grid(row=5, column=0, sticky="ew", padx=20, pady=(0, 10))
        result.columnconfigure(0, weight=1)
        ttk.Separator(result).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Label(result, text="OpenID", style="Card.TLabel").grid(row=1, column=0, sticky="w")
        self.reveal_button = ttk.Checkbutton(result, text="显示", variable=self.reveal, command=self.update_openid, state="disabled")
        self.reveal_button.grid(row=1, column=1, sticky="e")
        self.result_entry = ttk.Entry(result, textvariable=self.openid_text, state="readonly")
        self.result_entry.grid(row=2, column=0, columnspan=2, sticky="ew", pady=8)
        result_buttons = ttk.Frame(result, style="Card.TFrame")
        result_buttons.grid(row=3, column=0, columnspan=2, sticky="w")
        self.copy_button = ttk.Button(result_buttons, text="复制 OpenID", command=self.copy_openid, state="disabled")
        self.copy_button.pack(side="left", padx=(0, 8))
        self.folder_button = ttk.Button(result_buttons, text="打开结果目录", command=self.open_folder, state="disabled")
        self.folder_button.pack(side="left")
        footer = ttk.Frame(root, padding=(20, 0, 20, 12))
        footer.grid(row=6, column=0, sticky="ew")
        self.privacy_label = ttk.Label(footer, text="捕获时临时修改代理和信任根证书，结束后恢复；保持 HTTPS 校验。", style="Muted.TLabel", wraplength=860)
        self.privacy_label.pack(fill="x")
        root.bind("<Configure>", self.resize_wrap)

    def resize_wrap(self, event):
        if event.widget == self.root:
            width = max(500, event.width - 40)
            for label in (self.instruction_label, self.notice_label, self.privacy_label):
                label.configure(wraplength=width)

    def update_notice_visibility(self, *_):
        if self.notice.get():
            self.notice_label.grid()
        else:
            self.notice_label.grid_remove()

    def apply_style(self):
        p = COLORS
        self.root.configure(bg=p["bg"])
        s = self.style
        s.configure(".", font=("Microsoft YaHei UI", 10), background=p["bg"], foreground=p["text"])
        s.configure("TFrame", background=p["bg"])
        s.configure("TLabel", background=p["bg"], foreground=p["text"])
        s.configure("Card.TFrame", background=p["card"])
        s.configure("Card.TLabel", background=p["card"], foreground=p["text"])
        s.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        s.configure("Status.TLabel", background=p["card"], foreground=p["text"], font=("Microsoft YaHei UI", 10, "bold"))
        s.configure("Muted.TLabel", foreground=p["muted"], font=("Microsoft YaHei UI", 9))
        s.configure("Notice.TLabel", foreground=p["muted"], font=("Microsoft YaHei UI", 9))
        s.configure("Error.TLabel", foreground=p["error"], font=("Microsoft YaHei UI", 9))
        s.configure("TButton", background=p["soft"], foreground=p["text"], padding=(10, 5), borderwidth=1, bordercolor=p["border"], lightcolor=p["soft"], darkcolor=p["soft"])
        s.map("TButton", background=[("disabled", p["soft"]), ("active", p["hover"])], foreground=[("disabled", p["muted"]), ("active", p["text"])])
        s.configure("Accent.TButton", background=p["accent"], foreground="white")
        s.map("Accent.TButton", background=[("disabled", p["soft"]), ("active", p["accent"])], foreground=[("disabled", p["muted"]), ("active", "white")])
        s.configure("TEntry", fieldbackground=p["card"], foreground=p["text"], insertcolor=p["text"], padding=4)
        s.map("TEntry", fieldbackground=[("readonly", p["log"]), ("disabled", p["soft"])], foreground=[("readonly", p["text"])])
        s.configure("TCheckbutton", background=p["bg"], foreground=p["text"])
        s.map("TCheckbutton", background=[("active", p["soft"])])
        s.configure("TSeparator", background=p["border"])
        self.log.configure(bg=p["log"], fg=p["text"], insertbackground=p["text"], selectbackground=p["accent"], highlightbackground=p["border"], highlightcolor=p["border"])

    def toggle_advanced(self):
        if self.advanced.get():
            self.advanced_frame.grid()
            self.root.minsize(720, 790)
            if self.root.winfo_height() < 790:
                self.root.geometry(f"{self.root.winfo_width()}x790")
        else:
            self.advanced_frame.grid_remove()
            self.root.minsize(720, 640)

    def browse(self, variable, kind):
        value = filedialog.askdirectory(parent=self.root, title="选择输出目录") if kind == "dir" else filedialog.askopenfilename(parent=self.root, title="选择 mitmdump.exe", filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        if value:
            variable.set(value)

    def arguments(self, operation):
        # Recovery and installation must remain available even with invalid form fields.
        if operation in ("restore", "install"):
            return SimpleNamespace(state_dir=self.state_dir, index_url=None)
        port = valid_port(self.port.get().strip())
        timeout = positive_number(self.timeout.get().strip())
        upstream = self.upstream.get().strip()
        if upstream:
            parsed = urlsplit(upstream)
            try:
                valid = parsed.scheme in ("http", "https") and parsed.hostname and parsed.port and not parsed.query and not parsed.fragment
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("上游代理须为 http(s)://主机:端口，例如 http://127.0.0.1:7890。")
        directory = self.output.get().strip()
        if not directory:
            raise ValueError("请选择输出目录。")
        return SimpleNamespace(port=port, timeout=timeout, mitmdump=self.mitmdump.get().strip() or None, upstream=upstream or None, output=Path(directory).expanduser() / "openid.json", state_dir=self.state_dir, manual_proxy=False, skip_cert_install=False, env_file=None, upstream_ca=None, target_host=TARGET_HOST, target_path=TARGET_PATH)

    def start(self, operation):
        if self.controller.busy or self.close_requested:
            return
        if operation == "install" and self.packaged:
            self.notice.set("发行版已内置依赖。运行组件缺失时，请重新解压完整发行包或在高级设置指定 mitmdump。")
            return
        if operation == "doctor" and self.packaged:
            self.notice.set("点击“开始捕获”会自动检查环境，无需单独检查。")
            return
        self.notice_label.configure(style="Notice.TLabel")
        try:
            args = self.arguments(operation)
            if operation == "capture" and (self.state_dir / "restore.json").exists():
                raise RuntimeError("发现遗留恢复记录，请先点击“恢复代理”。旧备份不会被覆盖。")
        except (ValueError, RuntimeError, argparse.ArgumentTypeError) as error:
            self.notice_label.configure(style="Error.TLabel")
            self.notice.set(f"设置错误：{error}")
            self.append_log(self.notice.get())
            return
        if self.controller.start(operation, args):
            self.operation = operation
            self.notice.set("")
            self.status.set({"capture": "准备捕获", "restore": "恢复环境", "doctor": "检查环境", "install": "安装依赖"}[operation])
            self.remaining.set("")
            self.instruction_label.grid_remove()
            if operation == "capture":
                self.openid = ""
                self.result_dir = None
                self.reveal.set(False)
                self.update_openid()
            self.set_busy(True)

    def set_busy(self, busy):
        for button in (self.start_button, self.restore_button, self.doctor_button, self.install_button, *self.browse_buttons):
            if button is not None:
                button.configure(state="disabled" if busy else "normal")
        for entry in self.inputs:
            entry.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy and self.operation in ("capture", "install") and not self.close_requested else "disabled")
        self.copy_button.configure(state="normal" if self.openid and not busy else "disabled")
        self.folder_button.configure(state="normal" if self.result_dir and not busy else "disabled")
        self.reveal_button.configure(state="normal" if self.openid and not busy else "disabled")

    def cancel(self):
        if self.controller.busy:
            self.controller.cancel()
            self.status.set("取消中 · 等待清理")
            self.notice.set("")
            self.cancel_button.configure(state="disabled")

    def on_close(self):
        if self.closed:
            return
        if self.controller.busy:
            self.close_requested = True
            self.controller.cancel()
            self.status.set("关闭中 · 等待清理")
            self.notice.set("")
            self.cancel_button.configure(state="disabled")
        else:
            self.destroy()

    def destroy(self):
        self.closed = True
        self.root.destroy()

    def refresh_recovery(self):
        if (self.state_dir / "restore.json").exists():
            self.notice_label.configure(style="Error.TLabel")
            self.status.set("需要恢复上次捕获")
            self.notice.set("检测到遗留恢复记录，请先点击“恢复代理”。旧备份不会被覆盖。")
            self.start_button.configure(state="disabled")

    def append_log(self, message):
        if not message:
            return
        # No record payload is ever routed here; also mask known ID defensively.
        if self.openid:
            message = message.replace(self.openid, "[OpenID 已隐藏]")
        self.log.configure(state="normal")
        self.log.insert("end", f"{datetime.now():%H:%M:%S}  {message}\n")
        if int(self.log.index("end-1c").split(".")[0]) > 1200:
            self.log.delete("1.0", "200.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def handle_event(self, event):
        stage = event["stage"]
        self.stage = stage
        if stage != "waiting":
            self.instruction_label.grid_remove()
            self.remaining.set("")
        if stage == "done":
            result = event.get("result")
            if result:
                self.openid = result["openid"]
                self.result_dir = Path(self.output.get()).expanduser().resolve()
                self.reveal.set(False)
                self.update_openid()
            self.status.set({"success": "操作完成", "cancelled": "已取消 · 清理完成", "timeout": "捕获超时 · 清理完成", "error": "操作失败"}[event["outcome"]])
            if result:
                self.status.set("捕获成功 · 环境已恢复")
            self.notice.set(event["message"] if event["outcome"] in ("error", "timeout") else "")
            self.notice_label.configure(style="Error.TLabel" if event["outcome"] in ("error", "timeout") else "Notice.TLabel")
            self.append_log(event["message"])
            self.set_busy(False)
            recovery_pending = (self.state_dir / "restore.json").exists()
            self.refresh_recovery()
            if self.close_requested:
                if recovery_pending:
                    self.close_requested = False
                    self.notice.set("清理失败，恢复记录已保留。请点击“恢复代理”重试；详情见运行日志。")
                else:
                    self.destroy()
            return
        self.status.set(STAGES.get(stage, stage))
        if stage == "waiting":
            self.instruction_label.grid()
            self.remaining.set(f"剩余等待：{math.ceil(event.get('remaining', 0))} 秒")
            self.notice.set("")
        self.append_log(event.get("message", ""))

    def pump(self):
        if self.closed:
            return
        for event in self.controller.drain():
            self.handle_event(event)
            if self.closed:
                return
        self.root.after(60, self.pump)

    def update_openid(self):
        self.openid_text.set((self.openid if self.reveal.get() else "●" * 28) if self.openid else "尚未捕获")

    def copy_openid(self):
        if self.openid:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.openid)
            self.notice.set("已复制。")

    def open_folder(self):
        if self.result_dir and self.result_dir.is_dir():
            try:
                os.startfile(str(self.result_dir))
            except OSError:
                self.notice.set(f"无法打开目录，请手动打开：{self.result_dir}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="林科大 OpenID 桌面工具")
    parser.add_argument("--demo", action="store_true", help="离线模拟，不访问真实代理和证书库")
    parser.add_argument("--demo-outcome", choices=("success", "timeout", "error"), default="success")
    parser.add_argument("--demo-delay", type=positive_number, default=3)
    parser.add_argument("--demo-dir", type=Path, help="隔离的模拟输出与状态目录")
    parser.add_argument("--verify-demo", action="store_true", help="仅离线模式：自动验证真实窗口事件循环并保存报告")
    args = parser.parse_args(argv)
    if args.verify_demo and not args.demo:
        parser.error("--verify-demo 必须与 --demo 一起使用。")
    backend = None
    if args.demo:
        from offline_demo import OfflineBackend
        backend = OfflineBackend(args.demo_outcome, args.demo_delay)
    if os.name == "nt":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    root = tk.Tk()
    app = DesktopApp(root, backend=backend, base_dir=args.demo_dir if args.demo else None, demo=args.demo)
    if args.verify_demo:
        from demo_verify import run
        run(app)
    root.mainloop()


if __name__ == "__main__":
    main()
