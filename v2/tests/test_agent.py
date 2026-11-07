#!/usr/bin/env python3
"""End-to-end checks for agent.py with a real OpenCode binary and a fake AI provider.

  PB_OPENCODE=/path/to/opencode python3 v2/tests/test_agent.py

Needs the OpenCode build for this computer (npm package opencode-linux-x64 / -arm64, same version
as env.sh). Everything runs in a temporary HOME; nothing on this machine is touched. Run it after
every change to agent.py.
"""

import ast
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AGENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent.py")
OPENCODE = os.environ.get("PB_OPENCODE", "")


class FakeProvider(BaseHTTPRequestHandler):
    """OpenAI-compatible streaming API. The user's words pick the behavior: "hello" writes a file
    and runs ls, "fail" answers 401, "slow" streams for 30 s, anything else says hi."""

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass  # the run was stopped while streaming

    def do_GET(self):
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"data":[{"id":"m1","object":"model"}]}')

    def chunk(self, delta, finish=None, usage=None):
        d = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m1",
             "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage:
            d["usage"] = usage
        self.wfile.write(b"data: " + json.dumps(d).encode() + b"\n\n")
        self.wfile.flush()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        msgs = body["messages"]
        users = [m for m in msgs if m["role"] == "user"]
        last = json.dumps(users[-1]["content"]) if users else ""
        tools = sum(1 for m in msgs[msgs.index(users[-1]):] if m["role"] == "tool") if users else 0
        names = [t["function"]["name"] for t in body.get("tools") or []]
        if body.get("tools") and "fail" in last:
            raw = b'{"error":{"message":"Invalid API key","type":"invalid_request_error"}}'
            self.send_response(401)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
        if not body.get("tools"):
            self.chunk({"role": "assistant", "content": "Title"})
            self.chunk({}, "stop", usage)
        elif "slow" in last and tools == 0:
            for i in range(60):
                self.chunk({"role": "assistant", "content": "tick%d " % i})
                time.sleep(0.5)
            self.chunk({}, "stop", usage)
        elif "hello" in last and tools == 0:
            self.chunk({"role": "assistant", "content": "I'll create it."})
            args = json.dumps({"filePath": "hello.txt", "content": "hi\n" * 50})
            self.chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "write", "arguments": args}}]})
            self.chunk({}, "tool_calls", usage)
        elif "hello" in last and tools == 1 and "bash" in names:
            self.chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "c2", "type": "function", "function": {
                "name": "bash", "arguments": json.dumps({"command": "ls", "description": "List files"})}}]})
            self.chunk({}, "tool_calls", usage)
        else:
            self.chunk({"role": "assistant", "content": "Done. tools=" + ",".join(sorted(names))})
            self.chunk({}, "stop", usage)
        self.wfile.write(b"data: [DONE]\n\n")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipUnless(OPENCODE and os.access(OPENCODE, os.X_OK), "set PB_OPENCODE to an OpenCode binary")
class AgentTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.provider = ThreadingHTTPServer(("127.0.0.1", cls.port), FakeProvider)
        threading.Thread(target=cls.provider.serve_forever, daemon=True).start()
        cls.tmp = tempfile.mkdtemp(prefix="pb-agent-test-")
        cls.home = os.path.join(cls.tmp, "home")
        cls.project = os.path.join(cls.tmp, "project")
        os.makedirs(cls.home)
        os.makedirs(cls.project)
        cls.env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GH_", "XDG_", "OPENCODE"))}
        cls.env.update(HOME=cls.home, PB_OPENCODE=OPENCODE, PREFIX=os.path.join(cls.tmp, "prefix"))
        os.makedirs(os.path.join(cls.tmp, "prefix", "glibc"))
        spec = json.dumps({"id": "fake", "name": "Fake", "baseURL": "http://127.0.0.1:%d/v1" % cls.port,
                           "key": "sk-test-1234567890", "models": ["m1"]})
        cls.agent(cls, "add-provider", prompt=spec, check=True)

    @classmethod
    def tearDownClass(cls):
        cls.provider.shutdown()

    def agent(self, *args, prompt="", extra=None, check=False, timeout=180):
        env = dict(self.env, TC_PROMPT=prompt, **(extra or {}))
        r = subprocess.run([sys.executable, AGENT, *args], env=env, cwd=self.project, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise AssertionError("agent %s failed: %s" % (args, r.stderr))
        return r

    def run_chat(self, prompt, model="fake/m1", access="full", session="", timeout=180):
        r = self.agent("run", prompt=prompt, extra={"PB_MODEL": model, "PB_ACCESS": access, "TC_SESSION": session,
                                                    "PB_RULES": "Be short. Don't build APKs."}, timeout=timeout)
        events = [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")]
        return r, events

    def no_servers_left(self):
        time.sleep(1)
        ps = subprocess.run(["pgrep", "-f", OPENCODE + " serve"], capture_output=True, text=True)
        self.assertEqual(ps.stdout.strip(), "", "an OpenCode server was left running")

    def test_static_no_shadowed_modules(self):
        tree = ast.parse(open(AGENT).read())
        modules = {a.asname or a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        names |= {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
        self.assertEqual(names & modules, set(), "a top-level name hides an imported module")

    def test_run_writes_file_runs_command_and_resumes(self):
        r, ev = self.run_chat("make hello")
        self.assertEqual(r.returncode, 0, r.stderr)
        types = [e["type"] for e in ev]
        self.assertEqual(types[0], "status")
        self.assertIn("start", types)
        tools = [e for e in ev if e["type"] == "tool" and e["status"] == "completed"]
        self.assertEqual([t["name"] for t in tools], ["write", "bash"])
        self.assertTrue(os.path.exists(os.path.join(self.project, "hello.txt")))
        self.assertNotIn("make hello", "".join(e.get("text", "") for e in ev if e["type"] == "text"), "the user's words came back as reply")
        self.assertEqual(ev[-1], {"type": "done", "session_id": ev[-1]["session_id"], "ok": True})
        sid = ev[-1]["session_id"]
        r2, ev2 = self.run_chat("again", session=sid)
        self.assertEqual(r2.returncode, 0)
        self.assertEqual(ev2[-1]["session_id"], sid)
        self.no_servers_left()

    def test_missing_session_starts_a_new_one(self):
        r, ev = self.run_chat("hi", session="ses_doesnotexist")
        self.assertEqual(r.returncode, 0)
        self.assertNotEqual(ev[-1]["session_id"], "ses_doesnotexist")

    def test_edit_access_has_no_shell_and_read_access_no_edits(self):
        _, ev = self.run_chat("tell tools", access="edit")
        text = "".join(e.get("text", "") for e in ev if e["type"] == "text")
        self.assertNotIn("bash", text)
        self.assertIn("write", text)
        _, ev = self.run_chat("tell tools", access="read")
        text = "".join(e.get("text", "") for e in ev if e["type"] == "text")
        self.assertNotIn("write", text.split("tools=")[-1].split(","))

    def test_rejected_key_stops_with_an_error(self):
        start = time.time()
        r, ev = self.run_chat("please fail")
        self.assertEqual(r.returncode, 1)
        self.assertLess(time.time() - start, 90)
        self.assertTrue(any(e["type"] == "error" for e in ev))
        self.no_servers_left()

    def test_unknown_model_is_an_error(self):
        r, ev = self.run_chat("hi", model="fake/nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("nope", [e for e in ev if e["type"] == "error"][0]["message"])
        r, ev = self.run_chat("hi", model="no-slash")
        self.assertEqual(r.returncode, 1)

    def test_stop_ends_cleanly(self):
        env = dict(self.env, TC_PROMPT="go slow", PB_MODEL="fake/m1", PB_ACCESS="full")
        p = subprocess.Popen([sys.executable, AGENT, "run"], env=env, cwd=self.project, stdout=subprocess.PIPE, text=True,
                             start_new_session=True)
        deadline = time.time() + 60
        seen = ""
        while time.time() < deadline and "tick2" not in seen:
            seen += p.stdout.readline()
        os.killpg(p.pid, signal.SIGINT)  # what the bridge does on Stop
        out, _ = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 130)
        self.assertIn('"stopped"', out)
        self.no_servers_left()

    def test_status_keys_models_and_logout(self):
        st = json.loads(self.agent("status", check=True).stdout)
        ids = {p["id"]: p for p in st["providers"]}
        self.assertTrue(st["installed"])
        self.assertEqual(ids["opencode"]["status"], "available")
        self.assertEqual(ids["fake"]["auth_kind"], "Custom")
        self.assertEqual(ids["openai"]["auth_kind"], "OAuth or API key")
        self.assertEqual(json.loads(self.agent("models", "fake", check=True).stdout)["models"], ["m1"])
        self.agent("set-key", "deepseek", prompt="sk-abcdef123456", check=True)
        st = json.loads(self.agent("status", check=True).stdout)
        self.assertEqual({p["id"]: p for p in st["providers"]}["deepseek"]["status"], "available")
        self.assertEqual(json.loads(self.agent("logout", "deepseek", check=True).stdout)["removed"], ["login"])
        self.assertNotEqual(self.agent("set-key", "deepseek", prompt="bad key").returncode, 0)
        self.assertNotEqual(self.agent("models", "BAD NAME").returncode, 0)

    def test_mcp_servers_and_skills(self):
        self.agent("mcp-add", prompt=json.dumps({"name": "docs", "type": "remote", "url": "http://127.0.0.1:9/mcp",
                                                  "headers": {"X-Api-Key": "k1"}}), check=True)
        self.agent("mcp-add", prompt=json.dumps({"name": "tools", "type": "local", "command": "definitely-missing-mcp --flag 'a b'",
                                                  "env": {"TOKEN": "t"}}), check=True)
        self.agent("mcp-disable", "docs", check=True)
        self.assertNotEqual(self.agent("mcp-add", prompt=json.dumps({"name": "bad name", "type": "local", "command": "x"})).returncode, 0)
        self.assertNotEqual(self.agent("mcp-add", prompt=json.dumps({"name": "x", "type": "remote", "url": "ftp://x"})).returncode, 0)
        self.agent("skill-add", prompt=json.dumps({"name": "Tap Counter Style", "description": "Use for counter apps.",
                                                    "body": "Always say BUNNY-SKILL."}), check=True)
        self.agent("skill-add", prompt=json.dumps({"content": "---\nname: pasted-one\ndescription: 'Pasted: works'\n---\nBody"}), check=True)
        self.assertNotEqual(self.agent("skill-add", prompt=json.dumps({"name": "x", "body": "no description"})).returncode, 0)
        self.agent("skill-add", prompt=json.dumps({"content": "---\ndescription: From a file\n---\nBody", "fallback_id": "My File"}), check=True)
        self.assertNotEqual(self.agent("skill-add", prompt=json.dumps({"content": "---\ndescription: d\n---\nB", "fallback_id": "SKILL"})).returncode, 0)
        self.assertNotEqual(self.agent("skill-add", prompt=json.dumps({"content": "no header"})).returncode, 0)
        ex = json.loads(self.agent("extras", check=True).stdout)
        mcp = {m["name"]: m for m in ex["mcp"]}
        self.assertEqual(mcp["docs"]["enabled"], False)
        self.assertEqual(mcp["docs"]["headers"], ["X-Api-Key"])
        self.assertEqual(mcp["tools"]["target"], "definitely-missing-mcp --flag a b")
        self.assertEqual({s["id"]: s["description"] for s in ex["skills"]},
                         {"tap-counter-style": "Use for counter apps.", "pasted-one": "Pasted: works", "my-file": "From a file"})
        shown = json.loads(self.agent("skill-show", "tap-counter-style", check=True).stdout)
        self.assertIn("Always say BUNNY-SKILL.", shown["content"])
        self.assertEqual(shown["files"], [])
        other = os.path.join(self.home, ".agents", "skills", "outside-one")
        os.makedirs(other)
        with open(os.path.join(other, "SKILL.md"), "w") as f:
            f.write("---\nname: outside-one\ndescription: From npx skills\n---\nx")
        ex2 = {s["id"]: s for s in json.loads(self.agent("extras", check=True).stdout)["skills"]}
        self.assertEqual(ex2["outside-one"]["source"], "agents")
        self.agent("skill-remove", "outside-one", check=True)
        self.assertFalse(os.path.exists(other))
        self.assertNotEqual(self.agent("skill-show", "../etc").returncode, 0)
        self.assertTrue(mcp["docs"]["oauth"] is False and mcp["tools"]["oauth"] is False)
        cfg_file = os.path.join(self.home, ".config", "opencode", "opencode.json")
        self.assertEqual(os.stat(cfg_file).st_mode & 0o777, 0o600)
        # OpenCode accepts the config (a broken MCP server doesn't stop the chat) and sees the skills.
        r, events = self.run_chat("Say hi")
        self.assertEqual(events[-1]["type"], "done", (events, r.stderr[-500:]))
        r = subprocess.run([OPENCODE, "debug", "skill"], env=dict(self.env, PB_OPENCODE=OPENCODE), cwd=self.project,
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            self.assertIn("tap-counter-style", r.stdout)
            self.assertIn("pasted-one", r.stdout)
        self.agent("mcp-remove", "docs", check=True)
        self.agent("mcp-remove", "tools", check=True)
        self.agent("skill-remove", "tap-counter-style", check=True)
        self.agent("skill-remove", "pasted-one", check=True)
        self.agent("skill-remove", "my-file", check=True)
        ex = json.loads(self.agent("extras", check=True).stdout)
        self.assertEqual((ex["mcp"], ex["skills"]), ([], []))
        self.assertNotEqual(self.agent("skill-remove", "../x").returncode, 0)

    def test_dns_fix_writes_glibc_files_once(self):
        self.agent("dns", check=True)
        self.agent("dns", check=True)
        hosts = open(os.path.join(self.tmp, "prefix", "glibc", "etc", "hosts")).read()
        self.assertEqual(hosts.count("pocketbunty-dns start"), 1)
        self.assertIn("localhost", hosts)
        self.assertNotIn(" 127.0.0.1\n", hosts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
