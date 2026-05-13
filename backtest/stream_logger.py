import os
import queue
import sys
import threading
from datetime import datetime

_log_emit_lock = threading.Lock()
# 设置环境变量 QUANT_APP_LOG=logs/app.log 后，所有经 StreamLogger 的行会追加到该文件（适合后台跑服务时 tail）
_LOG_FILE_PATH = os.environ.get("QUANT_APP_LOG", "logs/quant_app.log").strip()


class StreamLogger:
    """
    线程安全队列 + 终端输出；已注册线程的 stdout 会经此模块写出，
    避免在 Windows / 多线程下子线程 print 不进控制台的问题。
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.log_queues = {}
            cls._instance.global_queue = queue.Queue()
            cls._instance.lock = threading.Lock()
        return cls._instance

    def register_thread(self):
        tid = threading.get_ident()
        with self.lock:
            if tid not in self.log_queues:
                self.log_queues[tid] = queue.Queue()
            else:
                with self.log_queues[tid].mutex:
                    self.log_queues[tid].queue.clear()

    def unregister_thread(self):
        tid = threading.get_ident()
        with self.lock:
            self.log_queues.pop(tid, None)

    def log(self, message: str):
        # 确保 message 是字符串
        if isinstance(message, bytes):
            try:
                message = message.decode('utf-8', errors='replace')
            except Exception:
                message = repr(message)
        elif not isinstance(message, str):
            message = str(message)

        tid = threading.get_ident()
        msg = message.rstrip()
        if not msg:
            return

        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_msg = f"[{timestamp}] {msg}"

        if tid in self.log_queues:
            self.log_queues[tid].put(formatted_msg)

        self.global_queue.put(formatted_msg)

        line = formatted_msg + "\n"
        # 控制台：优先 stderr（IDE/子线程/后台任务上往往比 stdout 更可靠），失败再写 UTF-8 stdout
        try:
            sys.__stderr__.write(line)
            sys.__stderr__.flush()
        except Exception:
            try:
                out = getattr(sys.stdout, "terminal", None) or sys.__stdout__
                if out and not getattr(out, "closed", False):
                    out.write(line)
                    out.flush()
            except Exception:
                pass
        # 可选文件：设置 QUANT_APP_LOG=logs/app.log，后台跑服务时可用 tail -f 看回测日志
        if _LOG_FILE_PATH:
            try:
                with _log_emit_lock:
                    log_dir = os.path.dirname(_LOG_FILE_PATH)
                    if log_dir:
                        os.makedirs(log_dir, exist_ok=True)
                    with open(_LOG_FILE_PATH, "a", encoding="utf-8", errors="replace") as fp:
                        fp.write(line)
            except Exception:
                pass

    def get_messages(self):
        while True:
            try:
                msg = self.global_queue.get(timeout=1.0)
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield ": keep-alive\n\n"

    def get_thread_messages(self, timeout=0.1):
        tid = threading.get_ident()
        if tid not in self.log_queues:
            return []
        messages = []
        try:
            while True:
                messages.append(self.log_queues[tid].get_nowait())
        except queue.Empty:
            pass
        return messages


class ThreadAwareStdout:
    """已 register 的线程：write 走 StreamLogger；未注册则直接写 terminal。"""

    def __init__(self, terminal=None):
        self.logger = StreamLogger()
        self.terminal = terminal if terminal is not None else sys.__stdout__
        self._closed = False

    def write(self, message):
        if self._closed or not message:
            return

        # 确保 message 是字符串（处理 bytes 输入）
        if isinstance(message, bytes):
            try:
                message = message.decode('utf-8', errors='replace')
            except Exception:
                # 如果解码失败，使用 repr 作为备选
                message = repr(message)
        elif not isinstance(message, str):
            # 其他非字符串类型转换为字符串
            message = str(message)

        current_tid = threading.get_ident()
        is_registered = current_tid in self.logger.log_queues

        try:
            if is_registered:
                self.logger.log(message)
            else:
                if self.terminal and not getattr(self.terminal, "closed", False):
                    self.terminal.write(message)
                    self.terminal.flush()
        except Exception as e:
            try:
                sys.__stderr__.write(f"[stream_logger] write error: {e}\n")
            except Exception:
                pass

    def flush(self):
        if self._closed:
            return
        try:
            if self.terminal and not getattr(self.terminal, "closed", False):
                self.terminal.flush()
        except Exception:
            self._closed = True

    def close(self):
        self._closed = True

    def isatty(self):
        try:
            return self.terminal.isatty()
        except Exception:
            return False


stream_logger = StreamLogger()


def start_capture():
    with stream_logger.global_queue.mutex:
        stream_logger.global_queue.queue.clear()
    stream_logger.register_thread()


def stop_capture():
    stream_logger.unregister_thread()
