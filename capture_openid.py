"""Standalone CSUFT OpenID capture CLI. Python 3.12+, Node.js not required."""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import threading
from pathlib import Path

from core import TARGET_HOST, TARGET_PATH, atomic_write, save_env
from runtime_paths import APP_ROOT, RESOURCE_ROOT
from windows_proxy import (
    capture_proxy, cert_is_trusted, cert_thumbprint, certutil,
    exclusive_capture, process_identity, read_proxy, require_windows,
    stop_saved_process, write_proxy,
)

ROOT = APP_ROOT


class CaptureCancelled(Exception):
    """Cooperative cancellation; capture's finally always performs recovery."""


def emit(callback, stage, message, **details):
    if callback:
        # Observers must never interrupt system recovery.
        try:
            callback({"stage": stage, "message": message, **details})
        except Exception:
            pass
    elif message:
        print(message, flush=True)


def checkpoint(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise CaptureCancelled("捕获已取消。")


def pause(cancel_event, seconds=0.2):
    if cancel_event is None:
        time.sleep(seconds)
    elif cancel_event.wait(seconds):
        checkpoint(cancel_event)


def dependency_guidance():
    if getattr(sys, "frozen", False):
        return "请重新完整解压发行包，或在“设置”中指定可用的 mitmdump。"
    return "请在高级设置检查环境或安装依赖（mitmproxy 12.2.3），也可运行 install.cmd。"


def locate_mitmdump(explicit=None):
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if not candidate.is_file():
            raise RuntimeError(f"找不到 mitmdump：{candidate}。" + dependency_guidance())
        return str(candidate)
    name = "mitmdump.exe" if os.name == "nt" else "mitmdump"
    for candidate in (ROOT / "runtime" / "mitmdump" / name, ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / name, Path(sys.executable).parent / name):
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError("找不到 mitmproxy 运行组件。" + dependency_guidance())


def external_python():
    candidates = [] if getattr(sys, "frozen", False) else [[sys.executable]]
    for name, suffix in (("py", ["-3"]), ("python", [])):
        found = shutil.which(name)
        if found:
            candidates.append([found, *suffix])
    for command in candidates:
        try:
            result = subprocess.run([*command, "-c", "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)"], capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode == 0:
                return command
        except (OSError, subprocess.TimeoutExpired):
            continue
    raise RuntimeError("未找到 Python 3.12+。请安装 Python 并勾选添加到 PATH，再重试安装；或在高级设置指定现有 mitmdump.exe。完整发行包已内置依赖。")


def install(args, status_callback=None, cancel_event=None):
    checkpoint(cancel_event)
    environment = ROOT / ".venv"
    emit(status_callback, "installing", "正在创建工具专用 Python 环境……")
    creator = external_python()
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--only-binary=:all:", "--progress-bar", "off", "-r", str(RESOURCE_ROOT / "requirements.txt")]
    if args.index_url:
        command.extend(["--index-url", args.index_url])
    for current in ([*creator, "-m", "venv", str(environment)], command):
        checkpoint(cancel_event)
        process = subprocess.Popen(current, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        lines = []
        def read_lines():
            for line in process.stdout:
                # pip progress has no capture payload; URLs may contain credentials.
                safe = re.sub(r"https?://\S+", "[下载地址]", line.strip())
                lines.append(safe)
                emit(status_callback, "installing", safe)
        reader = threading.Thread(target=read_lines, daemon=True)
        reader.start()
        try:
            while process.poll() is None:
                pause(cancel_event)
            reader.join(timeout=5)
            if process.returncode:
                raise RuntimeError("依赖安装失败。请检查网络、Python 版本和 HTTPS pip 源；勿关闭证书校验。\n" + "\n".join(lines[-8:]))
        finally:
            process_stop(process)
            reader.join(timeout=5)
            process.stdout.close()
    emit(status_callback, "installed", "依赖安装完成，可以开始捕获。")


def port_available(port):
    with socket.socket() as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            raise RuntimeError(f"端口 {port} 已被占用；请停止网页中的捕获或用 --port 指定其它端口。") from None


def doctor(args, status_callback=None):
    emit(status_callback, "checking", "只读检查：依赖、端口与恢复记录。")
    state_dir = getattr(args, "state_dir", ROOT / ".state")
    if (state_dir / "restore.json").exists():
        raise RuntimeError(f"发现遗留恢复记录：{state_dir / 'restore.json'}。请先恢复代理；不会覆盖旧备份。")
    executable = locate_mitmdump(args.mitmdump)
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("mitmdump 无法运行或检查超时。" + dependency_guidance()) from None
    if result.returncode or "12.2.3" not in result.stdout:
        raise RuntimeError("mitmdump 版本不匹配或启动失败。本工具验证版本为 12.2.3。" + dependency_guidance())
    port_available(args.port)
    emit(status_callback, "checking", f"mitmdump 12.2.3 可运行：{executable}；端口 {args.port} 可用。")


def process_stop(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def restore_state(state_file, status_callback=None):
    """Restore exact registry values, then remove only this run's newly trusted CA."""
    if not state_file.exists():
        return False
    state = json.loads(state_file.read_text(encoding="utf-8"))
    errors = []
    if state.get("proxy") is not None:
        try:
            write_proxy(state["proxy"])
            emit(status_callback, "cleanup", "已恢复原系统代理。")
        except Exception as error:
            errors.append(f"恢复代理失败：{error}")
    if state.get("new_certificate"):
        try:
            if cert_is_trusted(state["new_certificate"]):
                certutil("-delstore", "Root", state["new_certificate"])
            emit(status_callback, "cleanup", "已移除本次新增的根证书。")
        except Exception as error:
            errors.append(f"清理证书失败：{error}")
    try:
        stop_saved_process(state.get("process"))
    except Exception as error:
        errors.append(f"关闭代理进程失败：{error}")
    if errors:
        raise RuntimeError("\n".join(errors) + "\n恢复备份已保留，请运行 restore 命令重试。")
    state_file.unlink()
    return True


def capture(args, status_callback=None, cancel_event=None):
    emit(status_callback, "preparing", "准备环境：检查依赖、监听端口与恢复记录。")
    checkpoint(cancel_event)
    if args.output.suffix.lower() != ".json":
        raise RuntimeError("--output 必须使用 .json 文件名；工具会另存同名 .txt 文件。")
    if not args.manual_proxy or not args.skip_cert_install:
        require_windows()
    executable = locate_mitmdump(args.mitmdump)
    state_dir = args.state_dir.resolve()
    for destination in (args.output, args.env_file):
        if destination and destination.resolve().is_relative_to(state_dir):
            raise RuntimeError("结果文件和 ENV 文件不能位于恢复状态目录内，请选择其它输出目录以保护恢复记录。")
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "restore.json"
    if state_file.exists():
        raise RuntimeError(f"发现未恢复的上次捕获：{state_file}。请先运行 restore 命令（使用同一个 --state-dir）。")
    port_available(args.port)
    original = None if args.manual_proxy else read_proxy()
    result_file, ready_file = state_dir / "capture.json", state_dir / "ready"
    for item in (result_file, ready_file):
        item.unlink(missing_ok=True)
    state = {"proxy": original, "new_certificate": None, "process": None}
    process = None
    cleanup_error = None
    record = None
    sibling_python = Path(executable).parent / "python.exe"
    # Windows pip launchers may spawn a second process. Start Python directly so
    # the PID we manage is the proxy itself, including during recovery.
    launcher = [str(sibling_python), "-c", "from mitmproxy.tools.main import mitmdump; mitmdump()"] if os.name == "nt" and sibling_python.is_file() else [executable]
    command = [*launcher, "-q", "-s", str(RESOURCE_ROOT / "addon.py"), "--listen-host", "127.0.0.1", "--listen-port", str(args.port), "--set", f"confdir={state_dir / 'ca'}", "--set", f"allow_hosts=^{re.escape(args.target_host)}(?::[0-9]+)?$"]
    if args.upstream:
        command.extend(["--mode", "upstream:" + args.upstream])
    if args.upstream_ca:
        command.extend(["--set", f"ssl_verify_upstream_trusted_ca={args.upstream_ca.resolve()}"])
    environment = {**os.environ, "OPENID_TARGET_HOST": args.target_host, "OPENID_TARGET_PATH": args.target_path, "OPENID_RESULT_FILE": str(result_file), "OPENID_READY_FILE": str(ready_file)}
    emit(status_callback, "preparing", "捕获期间临时修改代理并信任根证书，结束后恢复；保持 HTTPS 证书校验。" if not args.manual_proxy and not args.skip_cert_install else "使用手动代理/证书模式。")
    try:
        checkpoint(cancel_event)
        # Persist the recovery journal BEFORE any proxy or trust-store mutation.
        atomic_write(state_file, json.dumps(state, ensure_ascii=False, indent=2))
        with (state_dir / "mitmdump.log").open("w", encoding="utf-8") as log:
            # Never persist proxy output: errors can include full URLs, codes or bodies.
            log.write("隐私模式：不保存代理原始输出。启动失败时请检查路径、端口和依赖版本。\n")
            emit(status_callback, "starting", "启动本地代理，等待证书与监听就绪……")
            process = subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            state["process"] = process_identity(process.pid)
            atomic_write(state_file, json.dumps(state, ensure_ascii=False, indent=2))
            deadline = time.monotonic() + 20
            certificate = state_dir / "ca" / "mitmproxy-ca-cert.pem"
            while not (ready_file.exists() and certificate.exists()):
                checkpoint(cancel_event)
                if process.poll() is not None:
                    raise RuntimeError(f"mitmdump 启动失败（退出码 {process.returncode}）。请检查设置中的程序路径和端口占用。" + dependency_guidance())
                if time.monotonic() >= deadline:
                    raise RuntimeError("mitmdump 启动超过 20 秒。请检查设置中的程序路径与端口。" + dependency_guidance())
                pause(cancel_event)
            checkpoint(cancel_event)
            if not args.skip_cert_install:
                thumbprint = cert_thumbprint(certificate)
                if not cert_is_trusted(thumbprint):
                    state["new_certificate"] = thumbprint
                    atomic_write(state_file, json.dumps(state, ensure_ascii=False, indent=2))
                    certutil("-addstore", "Root", str(certificate))
            if original is not None:
                checkpoint(cancel_event)
                write_proxy(capture_proxy(original, args.port))
            emit(status_callback, "waiting", f"正在监听 127.0.0.1:{args.port}，等待 {args.timeout:g} 秒。", remaining=args.timeout)
            emit(status_callback, "waiting", "请在电脑版微信打开“中南林业科技大学学生工作部”小程序，进入“我的”，退出后重新登录。", remaining=args.timeout)
            deadline = time.monotonic() + args.timeout
            last_remaining = None
            while time.monotonic() < deadline:
                checkpoint(cancel_event)
                remaining = max(0, int(deadline - time.monotonic() + 0.999))
                if remaining != last_remaining:
                    emit(status_callback, "waiting", "", remaining=remaining)
                    last_remaining = remaining
                if result_file.exists():
                    record = json.loads(result_file.read_text(encoding="utf-8"))
                    break
                if process.poll() is not None:
                    raise RuntimeError(f"代理进程提前退出（退出码 {process.returncode}）。请检查设置中的端口和上游代理配置。" + dependency_guidance())
                pause(cancel_event)
            if record is None:
                raise TimeoutError("捕获超时：未收到有效的 getOpenidByJsCode 响应。确认微信已重新登录且使用本机系统代理。")
            checkpoint(cancel_event)
            output = args.output.resolve()
            atomic_write(output, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
            atomic_write(output.with_suffix(".txt"), record["openid"] + "\n")
            if args.env_file:
                save_env(args.env_file.resolve(), record["openid"])
            emit(status_callback, "captured", f"捕获成功，结果已保存到 {output}（OpenID 已隐藏）。")
    except CaptureCancelled:
        emit(status_callback, "cancelled", "捕获已取消，正在恢复环境。")
        raise
    except TimeoutError:
        emit(status_callback, "timeout", "捕获超时，正在恢复环境。")
        raise
    except Exception:
        emit(status_callback, "error", "捕获失败，正在恢复环境。")
        raise
    finally:
        emit(status_callback, "cleanup", "清理：恢复代理、移除本次新增证书并停止子进程……")
        # Restore routing while the proxy is still alive; never strand it at a dead port.
        try:
            restore_state(state_file, status_callback)
        except Exception as error:
            cleanup_error = error
        finally:
            process_stop(process)
            result_file.unlink(missing_ok=True)
            ready_file.unlink(missing_ok=True)
        if cleanup_error:
            raise cleanup_error
        emit(status_callback, "cleaned", "本次捕获的环境清理完成。")
    emit(status_callback, "success", "捕获成功，环境已恢复。")
    return record


def positive_number(value):
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("捕获超时必须是有限的正数（秒）。") from None
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("必须是有限的正数。")
    return number


def valid_port(value):
    try:
        port = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("监听端口必须是 1–65535 的整数。") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口范围为 1–65535。")
    return port


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="独立 Python 工具：通过微信登录响应捕获 CSUFT OpenID。")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("install", help="安装独立 .venv 中的 mitmproxy")
    setup.add_argument("--index-url", help="自定义 HTTPS pip 源，保持证书校验")
    run = commands.add_parser("capture", help="捕获一次 OpenID")
    run.add_argument("--port", type=valid_port, default=8866)
    run.add_argument("--timeout", type=positive_number, default=120)
    run.add_argument("--mitmdump", help="指定已有的 mitmdump 可执行文件")
    run.add_argument("--output", type=Path, default=ROOT / "output" / "openid.json")
    run.add_argument("--env-file", type=Path, help="可选：追加账号到指定 .env，不覆盖其它账号")
    run.add_argument("--state-dir", type=Path, default=ROOT / ".state")
    run.add_argument("--target-host", default=TARGET_HOST, help="目标主机名，用于接口变更或本地测试")
    run.add_argument("--target-path", default=TARGET_PATH)
    run.add_argument("--manual-proxy", action="store_true", help="不读写系统代理，由使用者手动配置")
    run.add_argument("--skip-cert-install", action="store_true", help="不安装/移除根证书，由使用者配置证书")
    run.add_argument("--upstream", help="可选上游 HTTP 代理，例如 http://127.0.0.1:7890")
    run.add_argument("--upstream-ca", type=Path, help="可选：校验上游服务器的 PEM CA 文件（不关闭 TLS 校验）")
    recover = commands.add_parser("restore", help="异常终止后从备份恢复代理和根证书")
    recover.add_argument("--state-dir", type=Path, default=ROOT / ".state")
    doctor_parser = commands.add_parser("doctor", help="只读检查 Python、mitmdump 和端口")
    doctor_parser.add_argument("--mitmdump")
    doctor_parser.add_argument("--port", type=valid_port, default=8866)
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            with exclusive_capture():
                install(args)
        elif args.command == "doctor":
            print(f"Python：{sys.version.split()[0]}")
            doctor(args)
        else:
            with exclusive_capture():
                if args.command == "capture":
                    capture(args)
                elif not restore_state(args.state_dir.resolve() / "restore.json"):
                    print("没有待恢复的捕获记录。")
        return 0
    except (KeyboardInterrupt, CaptureCancelled):
        print("捕获已取消。若出现清理错误或曾强制结束进程，请运行 restore。", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
