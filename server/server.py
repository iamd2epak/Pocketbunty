#!/usr/bin/env python3
"""PocketBunty bridge server. Runs inside Termux at ~/.pocketbunty/server.py (hidden folder).

Listens on 127.0.0.1:8765 only. Every request needs the X-Token header.

  GET /health                -> {"status": "ok", "version": 2}
  GET /files?path=/some/dir  -> directory listing (read only)
  GET /stats                 -> {"sessions": n, "rss_kb": total memory of all shells}
  WS  /ws?session=ID&since=N -> one persistent bash (pty) per session ID
      client -> {"cmd": "..."} | {"input": "..."} | {"signal": "SIGINT"}
                | {"resize": {"cols": 80, "rows": 40}} | {"exit": true}
      server -> {"output": "...", "end": N}   (N = output offset, used to resume)
  WS  /run?job=ID            -> one command without a terminal (used by the Assistant tab)
      client -> {"start": {"cmd": "...", "prompt": "...", "session": "...", "cwd": "~"}} | {"stop": true}
      server -> {"out": "...", "fd": 1|2} ... then {"exit": code} | {"error": "..."}
      The job keeps running when the app disconnects; reconnecting replays all its output.
"""

import asyncio
import codecs
import fcntl
import hmac
import os
import pty
import re
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import termios

from aiohttp import WSMsgType, web

VERSION = 8  # 7: hidden ~/.pocketbunty folder; 8: `pocketbunty` reconnect command
HOST = "127.0.0.1"  # never 0.0.0.0
PORT = 8765
BRIDGE_DIR = os.path.expanduser("~/.pocketbunty")
TOKEN_FILE = os.path.join(BRIDGE_DIR, "token")
HISTORY_CHARS = 256 * 1024  # scrollback kept per session for reconnects
MAX_ENTRIES = 10000
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
IDLE_OK = set("\x03\x04\x1a\x1c\r\n")  # control keys that still make sense at the prompt
READY = "__TC_READY__"
JOB_OUTPUT_CHARS = 2 * 1024 * 1024  # output kept per assistant job
JOB_KEEP_SECONDS = 15 * 60  # finished jobs are kept this long so the app can still read them  # printed once bash is set up; startup noise before it is dropped


def load_or_create_token():
    os.makedirs(BRIDGE_DIR, exist_ok=True)
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
        if token:
            return token, False
    token = secrets.token_urlsafe(24)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token, True


def acquire_wake_lock():
    if shutil.which("termux-wake-lock"):
        subprocess.Popen(["termux-wake-lock"])
        print("Wake lock acquired (termux-wake-lock).")
    else:
        print("termux-wake-lock not found; server may stop when Termux is closed.")


def reap(pid, tries=50):
    """Collect an exited child without blocking the event loop."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return
    if done == 0 and tries > 0:
        asyncio.get_running_loop().call_later(0.2, reap, pid, tries - 1)


class Shell:
    """One persistent bash process attached to a pty."""

    def __init__(self, cols=80, rows=40):
        self.pid = None
        self.fd = None
        self.cols, self.rows = cols, rows
        self.clients = set()  # one asyncio.Queue per connected websocket
        self.decoder = None
        self.history = ""
        self.total = 0  # number of chars ever produced
        self.closed = False
        self.ready = False
        self.pending = ""
        self.program = None  # name of the running program; None while bash waits for a command
        self.poll_handle = None
        self.prompt_attrs = None  # cooked mode, echo off: restored at every prompt

    def start(self):
        pid, fd = pty.fork()
        if pid == 0:  # child: becomes bash
            try:
                attrs = termios.tcgetattr(0)
                attrs[3] &= ~termios.ECHO  # the app already shows the typed command
                termios.tcsetattr(0, termios.TCSANOW, attrs)
                env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
                os.chdir(os.path.expanduser("~"))
                os.execvpe("bash", ["bash", "--login", "--noediting"], env)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.ready, self.pending = False, ""
        try:  # also turn echo off from this side; the child may not have run yet
            attrs = termios.tcgetattr(fd)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            self.prompt_attrs = attrs
        except termios.error:
            pass
        self.resize(self.cols, self.rows)
        loop = asyncio.get_running_loop()
        loop.add_reader(fd, self._on_readable)
        loop.call_later(5, self._mark_ready)
        if self.poll_handle is None:
            self._poll_program()
        # Empty prompts: the app shows its own "$" prompt and command bubbles.
        # \137 is "_", so the marker only appears in the printf output, not the command.
        self.write("PS1='' PS2=''; printf '\\137\\137TC_READY\\137\\137\\n'\n")

    def _poll_program(self):
        """Tell clients when a program starts or stops (they switch to live typing)."""
        self.poll_handle = None
        if self.closed:
            return
        program = None
        try:
            pgid = os.tcgetpgrp(self.fd)
            if pgid != self.pid:
                try:
                    with open(f"/proc/{pgid}/comm") as f:
                        program = f.read().strip() or "program"
                except OSError:
                    program = "program"
        except OSError:
            pass
        if program != self.program:
            self.program = program
            if program is None:
                # Back at the prompt: cooked mode, no echo (a crashed program may leave raw mode on).
                self.restore_prompt_mode()
            else:
                # Simple programs (cat, read) need echo; full apps set their own mode.
                self.set_echo(True)
            for q in self.clients:
                q.put_nowait(("state", program))
        self.poll_handle = asyncio.get_running_loop().call_later(0.3, self._poll_program)

    def _mark_ready(self):
        if not self.ready and not self.closed:
            self.ready = True
            self._emit(self.pending)
            self.pending = ""

    def _on_readable(self):
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            data = b""
        if not data:
            self._on_exit()
            return
        text = self.decoder.decode(data)
        if self.ready:
            self._emit(text)
            return
        self.pending += text
        i = self.pending.find(READY)
        if i >= 0:
            rest = self.pending[i + len(READY):]
            self.ready, self.pending = True, ""
            self._emit(rest.lstrip("\r\n"))

    def _detach(self):
        try:
            asyncio.get_running_loop().remove_reader(self.fd)
        except (ValueError, OSError):
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        reap(self.pid)

    def _on_exit(self):
        self._detach()
        if self.closed:
            return
        self._emit("\r\n[shell exited, starting a new one]\r\n")
        self.start()

    def _emit(self, text):
        if not text:
            return
        self.total += len(text)
        self.history = (self.history + text)[-HISTORY_CHARS:]
        for q in self.clients:
            q.put_nowait(("out", text, self.total))

    def replay(self, since):
        """Output the client missed since offset `since` (all history if unknown)."""
        start = self.total - len(self.history)
        if since < start or since > self.total:
            since = start
        return self.history[since - start:]

    def write(self, text):
        if self.closed:
            return
        data = text.encode()
        while data:
            try:
                n = os.write(self.fd, data)
            except BlockingIOError:
                continue
            except OSError:
                return
            data = data[n:]

    def set_echo(self, on):
        try:
            attrs = termios.tcgetattr(self.fd)
            if on and not attrs[3] & termios.ICANON:
                # Raw mode: the program (claude, vim, python…) draws its own input.
                # Kernel echo here would print typed keys twice and ^? / ^[[A junk.
                return
            attrs[3] = attrs[3] | termios.ECHO if on else attrs[3] & ~termios.ECHO
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        except (termios.error, OSError):
            pass

    def restore_prompt_mode(self):
        try:
            if self.prompt_attrs is not None:
                termios.tcsetattr(self.fd, termios.TCSANOW, self.prompt_attrs)
            else:
                self.set_echo(False)
        except (termios.error, OSError):
            pass

    def run_command(self, cmd):
        """A command from the chat box: no echo for it, then echo on for the program it starts."""
        if self.shell_is_idle():
            self.set_echo(False)
            self.write(cmd + "\n")

            def echo_if_busy():
                if not self.closed and not self.shell_is_idle():
                    self.set_echo(True)

            asyncio.get_running_loop().call_later(0.05, echo_if_busy)
        else:
            self.write(cmd + "\n")

    def shell_is_idle(self):
        """True when bash itself waits for a command (no program running)."""
        try:
            return os.tcgetpgrp(self.fd) == self.pid
        except OSError:
            return False

    def key_input(self, text):
        # Keys like arrows, Tab or Esc are for running programs. Sent to idle bash
        # they would only end up as junk in front of the next command.
        if self.shell_is_idle() and any((c < " " and c not in IDLE_OK) or c == "\x7f" for c in text):
            return
        self.write(text)

    def resize(self, cols, rows):
        self.cols = max(10, min(int(cols), 500))
        self.rows = max(5, min(int(rows), 300))
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.cols, 0, 0))
        except OSError:
            pass

    def send_signal(self, name):
        sig = getattr(signal, str(name), None)
        if not isinstance(sig, signal.Signals):
            return
        try:
            os.killpg(os.tcgetpgrp(self.fd), sig)
        except OSError:
            if sig == signal.SIGINT:
                self.write("\x03")

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.poll_handle is not None:
            self.poll_handle.cancel()
        pid = self.pid
        try:
            os.killpg(pid, signal.SIGHUP)  # bash leads its own process group
        except OSError:
            pass
        self._detach()  # closing the pty also hangs up bash and its jobs

        def force_kill():
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass

        asyncio.get_running_loop().call_later(2, force_kill)


def authorized(request):
    sent = request.headers.get("X-Token", "")
    return hmac.compare_digest(sent.encode(), request.app["token"].encode())


def unauthorized():
    return web.json_response({"status": "unauthorized"}, status=401)


async def health(request):
    if not authorized(request):
        return unauthorized()
    return web.json_response({"status": "ok", "version": VERSION})


def list_dir(path):
    entries = []
    truncated = False
    with os.scandir(path) as it:
        for entry in it:
            if len(entries) >= MAX_ENTRIES:
                truncated = True
                break
            entries.append(describe(entry.path, entry.name))
    entries.sort(key=lambda e: (not e["dir"], e["name"].casefold()))
    return entries, truncated


def describe(path, name):
    item = {"name": name, "dir": False, "link": False, "size": None, "locked": False}
    try:
        item["link"] = os.path.islink(path)
        item["dir"] = os.path.isdir(path)  # follows symlinks
        if item["dir"]:
            item["locked"] = not os.access(path, os.R_OK | os.X_OK)
        else:
            item["size"] = os.stat(path).st_size
    except OSError:
        pass  # broken link or no permission: still show the name
    return item


# Android does not let apps list "/" and some folders above Termux, but they can
# still be entered. For those, show the well-known children that exist.
KNOWN_CHILDREN = {
    "/": ["data", "sdcard", "storage", "system", "vendor", "product", "apex", "proc", "dev",
          "etc", "mnt", "odm", "cache", "config", "sys", "system_ext", "bin"],
    "/data": ["data", "user", "local", "media"],
    "/data/data": ["com.termux"],
    "/data/user": ["0"],
    "/data/user/0": ["com.termux"],
    "/data/media": ["0"],
    "/storage": ["emulated", "self"],
    "/storage/emulated": ["0"],
    "/mnt": ["sdcard", "media_rw", "user"],
}


def known_children(path):
    entries = []
    for name in KNOWN_CHILDREN.get(path, []):
        child = os.path.join(path, name)
        if os.path.lexists(child):
            entries.append(describe(child, name))
    entries.sort(key=lambda e: (not e["dir"], e["name"].casefold()))
    return entries


async def files(request):
    if not authorized(request):
        return unauthorized()
    raw = request.query.get("path") or "/"
    path = os.path.abspath(os.path.expanduser(raw))
    home = os.path.expanduser("~")
    loop = asyncio.get_running_loop()
    partial = False
    try:
        entries, truncated = await loop.run_in_executor(None, list_dir, path)
    except PermissionError:
        entries = await loop.run_in_executor(None, known_children, path)
        if not entries:
            return web.json_response({"error": "Permission denied", "path": path, "home": home}, status=403)
        truncated, partial = False, True
    except FileNotFoundError:
        return web.json_response({"error": "Folder not found", "path": path, "home": home}, status=404)
    except NotADirectoryError:
        return web.json_response({"error": "Not a folder", "path": path, "home": home}, status=400)
    except OSError as e:
        return web.json_response({"error": e.strerror or "Can't open folder", "path": path, "home": home}, status=500)
    return web.json_response({
        "path": path,
        "parent": os.path.dirname(path) if path != "/" else None,
        "home": home,
        "entries": entries,
        "truncated": truncated,
        "partial": partial,
    })


def memory_kb(root_pids):
    """Resident memory of the given processes and all their descendants."""
    children = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            children.setdefault(ppid, []).append(int(name))
        except (OSError, ValueError, IndexError):
            continue
    page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
    total, seen, stack = 0, set(), list(root_pids)
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, []))
        try:
            with open(f"/proc/{pid}/statm") as f:
                total += int(f.read().split()[1]) * page_kb
        except (OSError, ValueError, IndexError):
            pass
    return total


async def stats(request):
    if not authorized(request):
        return unauthorized()
    sessions = request.app["sessions"]
    pids = [os.getpid()] + [s.pid for s in sessions.values()]
    pids += [j.proc.pid for j in request.app["jobs"].values() if j.proc is not None and not j.done]
    rss = await asyncio.get_running_loop().run_in_executor(None, memory_kb, pids)
    return web.json_response({"sessions": len(sessions), "rss_kb": rss})


class Job:
    """One assistant command (e.g. claude -p ...) without a terminal; output is kept for replays."""

    def __init__(self, job_id):
        self.id = job_id
        self.proc = None
        self.chunks = []  # (fd, text)
        self.size = 0
        self.exit_code = None
        self.error = None
        self.clients = set()
        self.finished_at = None
        self.stopping = False

    async def start(self, cmd, prompt, session, cwd):
        path = os.path.abspath(os.path.expanduser(cwd or "~"))
        if not os.path.isdir(path):
            self.fail("Folder not found: " + path)
            return
        env = dict(os.environ, TC_PROMPT=prompt, TC_SESSION=session or "",
                   NO_COLOR="1", FORCE_COLOR="0", TERM="dumb")
        try:
            self.proc = await asyncio.create_subprocess_exec(
                "bash", "-lc", cmd, cwd=path, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as e:
            self.fail("Can't start: " + (e.strerror or str(e)))
            return
        await asyncio.gather(self._read(self.proc.stdout, 1), self._read(self.proc.stderr, 2))
        code = await self.proc.wait()
        self.finish(code)

    async def _read(self, stream, fd):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            data = await stream.read(65536)
            if not data:
                text = decoder.decode(b"", final=True)
                if text:
                    self.emit(fd, text)
                return
            text = decoder.decode(data)
            if text:
                self.emit(fd, text)

    def emit(self, fd, text):
        self.chunks.append((fd, text))
        self.size += len(text)
        while self.size > JOB_OUTPUT_CHARS and len(self.chunks) > 1:
            self.size -= len(self.chunks.pop(0)[1])
        for q in self.clients:
            q.put_nowait({"out": text, "fd": fd})

    def fail(self, message):
        self.error = message
        self.finished_at = asyncio.get_running_loop().time()
        for q in self.clients:
            q.put_nowait({"error": message})

    def finish(self, code):
        self.exit_code = code
        self.finished_at = asyncio.get_running_loop().time()
        for q in self.clients:
            q.put_nowait({"exit": code})

    @property
    def done(self):
        return self.finished_at is not None

    def replay(self):
        items = [{"out": t, "fd": fd} for fd, t in self.chunks]
        if self.error is not None:
            items.append({"error": self.error})
        elif self.exit_code is not None:
            items.append({"exit": self.exit_code})
        return items

    def stop(self):
        """Ctrl+C first (agents save their work), then terminate, then kill."""
        if self.proc is None or self.done or self.stopping:
            return
        self.stopping = True
        loop = asyncio.get_running_loop()

        def send(sig):
            if not self.done:
                try:
                    os.killpg(self.proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass

        send(signal.SIGINT)
        loop.call_later(3, send, signal.SIGTERM)
        loop.call_later(6, send, signal.SIGKILL)


def clean_jobs(jobs):
    now = asyncio.get_running_loop().time()
    for job_id in [j.id for j in jobs.values() if j.done and not j.clients and now - j.finished_at > JOB_KEEP_SECONDS]:
        jobs.pop(job_id, None)


async def run_handler(request):
    if not authorized(request):
        return unauthorized()
    job_id = request.query.get("job", "")
    if not SESSION_ID.match(job_id):
        return web.json_response({"error": "bad job id"}, status=400)
    jobs = request.app["jobs"]
    clean_jobs(jobs)

    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)
    queue = asyncio.Queue()
    job = jobs.get(job_id)
    if job is not None:
        for item in job.replay():
            queue.put_nowait(item)
        job.clients.add(queue)

    async def sender():
        while True:
            items = [await queue.get()]
            while not queue.empty():
                items.append(queue.get_nowait())
            text, fd = "", None
            for item in items:  # join bursts of output from the same stream
                if "out" in item and (fd is None or item["fd"] == fd):
                    text += item["out"]
                    fd = item["fd"]
                    continue
                if fd is not None:
                    await ws.send_json({"out": text, "fd": fd})
                    text, fd = "", None
                if "out" in item:
                    text, fd = item["out"], item["fd"]
                else:
                    await ws.send_json(item)
            if fd is not None:
                await ws.send_json({"out": text, "fd": fd})

    send_task = asyncio.create_task(sender())
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = msg.json()
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            start = data.get("start")
            if isinstance(start, dict) and job is None:
                job = Job(job_id)
                jobs[job_id] = job
                job.clients.add(queue)
                asyncio.create_task(job.start(
                    str(start.get("cmd", "")), str(start.get("prompt", "")),
                    str(start.get("session", "")), str(start.get("cwd", "~")),
                ))
            elif data.get("stop") and job is not None:
                job.stop()
            elif "start" not in data and job is None and data.get("attach"):
                queue.put_nowait({"error": "unknown job"})
    finally:
        if job is not None:
            job.clients.discard(queue)
        send_task.cancel()
    return ws


async def ws_handler(request):
    if not authorized(request):
        return unauthorized()
    session_id = request.query.get("session", "default")
    if not SESSION_ID.match(session_id):
        return web.json_response({"error": "bad session id"}, status=400)
    try:
        since = int(request.query.get("since", "0"))
    except ValueError:
        since = 0

    sessions = request.app["sessions"]
    shell = sessions.get(session_id)
    if shell is None:
        shell = Shell()
        shell.start()
        sessions[session_id] = shell

    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)

    # Replay and subscribe with no await in between, so nothing is lost or doubled.
    queue = asyncio.Queue()
    missed = shell.replay(since)
    if missed:
        queue.put_nowait(("out", missed, shell.total))
    queue.put_nowait(("state", shell.program))
    shell.clients.add(queue)

    async def sender():
        while True:
            items = [await queue.get()]
            while not queue.empty():
                items.append(queue.get_nowait())
            chunk, end = "", None
            for item in items:  # coalesce bursts of output into one message, keep order
                if item[0] == "out":
                    chunk += item[1]
                    end = item[2]
                    continue
                if end is not None:
                    await ws.send_json({"output": chunk, "end": end})
                    chunk, end = "", None
                await ws.send_json({"program": item[1], "busy": item[1] is not None})
            if end is not None:
                await ws.send_json({"output": chunk, "end": end})

    send_task = asyncio.create_task(sender())
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = msg.json()
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            if "cmd" in data:
                shell.run_command(str(data["cmd"]))
            elif "input" in data:
                shell.key_input(str(data["input"]))
            elif "signal" in data:
                shell.send_signal(data["signal"])
            elif "resize" in data:
                size = data["resize"]
                try:
                    shell.resize(size["cols"], size["rows"])
                except (KeyError, TypeError, ValueError):
                    pass
            elif data.get("exit"):
                shell.close()
                sessions.pop(session_id, None)
                for q in shell.clients:
                    q.put_nowait(("out", "", shell.total))
                await ws.close()
                break
    finally:
        shell.clients.discard(queue)
        send_task.cancel()
    return ws


async def on_startup(app):
    app["sessions"] = {}
    app["jobs"] = {}


async def on_shutdown(app):
    for shell in app["sessions"].values():
        shell.close()
    for job in app["jobs"].values():
        job.stop()


def main():
    token, created = load_or_create_token()
    green, yellow, bold, reset = "\033[32m", "\033[33m", "\033[1m", "\033[0m"
    print(green + "=" * 50 + reset)
    print(bold + "PocketBunty bridge v%d" % VERSION + reset + (" (new token generated)" if created else ""))
    print("Token: " + bold + yellow + token + reset)
    print("Paste this token in the PocketBunty app.")
    print(green + "=" * 50 + reset)
    acquire_wake_lock()

    app = web.Application()
    app["token"] = token
    app.router.add_get("/health", health)
    app.router.add_get("/files", files)
    app.router.add_get("/stats", stats)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/run", run_handler)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    web.run_app(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
