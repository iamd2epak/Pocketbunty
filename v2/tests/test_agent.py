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


class FakeAntigravity(BaseHTTPRequestHandler):
    """Google's token endpoint and Antigravity's Cloud Code server. Records what it was sent."""
    seen = []

    def log_message(self, *args):
        pass

    def reply(self, obj, status=200, ctype="application/json"):
        raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.reply(b"version: 2.30.1\nfiles: []\n", ctype="text/yaml")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0)).decode()
        FakeAntigravity.seen.append((self.path, dict(self.headers), raw))
        if self.path == "/token":
            import urllib.parse
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            if form.get("grant_type") == "authorization_code" and form.get("code") == "good-code":
                return self.reply({"access_token": "ya29.access", "refresh_token": "1//refresh", "expires_in": 3599})
            if form.get("grant_type") == "refresh_token" and form.get("refresh_token") == "1//refresh":
                return self.reply({"access_token": "ya29.access", "expires_in": 3599})
            return self.reply({"error": "invalid_grant"}, 400)
        if self.headers.get("authorization") != "Bearer ya29.access":
            return self.reply({"error": {"message": "bad token"}}, 401)
        if self.path == "/v1internal:loadCodeAssist":
            return self.reply({"cloudaicompanionProject": "proj-123", "currentTier": {"id": "free-tier"}})
        if self.path == "/v1internal:fetchAvailableModels":
            return self.reply({"models": {"gemini-3-flash": {"displayName": "Gemini 3 Flash", "maxTokens": 1048576},
                                          "claude-sonnet-4-6": {"displayName": "Claude Sonnet 4.6", "supportsThinking": False},
                                          "gpt-oss-120b-medium": {"displayName": "GPT-OSS 120B"},
                                          "chat_20706": {}, "old-model": {"disabled": True}},
                               "agentModelSorts": [{"groups": [{"modelIds": ["gemini-3-flash", "claude-sonnet-4-6", "gpt-oss-120b-medium", "old-model"]}]}]})
        if self.path == "/v1internal:streamGenerateContent?alt=sse":
            body = json.loads(raw)
            contents = body["request"]["contents"]
            asked = json.dumps(contents[-1])
            answered = any("functionResponse" in p for c in contents for p in c["parts"])
            if "make ag.txt" in asked and body["request"].get("tools") and not answered:
                call = {"functionCall": {"name": "write", "args": {"filePath": "ag.txt", "content": "hi\n"}}}
                first = {"candidates": [{"content": {"role": "model", "parts": [call]}}]}
            else:
                first = {"candidates": [{"content": {"role": "model", "parts": [{"text": "AG says hi from " + body["model"]}]}}]}
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for chunk in (first,
                          {"candidates": [{"content": {"role": "model", "parts": [{"text": "."}]}, "finishReason": "STOP"}],
                           "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 5, "totalTokenCount": 55}}):
                self.wfile.write(b"data: " + json.dumps({"response": chunk, "traceId": "t"}).encode() + b"\r\n\r\n")
                self.wfile.flush()
            return
        self.reply({"error": {"message": "unknown " + self.path}}, 404)


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
        cls.ag_port = free_port()
        cls.ag = ThreadingHTTPServer(("127.0.0.1", cls.ag_port), FakeAntigravity)
        threading.Thread(target=cls.ag.serve_forever, daemon=True).start()
        ag = "http://127.0.0.1:%d" % cls.ag_port
        cls.env.update(HOME=cls.home, PB_OPENCODE=OPENCODE, PREFIX=os.path.join(cls.tmp, "prefix"),
                       PB_AG_ENDPOINT=ag, PB_AG_TOKEN_URL=ag + "/token", PB_AG_MANIFEST=ag + "/manifest.yml")
        os.makedirs(os.path.join(cls.tmp, "prefix", "glibc"))
        spec = json.dumps({"id": "fake", "name": "Fake", "baseURL": "http://127.0.0.1:%d/v1" % cls.port,
                           "key": "sk-test-1234567890", "models": ["m1"]})
        cls.agent(cls, "add-provider", prompt=spec, check=True)

    @classmethod
    def tearDownClass(cls):
        cls.provider.shutdown()
        cls.ag.shutdown()

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
        self.assertIsNone(mcp["docs"]["signed_in"])
        self.agent("mcp-add", prompt=json.dumps({"name": "designs", "type": "remote", "url": "https://mcp.example.com/mcp"}), check=True)
        signed = lambda: {m["name"]: m for m in json.loads(self.agent("extras", check=True).stdout)["mcp"]}["designs"]
        self.assertEqual((signed()["oauth"], signed()["signed_in"]), (True, False))
        auth_file = os.path.join(self.home, ".local", "share", "opencode", "mcp-auth.json")
        with open(auth_file, "w") as f:
            json.dump({"designs": {"serverUrl": "https://mcp.example.com/mcp", "tokens": {"accessToken": "a", "refreshToken": "r"}}}, f)
        self.assertTrue(signed()["signed_in"])
        self.agent("mcp-add", prompt=json.dumps({"name": "fb", "type": "local", "command": "npx -y firebase-tools@latest mcp"}), check=True)
        fb = lambda: {m["name"]: m for m in json.loads(self.agent("extras", check=True).stdout)["mcp"]}["fb"]["signed_in"]
        self.assertFalse(fb())
        os.makedirs(os.path.join(self.home, ".config", "configstore"), exist_ok=True)
        with open(os.path.join(self.home, ".config", "configstore", "firebase-tools.json"), "w") as f:
            json.dump({"tokens": {"refresh_token": "x"}}, f)
        self.assertTrue(fb())
        self.agent("mcp-remove", "designs", check=True)
        self.agent("mcp-remove", "fb", check=True)
        with open(auth_file) as f:
            self.assertNotIn("designs", json.load(f))
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

    def test_antigravity_login_and_chat(self):
        import urllib.parse
        import urllib.request
        st = {p["id"]: p for p in json.loads(self.agent("status", check=True).stdout)["providers"]}
        self.assertEqual((st["antigravity"]["auth_kind"], st["antigravity"]["status"]), ("OAuth", "not configured"))
        self.assertNotEqual(self.agent("login-start", "antigravity").returncode, 0, "no OAuth client from the app")
        client = json.dumps({"client_id": "123-test.apps.googleusercontent.com", "client_secret": "test-secret"})
        start = json.loads(self.agent("login-start", "antigravity", prompt=client, check=True).stdout)
        self.assertIn("client_id=123-test.apps", start["auth_url"])
        self.assertEqual(start["input_kind"], "complete")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(start["auth_url"]).query)
        self.assertEqual(q["redirect_uri"], ["http://localhost:51121/oauth-callback"])
        # A wrong state is refused; the right one is taken (what the browser does after Google's page).
        bad = "http://127.0.0.1:51121/oauth-callback?code=x&state=wrong"
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(bad, timeout=5)
        page = urllib.request.urlopen("http://127.0.0.1:51121/oauth-callback?code=good-code&state=" + q["state"][0], timeout=5)
        self.assertIn("Signed in", page.read().decode())
        done = self.agent("login-finish", "antigravity", check=True)
        self.assertIn('"done"', done.stdout)
        with open(os.path.join(self.home, ".local", "share", "opencode", "auth.json")) as f:
            auth = json.load(f)
        self.assertEqual(auth["antigravity"]["refresh"], "1//refresh|proj-123")
        self.assertTrue(os.path.exists(os.path.join(self.home, ".config", "opencode", "plugin", "pocketbunty-antigravity.js")))
        models = json.loads(self.agent("models", "antigravity", check=True).stdout)["models"]
        self.assertEqual(sorted(models), ["claude-sonnet-4-6", "gemini-3-flash", "gpt-oss-120b-medium"])
        FakeAntigravity.seen.clear()
        for model in ("gemini-3-flash", "claude-sonnet-4-6"):
            r, events = self.run_chat("say hi", model="antigravity/" + model)
            text = "".join(e.get("text", "") for e in events if e.get("type") == "text")
            self.assertIn("AG says hi from " + model, text, r.stdout + r.stderr)
        sent = [(h, json.loads(b)) for p, h, b in FakeAntigravity.seen if p.startswith("/v1internal:stream")]
        self.assertTrue(sent)
        for headers, body in sent:
            self.assertEqual(body["project"], "proj-123")
            self.assertTrue(headers["User-Agent"].startswith("antigravity/hub/2.30.1 "))
            self.assertTrue(body["request"]["systemInstruction"]["parts"][0]["text"].startswith("You are Antigravity"))
        # An old access token is refreshed with the saved login.
        auth["antigravity"].update(access="ya29.old", expires=0)
        with open(os.path.join(self.home, ".local", "share", "opencode", "auth.json"), "w") as f:
            json.dump(auth, f)
        for model in ("gemini-3-flash", "claude-sonnet-4-6", "gpt-oss-120b-medium"):
            FakeAntigravity.seen.clear()
            target = os.path.join(self.project, "ag.txt")
            if os.path.exists(target):
                os.remove(target)
            r, events = self.run_chat("make ag.txt", model="antigravity/" + model)
            self.assertTrue(os.path.exists(target), model + ": " + r.stdout + r.stderr)
            replay = [json.loads(b) for p, h, b in FakeAntigravity.seen if p.startswith("/v1internal:stream") and "functionResponse" in b]
            parts = [p for c in replay[0]["request"]["contents"] for p in c["parts"]]
            call = next(p for p in parts if "functionCall" in p)
            result = next(p for p in parts if "functionResponse" in p)
            self.assertEqual(call["thoughtSignature"], "skip_thought_signature_validator")
            if model.startswith("gemini"):
                self.assertNotIn("id", call["functionCall"])
            else:
                # Claude and GPT need matching ids on the call and its result.
                self.assertTrue(call["functionCall"]["id"])
                self.assertEqual(result["functionResponse"]["id"], call["functionCall"]["id"])
                try:
                    import jsonschema
                except ImportError:
                    jsonschema = None
                for decl in replay[0]["request"]["tools"][0]["functionDeclarations"]:
                    if jsonschema:
                        jsonschema.Draft202012Validator.check_schema(decl["parameters"])
                    self.assertEqual(decl["parameters"]["type"], "object")
        claude = [b for h, b in sent if b["model"] == "claude-sonnet-4-6" and b["request"].get("tools")]
        self.assertTrue(claude)
        self.assertEqual(claude[0]["request"]["toolConfig"]["functionCallingConfig"]["mode"], "VALIDATED")
        self.assertEqual(json.loads(self.agent("logout", "antigravity", check=True).stdout)["removed"], ["login", "custom provider"])
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "opencode", "plugin", "pocketbunty-antigravity.js")))
        self.no_servers_left()

    def test_dns_fix_writes_glibc_files_once(self):
        self.agent("dns", check=True)
        self.agent("dns", check=True)
        hosts = open(os.path.join(self.tmp, "prefix", "glibc", "etc", "hosts")).read()
        self.assertEqual(hosts.count("pocketbunty-dns start"), 1)
        self.assertIn("localhost", hosts)
        self.assertNotIn(" 127.0.0.1\n", hosts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
