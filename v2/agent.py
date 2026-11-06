#!/usr/bin/env python3
"""pocketbunty-agent: the PocketBunty app's link to OpenCode (the AI coder), inside Termux.

Lives at ~/.pocketbunty/agent.py. The app runs it through the bridge (server.py):

  agent.py run                 one chat message (live JSON lines on stdout, see below)
  agent.py status              is OpenCode installed, which AI providers are connected (fast, no server)
  agent.py models <provider>   models of one provider
  agent.py set-key <provider>  saves an API key (key in $TC_PROMPT)
  agent.py logout <provider>   removes a saved login
  agent.py add-provider        adds an OpenAI-compatible provider (JSON in $TC_PROMPT)
  agent.py login-start <p>     starts a browser login: prints the page to open
  agent.py login-finish <p>    finishes it (code in $TC_PROMPT, or waits for the approval)
  agent.py dns                 makes OpenCode's name lookups work on this phone (also done before every run)
  agent.py version

`run` reads: TC_PROMPT (message), TC_SESSION (OpenCode session to continue, may be empty),
PB_MODEL ("provider/model"), PB_ACCESS (read|edit|full), PB_VARIANT (reasoning effort) and
PB_RULES (extra rules for the AI). It starts a private OpenCode server for this one message
(random port, password, only 127.0.0.1), sends the message and prints one JSON object per line:

  {"type":"start","session_id","provider","model"}      {"type":"text","id","text"}  (text delta)
  {"type":"thinking"}                                   {"type":"tool","id","name","status","input","output","error"}
  {"type":"tokens","input","output","cache_read","cache_write"}   {"type":"retry","attempt","message"}
  {"type":"status","text"}  {"type":"compacted"}  {"type":"error","message"}  {"type":"done","session_id"}

Ctrl+C (SIGINT/SIGTERM) stops the AI cleanly. Only Python's standard library is used.
"""

import base64
import http.client
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

VERSION = 1
HOME = os.path.expanduser("~")
DIR = os.path.join(HOME, ".pocketbunty")
OC_DIR = os.path.join(DIR, "opencode")
DATA = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share"), "opencode")
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "opencode")
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME, ".cache"), "opencode")
AUTH_FILE = os.path.join(DATA, "auth.json")
CONFIG_FILE = os.path.join(CONFIG, "opencode.json")
RULES_FILE = os.path.join(DIR, "rules.md")
LOGIN_FILE = os.path.join(DIR, "login.json")
DEFAULT_MODEL = "opencode/deepseek-v4.1-flash-free"
PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,200}$")

# Providers shown in the app even before they are connected, in this order (OpenCode knows 200+).
POPULAR = [
    ("opencode", "OpenCode Free"), ("opencode-go", "OpenCode Go"), ("openrouter", "OpenRouter"),
    ("anthropic", "Anthropic (Claude API)"), ("openai", "OpenAI / ChatGPT"), ("google", "Google Gemini"),
    ("deepseek", "DeepSeek"), ("zai-coding-plan", "Z.AI Coding Plan (GLM)"), ("zai", "Z.AI (GLM)"),
    ("moonshotai", "Moonshot (Kimi)"), ("kimi-code-plan-global", "Kimi Code Plan"), ("mistral", "Mistral"),
    ("github-copilot", "GitHub Copilot"), ("xai", "xAI (Grok)"), ("groq", "Groq"),
    ("minimax", "MiniMax"), ("alibaba", "Alibaba (Qwen)"), ("togetherai", "Together AI"),
    ("fireworks-ai", "Fireworks AI"), ("deepinfra", "DeepInfra"), ("cerebras", "Cerebras"),
    ("nvidia", "NVIDIA"), ("huggingface", "Hugging Face"), ("chutes", "Chutes"), ("ollama-cloud", "Ollama Cloud"),
]
# Providers with a browser/device login (OpenCode's OAuth): only that, or that or an API key.
OAUTH_ONLY = {"github-copilot"}
OAUTH_OR_KEY = {"openai", "gitlab", "poe"}


# ---------------------------------------------------------------- small helpers

def out(obj):
    try:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, ValueError):
        pass  # nobody reads any more (app gone): keep going, the bridge keeps the job


def fail(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)


def read_json(path, default):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def write_json(path, data, private=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def opencode_bin():
    for p in (os.environ.get("PB_OPENCODE", ""), os.path.join(OC_DIR, "opencode")):
        if p and os.access(p, os.X_OK):
            return p
    return None


def oc_env(extra=None):
    """OpenCode is a glibc program: Termux's LD_PRELOAD (termux-exec) breaks it."""
    env = dict(os.environ)
    env.pop("LD_PRELOAD", None)
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    env.setdefault("TMPDIR", os.path.join(DIR, "tmp"))
    os.makedirs(env["TMPDIR"], exist_ok=True)
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------- DNS for OpenCode
# OpenCode is a glibc program. glibc looks names up itself, from its own resolv.conf and hosts file
# ($PREFIX/glibc/etc), not through Android's resolver like Termux's own programs (and Python) do.
# Often that file is missing or names a DNS server the network blocks, so OpenCode fails with
# "getaddrinfo EAI_AGAIN chatgpt.com" while the phone's internet works. Before OpenCode starts:
# 1. the hosts it needs are looked up with Android's resolver and written to glibc's hosts file;
# 2. glibc's resolv.conf gets public DNS servers that answer on this network, for any other host.

# Hosts the AI providers and OpenCode use (more are added from the chosen provider's settings).
DNS_HOSTS = [
    "opencode.ai", "api.opencode.ai", "models.dev", "chatgpt.com", "auth.openai.com", "api.openai.com",
    "api.anthropic.com", "openrouter.ai", "api.githubcopilot.com", "github.com", "api.github.com",
    "generativelanguage.googleapis.com", "api.deepseek.com", "api.z.ai", "open.bigmodel.cn", "api.moonshot.ai",
    "api.kimi.com", "api.mistral.ai", "api.x.ai", "api.groq.com", "api.together.xyz", "api.fireworks.ai",
    "api.deepinfra.com", "api.cerebras.ai", "integrate.api.nvidia.com", "router.huggingface.co",
    "api.minimax.io", "dashscope-intl.aliyuncs.com", "llm.chutes.ai", "ollama.com", "registry.npmjs.org",
]
PUBLIC_DNS = ["1.1.1.1", "8.8.8.8", "9.9.9.9", "1.0.0.1", "8.8.4.4", "208.67.222.222"]
HOSTS_START = "# pocketbunty-dns start (written by PocketBunty for OpenCode; refreshed on every run)"
HOSTS_END = "# pocketbunty-dns end"
_dns_done = False


def glibc_etc():
    prefix = os.environ.get("PREFIX") or "/data/data/com.termux/files/usr"
    return os.path.join(prefix, "glibc", "etc")


def dns_answers(server, timeout=1.5):
    """True when [server] answers a DNS query for opencode.ai on this network (UDP 53)."""
    query_id = secrets.randbits(16)
    packet = query_id.to_bytes(2, "big") + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    for label in "opencode.ai".split("."):
        packet += bytes([len(label)]) + label.encode()
    packet += b"\x00\x00\x01\x00\x01"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(packet, (server, 53))
        data, _ = s.recvfrom(512)
        return len(data) > 12 and int.from_bytes(data[:2], "big") == query_id and (data[3] & 0x0F) == 0
    except OSError:
        return False
    finally:
        s.close()


def provider_hosts():
    """API hosts of connected and custom providers (from OpenCode's catalog and config)."""
    hosts = set()
    names = set(read_json(AUTH_FILE, {})) | set(custom_providers())
    cat = catalog()
    for pid in names:
        entry = custom_providers().get(pid) or {}
        urls = [((entry.get("options") or {}).get("baseURL") or ""), str((cat.get(pid) or {}).get("api") or "")]
        for url in urls:
            m = re.match(r"^https?://([^/:]+)", url)
            if m and not re.match(r"^[0-9.]+$|^\[|^localhost$", m.group(1).lower()):
                hosts.add(m.group(1).lower())
    return hosts


def fix_dns():
    """Makes OpenCode's own name lookups work on this phone (see above). Never fails the run."""
    global _dns_done
    if _dns_done:
        return
    _dns_done = True
    try:
        etc = glibc_etc()
        if not os.path.isdir(os.path.dirname(etc)):
            return  # not Termux's glibc (a computer): its own DNS works
        os.makedirs(etc, exist_ok=True)
        hosts = sorted(set(DNS_HOSTS) | provider_hosts())
        found = {}
        servers = []

        def look(host):
            try:
                ips = {a[4][0] for a in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)}
                if ips:
                    found[host] = sorted(ips)[:4]
            except OSError:
                pass

        def probe(server):
            if dns_answers(server):
                servers.append(server)

        threads = [threading.Thread(target=look, args=(h,), daemon=True) for h in hosts]
        threads += [threading.Thread(target=probe, args=(d,), daemon=True) for d in PUBLIC_DNS]
        for t in threads:
            t.start()
        end = time.time() + 4
        for t in threads:
            t.join(max(0.0, end - time.time()))

        # hosts: our block (fresh addresses) + whatever else was there.
        path = os.path.join(etc, "hosts")
        try:
            with open(path) as f:
                old = f.read()
        except OSError:
            old = "127.0.0.1 localhost\n::1 localhost\n"
        old = re.sub(re.escape(HOSTS_START) + r".*?" + re.escape(HOSTS_END) + r"\n?", "", old, flags=re.S)
        block = [HOSTS_START] + ["%s %s" % (ip, host) for host in sorted(found) for ip in found[host]] + [HOSTS_END]
        tmp = "%s.%d.tmp" % (path, os.getpid())  # two chats at once each write their own
        with open(tmp, "w") as f:
            f.write(old.rstrip("\n") + "\n" + "\n".join(block) + "\n")
        os.replace(tmp, path)

        # resolv.conf: DNS servers that answer here (kept as they are when none answer).
        working = [d for d in PUBLIC_DNS if d in servers][:3]
        if working:
            tmp = os.path.join(etc, "resolv.conf.%d.tmp" % os.getpid())
            with open(tmp, "w") as f:
                f.write("# Written by PocketBunty: DNS servers that answer on this network.\n")
                f.writelines("nameserver %s\n" % d for d in working)
                f.write("options timeout:2 attempts:2\n")
            os.replace(tmp, os.path.join(etc, "resolv.conf"))
        with open(os.path.join(DIR, "dns.log"), "w") as f:
            f.write("hosts: %d of %d resolved\nservers that answer: %s\n" % (len(found), len(hosts), " ".join(working) or "none"))
    except Exception as e:  # noqa: BLE001 - DNS help must never stop a run
        try:
            with open(os.path.join(DIR, "dns.log"), "w") as f:
                f.write("failed: %s\n" % e)
        except OSError:
            pass


def die_with_parent():
    """In the child: if this script is killed, the OpenCode server goes too (no leftover server)."""
    try:
        import ctypes
        ctypes.CDLL(None).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    """A private `opencode serve` (random port, random password, 127.0.0.1 only)."""

    def __init__(self, cwd, config=None, detached=False):
        self.port = free_port()
        self.password = secrets.token_urlsafe(18)
        self.auth = "Basic " + base64.b64encode(("opencode:" + self.password).encode()).decode()
        binary = opencode_bin()
        if binary is None:
            raise RuntimeError("OpenCode isn't installed. Run the PocketBunty setup again.")
        fix_dns()
        extra = {"OPENCODE_SERVER_PASSWORD": self.password}
        if config:
            extra["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        log_path = os.path.join(DIR, "opencode-server.log")
        try:
            if os.path.getsize(log_path) > 2_000_000:
                os.remove(log_path)  # keep only recent runs
        except OSError:
            pass
        self.log = open(log_path, "ab")
        # Warnings and errors go to the log, so a failure can say why (see server_error).
        cmd = [binary, "serve", "--port", str(self.port), "--hostname", "127.0.0.1", "--print-logs", "--log-level", "WARN"]
        if detached and shutil.which("timeout"):
            cmd = ["timeout", "900"] + cmd  # a login nobody finishes doesn't keep a server forever
        self.proc = subprocess.Popen(
            cmd,
            cwd=cwd, env=oc_env(extra), stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
            start_new_session=True, preexec_fn=None if detached else die_with_parent,
        )

    def request(self, method, path, body=None, timeout=60):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            data = None if body is None else json.dumps(body)
            conn.request(method, path, data, {"Authorization": self.auth, "Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError:
                parsed = raw.decode(errors="replace")
            return resp.status, parsed
        finally:
            conn.close()

    def wait_ready(self, seconds=120):
        end = time.time() + seconds
        while time.time() < end:
            if self.proc.poll() is not None:
                raise RuntimeError("OpenCode stopped while starting (exit %s). See ~/.pocketbunty/opencode-server.log" % self.proc.returncode)
            try:
                status, _ = self.request("GET", "/config", timeout=5)
                if status == 200:
                    return
            except OSError:
                pass
            time.sleep(0.25)
        raise RuntimeError("OpenCode didn't start in time. Close other apps and try again.")

    def events(self, sink):
        """Reads the server's event stream into [sink] (a queue) until the server goes away."""
        def reader():
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.port)
                conn.request("GET", "/event", headers={"Authorization": self.auth, "Accept": "text/event-stream"})
                resp = conn.getresponse()
                data = []
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    line = line.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif line == "" and data:
                        try:
                            sink.put(json.loads("\n".join(data)))
                        except ValueError:
                            pass
                        data = []
            except Exception as e:  # noqa: BLE001 - any failure ends the stream
                sink.put({"type": "pb.stream_error", "message": str(e)})
            sink.put({"type": "pb.stream_end"})
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        return t

    def stop(self):
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    pass


def server_error(ref=None):
    """The reason OpenCode logged for its last error ("Unexpected server error" says nothing)."""
    try:
        with open(os.path.join(DIR, "opencode-server.log"), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if "level=ERROR" not in line or (ref and ref not in line):
            continue
        m = re.search(r'error="(?:Error: )?([^"\\]{1,200})', line)
        if m:
            return m.group(1).strip()
    return None


def error_text(err):
    """OpenCode error object -> one readable line."""
    if not isinstance(err, dict):
        return str(err or "Unknown error")
    data = err.get("data") if isinstance(err.get("data"), dict) else {}
    msg = data.get("message") or err.get("message") or err.get("name") or "Unknown error"
    if "Unexpected server error" in str(msg):
        msg = server_error(data.get("ref")) or msg
    body = data.get("responseBody") or ""
    if "FreeUsageLimitError" in body or "Free usage limit" in str(msg):
        return FREE_LIMIT
    return str(msg)


FREE_LIMIT = ("Free model limit reached for now. Wait a while and try again, or pick another model "
              "(or connect your own provider in AI providers).")


# ---------------------------------------------------------------- run

ACCESS = {
    # Never ask (nobody can answer on a phone run): allow or deny each kind of action up front.
    "full": {"*": "allow", "question": "deny"},
    "edit": {"*": "allow", "bash": "deny", "question": "deny"},
    "read": {"*": "allow", "edit": "deny", "bash": "deny", "question": "deny"},
}
KEEP_INPUT = ("filePath", "path", "command", "pattern", "url", "query", "description", "include", "subagent_type", "prompt")


def short_input(inp):
    if not isinstance(inp, dict):
        return {}
    keep = {k: (str(v)[:2000]) for k, v in inp.items() if k in KEEP_INPUT and v not in (None, "")}
    for k in ("content", "newString", "patchText"):
        if isinstance(inp.get(k), str):
            keep[k + "_bytes"] = len(inp[k].encode())
    return keep


def cmd_run():
    prompt = os.environ.get("TC_PROMPT", "")
    session = os.environ.get("TC_SESSION", "").strip()
    model = os.environ.get("PB_MODEL", "").strip() or DEFAULT_MODEL
    access = os.environ.get("PB_ACCESS", "edit").strip().lower()
    variant = os.environ.get("PB_VARIANT", "").strip()
    rules = os.environ.get("PB_RULES", "")
    if "/" not in model or not MODEL_ID.match(model):
        out({"type": "error", "message": "Pick a model first (Model button)."})
        return 1
    provider, model_id = model.split("/", 1)
    if access not in ACCESS:
        access = "edit"

    config = {"autoupdate": False, "permission": ACCESS[access], "small_model": model}
    if rules.strip():
        try:
            with open(RULES_FILE, "w") as f:
                f.write(rules.strip() + "\n")
            config["instructions"] = [RULES_FILE]
        except OSError:
            pass

    stopping = threading.Event()
    def on_signal(signum, frame):
        stopping.set()
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    out({"type": "status", "text": "Starting OpenCode…"})
    try:
        server = Server(os.getcwd(), config)
    except RuntimeError as e:
        out({"type": "error", "message": str(e)})
        return 127
    try:
        return run_with(server, prompt, session, provider, model_id, access, variant, stopping)
    except RuntimeError as e:
        out({"type": "error", "message": str(e)})
        return 1
    finally:
        server.stop()


def run_with(server, prompt, session, provider, model_id, access, variant, stopping):
    server.wait_ready()
    if stopping.is_set():
        return 130
    events = queue.Queue()
    server.events(events)

    sid = None
    if session:
        status, info = server.request("GET", "/session/" + session)
        if status == 200 and isinstance(info, dict) and info.get("id"):
            sid = info["id"]
        else:
            out({"type": "status", "text": "Old session not found, starting a new one"})
    if sid is None:
        status, info = server.request("POST", "/session", {})
        if status != 200 or not isinstance(info, dict) or not info.get("id"):
            raise RuntimeError("OpenCode couldn't start a session (%s): %s" % (status, str(info)[:300]))
        sid = info["id"]
    out({"type": "start", "session_id": sid, "provider": provider, "model": model_id})

    body = {
        "parts": [{"type": "text", "text": prompt}],
        "model": {"providerID": provider, "modelID": model_id},
        "agent": "plan" if access == "read" else "build",
    }
    if variant:
        body["variant"] = variant
    status, resp = server.request("POST", "/session/%s/prompt_async" % sid, body)
    if status >= 300:
        raise RuntimeError("OpenCode refused the message (%s): %s" % (status, error_text(resp) if isinstance(resp, dict) else str(resp)[:300]))

    texts = {}       # part id -> text sent so far
    kinds = {}       # part id -> part type
    tools = {}       # part id -> last status sent
    users = set()    # ids of the user's own messages (their text isn't the reply)
    empty_steps = 0  # model calls in a row that gave nothing (OpenCode can loop on a rejected request)
    step_output = False
    error = None
    busy_seen = False
    aborted_at = None
    last_event = time.time()

    while True:
        if stopping.is_set() and aborted_at is None:
            aborted_at = time.time()
            out({"type": "status", "text": "Stopping…"})
            try:
                server.request("POST", "/session/%s/abort" % sid, {}, timeout=10)
            except OSError:
                pass
        if aborted_at is not None and time.time() - aborted_at > 4:
            break
        try:
            ev = events.get(timeout=0.5)
        except queue.Empty:
            # The stream is quiet: make sure the session didn't finish without us seeing it.
            if busy_seen and time.time() - last_event > 30:
                last_event = time.time()
                try:
                    st, all_status = server.request("GET", "/session/status", timeout=10)
                    if st == 200 and isinstance(all_status, dict) and sid not in all_status:
                        break
                except OSError:
                    pass
            continue
        last_event = time.time()
        etype = ev.get("type", "")
        props = ev.get("properties") or {}
        if etype == "pb.stream_end":
            if server.proc.poll() is not None and aborted_at is None:
                error = error or "OpenCode stopped unexpectedly. Tap Retry. (Log: ~/.pocketbunty/opencode-server.log)"
            break
        if etype == "pb.stream_error":
            if server.proc.poll() is not None:
                error = error or "OpenCode stopped unexpectedly. See ~/.pocketbunty/opencode-server.log"
            break
        ev_sid = props.get("sessionID") or (props.get("part") or {}).get("sessionID") or (props.get("info") or {}).get("sessionID")
        if ev_sid and ev_sid != sid:
            continue

        if etype == "message.updated":
            info = props.get("info") or {}
            if info.get("role") == "user" and info.get("id"):
                users.add(info["id"])
            elif info.get("role") == "assistant" and info.get("error") and aborted_at is None:
                if (info["error"] or {}).get("name") != "MessageAbortedError":
                    error = error_text(info["error"])
            continue
        if etype == "message.part.updated":
            part = props.get("part") or {}
            pid, ptype = part.get("id", ""), part.get("type", "")
            if part.get("messageID") in users:
                continue
            kinds[pid] = ptype
            if ptype in ("text", "tool", "reasoning"):
                step_output = True
            if ptype == "text":
                full = part.get("text") or ""
                sent = texts.get(pid, "")
                if len(full) > len(sent) and full.startswith(sent):
                    out({"type": "text", "id": pid, "text": full[len(sent):]})
                    texts[pid] = full
            elif ptype == "reasoning":
                out({"type": "thinking"})
            elif ptype == "tool":
                state = part.get("state") or {}
                st = state.get("status", "")
                running_out = (state.get("metadata") or {}).get("output") if st == "running" else None
                if tools.get(pid) == st and not running_out:
                    continue
                tools[pid] = st
                item = {"type": "tool", "id": pid, "name": part.get("tool", ""), "status": st,
                        "input": short_input(state.get("input"))}
                if st == "completed":
                    item["output"] = str(state.get("output") or "")[:4000]
                    if state.get("title"):
                        item["title"] = str(state["title"])[:200]
                elif st == "error":
                    item["error"] = str(state.get("error") or "failed")[:2000]
                elif running_out:
                    item["output"] = str(running_out)[-2000:]
                out(item)
            elif ptype == "step-start":
                step_output = False
            elif ptype == "step-finish":
                tok = part.get("tokens") or {}
                cache = tok.get("cache") or {}
                if not step_output and not tok.get("input") and not tok.get("output"):
                    empty_steps += 1
                    if empty_steps >= 3 and aborted_at is None:
                        error = error or ("The AI provider gave no answer. Check its API key and credit in AI providers, "
                                          "or pick another model.")
                        stopping.set()
                    continue
                empty_steps = 0
                out({"type": "tokens", "input": tok.get("input", 0), "output": tok.get("output", 0) + tok.get("reasoning", 0),
                     "cache_read": cache.get("read", 0), "cache_write": cache.get("write", 0)})
            elif ptype == "compaction":
                out({"type": "compacted"})
        elif etype == "message.part.delta":
            pid = props.get("partID", "")
            if props.get("field") != "text" or props.get("messageID") in users:
                continue
            step_output = True
            if kinds.get(pid) == "reasoning":
                out({"type": "thinking"})
            else:
                delta = props.get("delta") or ""
                texts[pid] = texts.get(pid, "") + delta
                out({"type": "text", "id": pid, "text": delta})
        elif etype == "session.status":
            status = props.get("status") or {}
            if status.get("type") == "busy":
                busy_seen = True
            elif status.get("type") == "retry":
                action = status.get("action") or {}
                if action.get("reason") == "free_tier_limit":
                    error = FREE_LIMIT
                    stopping.set()
                    continue
                message = str(status.get("message", ""))[:300]
                attempt = int(status.get("attempt", 0) or 0)
                host = re.search(r"(?:EAI_AGAIN|ENOTFOUND|getaddrinfo)\W+([A-Za-z0-9.-]+\.[a-z]{2,})", message)
                if (host or re.search(r"ECONNREFUSED|ECONNRESET|ETIMEDOUT|fetch failed|Unable to connect", message)) and attempt >= 2:
                    error = ("Can't reach %s from OpenCode. Check the internet (or turn off a VPN / Private DNS that blocks it), "
                             "then tap Retry." % (host.group(1) if host else "the AI provider"))
                    stopping.set()
                    continue
                out({"type": "retry", "attempt": attempt, "message": message})
            elif status.get("type") == "idle" and (busy_seen or aborted_at is not None):
                break
        elif etype == "session.idle":
            if busy_seen or aborted_at is not None:
                break
        elif etype == "session.error":
            err = props.get("error")
            name = err.get("name") if isinstance(err, dict) else ""
            if name == "MessageAbortedError" or aborted_at is not None:
                continue
            error = error_text(err)
        elif etype == "session.compacted":
            out({"type": "compacted"})
        elif etype == "permission.asked":
            # Shouldn't happen (all actions are allowed or denied above): say no, so nothing waits.
            rid = props.get("id", "")
            if rid:
                try:
                    server.request("POST", "/permission/%s/reply" % rid, {"reply": "reject"}, timeout=10)
                except OSError:
                    pass
        elif etype == "question.asked":
            rid = props.get("id", "")
            if rid:
                try:
                    server.request("POST", "/question/%s/reject" % rid, {}, timeout=10)
                except OSError:
                    pass

    if aborted_at is not None and error is None:
        out({"type": "stopped", "session_id": sid})
        return 130
    if error:
        out({"type": "error", "message": error})
        out({"type": "done", "session_id": sid, "ok": False})
        return 1
    out({"type": "done", "session_id": sid, "ok": True})
    return 0


# ---------------------------------------------------------------- providers and logins

def catalog():
    """OpenCode's provider list (models.dev, cached by OpenCode itself)."""
    return read_json(os.path.join(CACHE, "models.json"), {})


def custom_providers():
    cfg = read_json(CONFIG_FILE, {})
    prov = cfg.get("provider")
    return prov if isinstance(prov, dict) else {}


def cmd_status():
    auth = read_json(AUTH_FILE, {})
    cat = catalog()
    custom = custom_providers()
    binary = opencode_bin()
    version = ""
    try:
        with open(os.path.join(OC_DIR, "version")) as f:
            version = f.read().strip()
    except OSError:
        pass
    rows, seen = [], set()

    def add(pid, name):
        if pid in seen or not PROVIDER_ID.match(pid):
            return
        seen.add(pid)
        info = auth.get(pid) if isinstance(auth.get(pid), dict) else None
        kind = "OAuth" if pid in OAUTH_ONLY else "OAuth or API key" if pid in OAUTH_OR_KEY else "API key"
        if pid == "opencode":
            # Free models work without any login.
            rows.append({"id": pid, "name": name, "status": "available", "method": "free models" if not info else "API key",
                         "auth_kind": "Free"})
            return
        rows.append({
            "id": pid,
            "name": name,
            "status": "available" if info else "not configured",
            "method": ("browser login" if info and info.get("type") == "oauth" else "API key") if info else "",
            "auth_kind": "Custom" if pid in custom else kind,
        })

    for pid, name in POPULAR:
        add(pid, name)
    for pid in list(auth) + list(custom):
        entry = custom.get(pid) or cat.get(pid) or {}
        add(pid, entry.get("name") or pid)
    out({"installed": binary is not None, "version": version, "providers": rows})
    return 0


def cmd_models(provider):
    if not PROVIDER_ID.match(provider):
        fail("Bad provider name")
    binary = opencode_bin()
    if binary is None:
        fail("OpenCode isn't installed. Run the PocketBunty setup again.", 127)
    try:
        fix_dns()
        r = subprocess.run([binary, "models", provider], env=oc_env(), cwd=HOME, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        fail("OpenCode took too long to list the models.")
    if r.returncode != 0:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", (r.stderr or r.stdout).strip()).replace("Error:", "").strip()
        if "Provider not found" in msg:
            msg = "Connect this provider first (AI providers)."
        fail(msg or "OpenCode couldn't list the models.")
    models = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith(provider + "/"):
            models.append(line[len(provider) + 1:])
    info = (catalog().get(provider) or {}).get("models") or {}
    free = [m for m in models if m.endswith("-free") or m == "big-pickle"
            or ((info.get(m) or {}).get("cost") or {}).get("input") == 0 and ((info.get(m) or {}).get("cost") or {}).get("output") == 0]
    # Free ones first, then the rest in OpenCode's order.
    ordered = free + [m for m in models if m not in free]
    out({"provider": provider, "models": ordered, "free": free})
    return 0


def secret_from_env():
    value = os.environ.get("TC_PROMPT", "")
    if not value and not sys.stdin.isatty():
        value = sys.stdin.read()
    return value.strip()


def cmd_set_key(provider):
    if not PROVIDER_ID.match(provider):
        fail("Bad provider name")
    key = secret_from_env()
    if len(key) < 8 or any(c.isspace() for c in key):
        fail("That doesn't look like an API key.")
    auth = read_json(AUTH_FILE, {})
    auth[provider] = {"type": "api", "key": key}
    write_json(AUTH_FILE, auth, private=True)
    out({"provider": provider, "saved": True})
    return 0


def cmd_logout(provider):
    if not PROVIDER_ID.match(provider):
        fail("Bad provider name")
    removed = []
    auth = read_json(AUTH_FILE, {})
    if provider in auth:
        del auth[provider]
        write_json(AUTH_FILE, auth, private=True)
        removed.append("login")
    cfg = read_json(CONFIG_FILE, {})
    if isinstance(cfg.get("provider"), dict) and provider in cfg["provider"]:
        del cfg["provider"][provider]
        write_json(CONFIG_FILE, cfg)
        removed.append("custom provider")
    out({"provider": provider, "removed": removed})
    return 0


def cmd_add_provider():
    """{"id","name","baseURL","key","models":[...]} in $TC_PROMPT: an OpenAI-compatible API."""
    try:
        spec = json.loads(os.environ.get("TC_PROMPT", ""))
    except ValueError:
        fail("Bad provider details.")
    pid = str(spec.get("id", "")).strip().lower()
    name = str(spec.get("name", "")).strip()[:60] or pid
    base = str(spec.get("baseURL", "")).strip().rstrip("/")
    key = str(spec.get("key", "")).strip()
    models = [str(m).strip() for m in spec.get("models") or [] if str(m).strip()]
    if not PROVIDER_ID.match(pid):
        fail("The ID can use small letters, numbers and dashes only.")
    if not re.match(r"^https?://[^\s]+$", base):
        fail("The address must start with https:// (or http://).")
    if not models or not all(MODEL_ID.match(m) for m in models):
        fail("Add at least one model name.")
    cfg = read_json(CONFIG_FILE, {})
    cfg.setdefault("$schema", "https://opencode.ai/config.json")
    prov = cfg.get("provider") if isinstance(cfg.get("provider"), dict) else {}
    prov[pid] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": name,
        "options": {"baseURL": base},
        "models": {m: {"name": m} for m in models},
    }
    cfg["provider"] = prov
    write_json(CONFIG_FILE, cfg)
    if key:
        auth = read_json(AUTH_FILE, {})
        auth[pid] = {"type": "api", "key": key}
        write_json(AUTH_FILE, auth, private=True)
    out({"provider": pid, "saved": True})
    return 0


def login_server():
    """The server kept alive between login-start and login-finish."""
    info = read_json(LOGIN_FILE, {})
    pid = info.get("pid")
    if not pid:
        return None, info
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return None, info
    s = Server.__new__(Server)
    s.port, s.password = int(info["port"]), info["password"]
    s.auth = "Basic " + base64.b64encode(("opencode:" + s.password).encode()).decode()
    s.proc = None
    return s, info


def end_login():
    _, info = login_server()
    pid = info.get("pid")
    if pid:
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    try:
        os.remove(LOGIN_FILE)
    except OSError:
        pass


# ---- device logins done here, without OpenCode (same steps as its built-in plugins) ----
# OpenCode's server makes these requests with Bun, which can fail on some phones ("Unexpected
# server error"); Python's own network code is reliable in Termux. The result is saved in
# OpenCode's auth file exactly like OpenCode saves it.

OPENAI_ISSUER = "https://auth.openai.com"
OPENAI_CLIENT = "app_EMoamEEZ73f0CkXaXp7hrann"
COPILOT_CLIENT = "Ov23li8tweQw6odWQebz"
NATIVE_LOGINS = {"openai", "github-copilot"}
UA = "opencode/1.19.5"


def web(method, url, data=None, form=False, timeout=30):
    """(status, parsed JSON or text). Network failures raise RuntimeError with a readable reason."""
    import urllib.error
    import urllib.request
    headers = {"User-Agent": UA, "Accept": "application/json"}
    body = None
    if data is not None:
        if form:
            import urllib.parse
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        host = url.split("/")[2]
        raise RuntimeError("Couldn't reach %s (%s). Check the internet and try again." % (host, reason))
    try:
        return status, json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except ValueError:
        return status, raw.decode("utf-8", "replace")[:300]


def jwt_claims(token):
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:  # noqa: BLE001 - not a JWT
        return {}


def openai_account(tokens):
    for key in ("id_token", "access_token"):
        claims = jwt_claims(tokens.get(key) or "")
        account = (claims.get("chatgpt_account_id") or (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
                   or ((claims.get("organizations") or [{}])[0] or {}).get("id"))
        if account:
            return account
    return None


def save_oauth(provider, info):
    auth = read_json(AUTH_FILE, {})
    auth[provider] = dict({"type": "oauth"}, **info)
    write_json(AUTH_FILE, auth, private=True)


def native_login_start(provider):
    if provider == "openai":
        status, data = web("POST", OPENAI_ISSUER + "/api/accounts/deviceauth/usercode", {"client_id": OPENAI_CLIENT})
        if status != 200 or not isinstance(data, dict) or not data.get("user_code"):
            fail("OpenAI didn't start the login (HTTP %s). Try again in a minute." % status)
        state = {"provider": provider, "native": True, "device_auth_id": data["device_auth_id"], "user_code": data["user_code"],
                 "interval": max(int(str(data.get("interval") or 5)), 1)}
        url = OPENAI_ISSUER + "/codex/device"
    else:
        status, data = web("POST", "https://github.com/login/device/code", {"client_id": COPILOT_CLIENT, "scope": "read:user"})
        if status != 200 or not isinstance(data, dict) or not data.get("device_code"):
            fail("GitHub didn't start the login (HTTP %s). Try again in a minute." % status)
        state = {"provider": provider, "native": True, "device_code": data["device_code"], "user_code": data["user_code"],
                 "interval": max(int(data.get("interval") or 5), 1)}
        url = data.get("verification_uri") or "https://github.com/login/device"
    state["expires"] = time.time() + 15 * 60
    write_json(LOGIN_FILE, state, private=True)
    out({"status": "pending", "provider": provider, "auth_url": url, "input_kind": "complete",
         "user_code": state["user_code"], "instructions": "Enter code: " + state["user_code"],
         "expires_at_ms": int(state["expires"] * 1000)})
    return 0


def native_login_finish(provider, state):
    """Waits (≤ 280 s) until the user approves the code on the page, then saves the login."""
    end = min(time.time() + 280, state.get("expires", 0))
    interval = state.get("interval", 5)
    while time.time() < end:
        if provider == "openai":
            status, data = web("POST", OPENAI_ISSUER + "/api/accounts/deviceauth/token",
                                {"device_auth_id": state["device_auth_id"], "user_code": state["user_code"]})
            if status == 200 and isinstance(data, dict) and data.get("authorization_code"):
                status, tokens = web("POST", OPENAI_ISSUER + "/oauth/token", {
                    "grant_type": "authorization_code", "code": data["authorization_code"],
                    "redirect_uri": OPENAI_ISSUER + "/deviceauth/callback", "client_id": OPENAI_CLIENT,
                    "code_verifier": data.get("code_verifier", ""),
                }, form=True)
                if status != 200 or not isinstance(tokens, dict) or not tokens.get("access_token"):
                    fail("OpenAI login failed at the last step (HTTP %s). Tap Start again." % status)
                info = {"refresh": tokens.get("refresh_token", ""), "access": tokens["access_token"],
                        "expires": int((time.time() + int(tokens.get("expires_in") or 3600)) * 1000)}
                account = openai_account(tokens)
                if account:
                    info["accountId"] = account
                save_oauth("openai", info)
                return
            if status not in (403, 404):
                fail("OpenAI login failed (HTTP %s). Tap Start again." % status)
        else:
            status, data = web("POST", "https://github.com/login/oauth/access_token", {
                "client_id": COPILOT_CLIENT, "device_code": state["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            })
            if status != 200 or not isinstance(data, dict):
                fail("GitHub login failed (HTTP %s). Tap Start again." % status)
            if data.get("access_token"):
                save_oauth("github-copilot", {"refresh": data["access_token"], "access": data["access_token"], "expires": 0})
                return
            err = data.get("error")
            if err == "slow_down":
                interval = int(data.get("interval") or interval + 5)
            elif err == "expired_token":
                fail("The code expired. Tap Start again.")
            elif err == "access_denied":
                fail("The login was cancelled on GitHub.")
            elif err != "authorization_pending":
                fail("GitHub login failed: %s" % (data.get("error_description") or err or "unknown"))
        time.sleep(interval + 3)
    fail("Login wasn't approved in time. Tap Start again.")


def cmd_login_start(provider):
    if not PROVIDER_ID.match(provider):
        fail("Bad provider name")
    end_login()
    if provider in NATIVE_LOGINS:
        try:
            return native_login_start(provider)
        except RuntimeError as e:
            fail(str(e))
    try:
        server = Server(HOME, detached=True)
    except RuntimeError as e:
        fail(str(e), 127)
    try:
        server.wait_ready()
        status, methods = server.request("GET", "/provider/auth")
        options = (methods or {}).get(provider) if isinstance(methods, dict) else None
        oauth = [(i, m) for i, m in enumerate(options or []) if m.get("type") == "oauth"]
        if not oauth:
            raise RuntimeError("No browser login for this provider: it uses an API key.")
        # Prefer a login that works on a phone (device code / headless) over a desktop browser one.
        index, method = next(((i, m) for i, m in oauth if "headless" in m.get("label", "").lower()), oauth[0])
        inputs = {}
        for p in method.get("prompts") or []:
            if p.get("when"):
                continue
            if p.get("type") == "select" and p.get("options"):
                inputs[p["key"]] = p["options"][0]["value"]
            else:
                raise RuntimeError("This login asks for more details: use the Terminal (opencode auth login).")
        status, auth = server.request("POST", "/provider/%s/oauth/authorize" % provider, {"method": index, "inputs": inputs})
        if status != 200 or not isinstance(auth, dict) or not auth.get("url"):
            why = error_text(auth) if isinstance(auth, dict) else str(auth)[:200]
            if "device authorization" in why.lower() or "fetch" in why.lower():
                why += " (check the internet connection and try again)"
            raise RuntimeError("Couldn't start the login: %s" % why)
        code = re.search(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,})\b", auth.get("instructions") or "")
        write_json(LOGIN_FILE, {"pid": server.proc.pid, "port": server.port, "password": server.password,
                                "provider": provider, "method": index, "kind": auth.get("method")}, private=True)
        out({
            "status": "pending",
            "provider": provider,
            "auth_url": auth["url"],
            "input_kind": "complete" if auth.get("method") == "auto" else "auth_code",
            "user_code": code.group(1) if code else None,
            "instructions": auth.get("instructions") or "",
            "expires_at_ms": int((time.time() + 10 * 60) * 1000),
        })
        return 0
    except (RuntimeError, OSError) as e:
        server.stop()
        fail(str(e) if isinstance(e, RuntimeError) else "Couldn't reach OpenCode: %s" % e)


def cmd_login_finish(provider):
    state = read_json(LOGIN_FILE, {})
    if state.get("native"):
        if state.get("provider") != provider or time.time() > state.get("expires", 0):
            fail("This login expired. Tap Start again.")
        try:
            native_login_finish(provider, state)
        except RuntimeError as e:
            fail(str(e))
        finally:
            try:
                os.remove(LOGIN_FILE)
            except OSError:
                pass
        out({"status": "done", "provider": provider})
        return 0
    server, info = login_server()
    if server is None or info.get("provider") != provider:
        fail("This login expired. Tap Start again.")
    body = {"method": info.get("method", 0)}
    code = os.environ.get("TC_PROMPT", "").strip()
    if info.get("kind") == "code":
        if not code:
            fail("Paste the code first.")
        m = re.search(r"[?&]code=([^&#\s]+)", code)
        body["code"] = m.group(1) if m else code
    try:
        # A device login waits here until the user approves it on the page.
        status, resp = server.request("POST", "/provider/%s/oauth/callback" % provider, body, timeout=280)
    except OSError as e:
        end_login()
        fail("Login wasn't approved in time. Tap Start again." if isinstance(e, socket.timeout) else "Login failed: %s" % e)
    end_login()
    if status >= 300:
        fail("Login failed: %s" % (error_text(resp) if isinstance(resp, dict) else str(resp)[:200]))
    out({"status": "done", "provider": provider})
    return 0


def main():
    args = sys.argv[1:]
    if not args:
        fail(__doc__)
    cmd, rest = args[0], args[1:]
    os.makedirs(DIR, exist_ok=True)
    if cmd == "version":
        out({"agent": VERSION, "opencode": opencode_bin() is not None})
        return 0
    if cmd == "run":
        return cmd_run()
    if cmd == "status":
        return cmd_status()
    if cmd == "dns":
        fix_dns()
        try:
            with open(os.path.join(DIR, "dns.log")) as f:
                sys.stdout.write(f.read())
        except OSError:
            pass
        return 0
    if cmd == "add-provider":
        return cmd_add_provider()
    if cmd in ("models", "set-key", "logout", "login-start", "login-finish") and len(rest) == 1:
        return {"models": cmd_models, "set-key": cmd_set_key, "logout": cmd_logout,
                "login-start": cmd_login_start, "login-finish": cmd_login_finish}[cmd](rest[0])
    fail("Unknown command: " + " ".join(args))


if __name__ == "__main__":
    sys.exit(main())
