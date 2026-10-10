"""Frozen console child; never modifies OS proxy or trusts certificates itself."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from mitmproxy.tools.main import mitmdump
    mitmdump()
