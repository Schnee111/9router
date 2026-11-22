#!/usr/bin/env python3
"""
agy-acp-bridge v2 — OpenAI-compatible surface over the official Antigravity CLI.

Adds account profiles: every account lives in its own `--app-data-dir`, so
several Google accounts hold credentials side by side and the bridge rotates
across the ones that pass a real turn.

Endpoints
  GET  /admin                     web UI: add account, paste code, test turn
  POST /admin/login/start         {profile}         -> {loginId, url}
  POST /admin/login/complete      {loginId, code}   -> {ok, detail}
  GET  /admin/login/<id>          login progress (url, done, detail)
  GET  /admin/profiles            profiles + test state
  POST /admin/test                {profile, model}  -> one real turn
  GET  /health
  GET  /v1/models
  POST /v1/chat/completions       stream + non-stream (model "profile/model" pins)
"""
import base64
import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AGY = os.environ.get("AGY_BIN", "/root/.local/bin/agy")
PORT = int(os.environ.get("AGY_BRIDGE_PORT", "20130"))
# --app-data-dir rejects absolute paths ("must not be absolute"): it is a
# directory NAME resolved under ~/.gemini. So each profile is a name, not a path.
APPDATA_ROOT = os.environ.get("AGY_APPDATA_ROOT", "/root/.gemini")
DEFAULT_PROFILE_DIR = os.path.join(APPDATA_ROOT, "antigravity-cli")
PROFILES_DIR = os.environ.get("AGY_PROFILES_DIR", "/root/.agy-profiles")
WORKDIR = os.environ.get("AGY_CWD", "/root/agy-bridge/work")
TURN_TIMEOUT = int(os.environ.get("AGY_TURN_TIMEOUT", "300"))
LOCK_WAIT = int(os.environ.get("AGY_LOCK_WAIT", "20"))
# A turn that produces NO event for this long is wedged upstream: cancel
# it and fail over instead of holding the worker until TURN_TIMEOUT.
STALL_TIMEOUT = int(os.environ.get("AGY_STALL_TIMEOUT", "120"))
# In-session corrections when the agent reaches for its own tools.
TOOL_RETRIES = max(0, int(os.environ.get("AGY_TOOL_RETRIES", "1")))
PLAIN_RETRY_NUDGE = ("Do not use any tool for this. Answer the user's "
                     "question directly in plain text, using what you already "
                     "know or can see in the conversation.")

TOOL_RETRY_NUDGE = ("You used your own built-in tool. Built-in tools are NOT "
                    "available in this mode. Fulfil the user's request by emitting "
                    "the call as a <tool_call>{...}</tool_call> block in OpenAI "
                    "function-call shape, with arguments as a JSON string. Do not "
                    "run anything else; just emit the block.")

WORKERS_PER_PROFILE = max(1, int(os.environ.get("AGY_WORKERS_PER_PROFILE", "1")))
# Session cache: a request that extends a conversation we already
# have in a live ACP session only sends the new tail.
SESSION_CACHE = os.environ.get("AGY_SESSION_CACHE", "1") != "0"
SESSION_IDLE = int(os.environ.get("AGY_SESSION_IDLE", "900"))
SESSION_MAX = max(1, int(os.environ.get("AGY_SESSION_MAX", "4")))
# Spawn the primary profile at boot instead of on the first request.
PREWARM = os.environ.get("AGY_PREWARM", "1") != "0"
PREWARM_PROFILES = max(0, int(os.environ.get("AGY_PREWARM_PROFILES", "1")))
TOKEN_FILE = "antigravity-oauth-token"

os.makedirs(WORKDIR, exist_ok=True)
os.makedirs(PROFILES_DIR, exist_ok=True)

_lock = threading.Lock()


def log(*a):
    with _lock:
        print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- profiles
def profile_name(name):
    """Value to pass to --app-data-dir: a bare directory name, never a path."""
    if name in ("default", "", None):
        return "antigravity-cli"
    return "agy-" + name


def profile_dir(name):
    """Absolute on-disk location of that profile's data."""
    if name in ("default", "", None):
        return DEFAULT_PROFILE_DIR
    return os.path.join(APPDATA_ROOT, profile_name(name))


def profile_authed(name):
    return os.path.isfile(os.path.join(profile_dir(name), TOKEN_FILE))


def list_profiles():
    out = []
    if profile_authed("default"):
        out.append("default")
    if os.path.isdir(APPDATA_ROOT):
        for entry in sorted(os.listdir(APPDATA_ROOT)):
            if not entry.startswith("agy-"):
                continue
            p = os.path.join(APPDATA_ROOT, entry)
            if os.path.isdir(p) and os.path.isfile(os.path.join(p, TOKEN_FILE)):
                out.append(entry[4:])
    if os.path.isdir(PROFILES_DIR):  # legacy absolute-path profiles
        for entry in sorted(os.listdir(PROFILES_DIR)):
            p = os.path.join(PROFILES_DIR, entry)
            if os.path.isdir(p) and os.path.isfile(os.path.join(p, TOKEN_FILE)) and entry not in out:
                out.append("__legacy__" + entry)
    return out


# ---------------------------------------------------------------- login flow
class Login:
    def __init__(self, profile):
        self.id = uuid.uuid4().hex[:12]
        self.profile = profile
        args = [AGY, "acp", "--login"]
        if profile != "default":
            args += ["--app-data-dir", profile_name(profile)]
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb",
                 "HOME": os.environ.get("HOME", "/root")},
        )
        self.url = None
        self.done = False
        self.ok = None
        self.detail = ""
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = ""
        for line in self.proc.stdout:
            line = line.rstrip()
            buf += line + "\n"
            if not self.url:
                i = buf.find("https://accounts.google.com/")
                if i >= 0:
                    j = buf.find("\n", i)
                    self.url = buf[i:j if j > 0 else len(buf)].strip()
            low = line.lower()
            if "logged in as" in low:
                self.ok, self.detail, self.done = True, line, True
            elif "couldn't log in" in low:
                self.ok, self.detail, self.done = False, line, True
        self.proc.wait()
        if not self.done:
            self.done = True
            self.ok = self.ok or False
            self.detail = self.detail or f"exited {self.proc.returncode}"

    def submit(self, code):
        if self.proc.poll() is not None:
            return False
        self.proc.stdin.write(code.strip() + "\n")
        self.proc.stdin.flush()
        return True


LOGINS = {}


# ---------------------------------------------------------------- ACP worker
def message_hash(m):
    """Stable fingerprint of one chat message for the session cache.

    Images are hashed by their URL/base64 digest, not embedded raw:
    the same picture in two requests must line up without carrying
    megabytes of base64 around.
    """
    role = str(m.get("role") or "user")
    c = m.get("content")
    if isinstance(c, list):
        parts = []
        for p in c:
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            if t == "text":
                parts.append("t:" + str(p.get("text") or ""))
            elif t in ("image_url", "image", "input_image"):
                iu = p.get("image_url") or p.get("imageUrl") or {}
                url = iu.get("url") if isinstance(iu, dict) else iu
                parts.append("i:" + hashlib.sha1(
                    str(url).encode("utf-8", "replace")).hexdigest())
            else:
                parts.append(str(t) + ":" + json.dumps(p, sort_keys=True)[:200])
        c = "\n".join(parts)
    return hashlib.sha1((role + "\x00" + str(c or "")).encode(
        "utf-8", "replace")).hexdigest()


class AgyWorker:
    def __init__(self, profile, idx):
        self.profile = profile
        self.idx = idx
        self.lock = threading.RLock()
        self.busy = False              # RLock has no .locked(); track it explicitly
        self.sessions = {}             # (model, history hashes) -> live session
        self.stats = {"hit": 0, "miss": 0, "closed": 0}
        self.id_q = {}
        self.turn_q = {}
        self.next_id = 100
        self._spawn()

    def _spawn(self):
        args = [AGY, "acp", "--sandbox"]
        if self.profile != "default":
            args += ["--app-data-dir", profile_name(self.profile)]
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=WORKDIR, text=True, bufsize=1,
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb",
                 "HOME": os.environ.get("HOME", "/root")},
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self.rpc("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                   "terminal": False},
            "clientInfo": {"name": "agy-acp-bridge", "version": "2.0.0"},
        }, timeout=60)
        log(f"[{self.profile}] acp ready")

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    # ---- ACP session cache ------------------------------------------------
    # Every request used to pay session/new + set_model + the WHOLE transcript
    # as one prompt. A live ACP session already holds its context, so a request
    # whose history extends one we have only needs the new tail. Key = model +
    # hash of every message sent so far; a hit requires the stored hashes to be
    # a strict prefix of the incoming ones (a branch or an edit misses).

    def _find_session(self, hashes, model):
        for key, e in list(self.sessions.items()):
            if e["model"] != model:
                continue
            h = e["hashes"]
            if h and len(hashes) > len(h) and hashes[:len(h)] == h:
                return key, len(h)
        return None, 0

    def _drop_session(self, key, reason=""):
        e = self.sessions.pop(key, None)
        if not e:
            return
        self._clear_tool_mode(e["sid"])
        try:
            self.rpc("session/close", {"sessionId": e["sid"]}, 5)
            self.stats["closed"] += 1
        except Exception as ex:
            log(f"[{self.profile}] session/close {e['sid'][:8]}: {str(ex)[:80]}")
        if reason:
            log(f"[{self.profile}] session {e['sid'][:8]} dropped ({reason})")

    def _drop_session_by_sid(self, sid, reason="turn unfinished"):
        for k, e in list(self.sessions.items()):
            if e["sid"] == sid:
                self._drop_session(k, reason)
                return
        self._clear_tool_mode(sid)
        try:
            self.rpc("session/close", {"sessionId": sid}, 5)
        except Exception:
            pass

    def _cleanup_sessions(self):
        now = time.time()
        for k, e in list(self.sessions.items()):
            if now - e["last"] > SESSION_IDLE:
                self._drop_session(k, "idle")
        while len(self.sessions) > SESSION_MAX:
            oldest = min(self.sessions.items(), key=lambda kv: kv[1]["last"])[0]
            self._drop_session(oldest, "lru")

    def _set_tool_mode(self, sid, client_tools):
        """Tell the PreToolUse hook whether this session's client declared tools.

        The hook payload carries conversationId == ACP sessionId, so a marker
        file per session makes denial conditional without any shared state
        between concurrent workers.
        """
        d = os.path.join(WORKDIR, ".bridge-mode")
        p = os.path.join(d, sid + ".json")
        try:
            if client_tools:
                os.makedirs(d, exist_ok=True)
                with open(p, "w") as f:
                    json.dump({"client_tools": True}, f)
            elif os.path.exists(p):
                os.remove(p)
        except Exception as e:
            log(f"[{self.profile}] tool-mode marker: {str(e)[:80]}")

    def _clear_tool_mode(self, sid):
        try:
            os.remove(os.path.join(WORKDIR, ".bridge-mode", sid + ".json"))
        except Exception:
            pass

    def warm(self):
        """Pay the one-time first-session cost before a real request lands."""
        if not self.lock.acquire(timeout=30):
            return
        try:
            sid = self.rpc("session/new", {"cwd": WORKDIR, "mcpServers": []},
                           90).get("sessionId")
            if sid:
                try:
                    self.rpc("session/close", {"sessionId": sid}, 10)
                except Exception:
                    pass
        finally:
            self.lock.release()

    def _read_stderr(self):
        for line in self.proc.stderr:
            line = line.rstrip()
            if line and not line.startswith(("I0", "W0", "E0")):
                pass

    def _read_stdout(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._dispatch(json.loads(line))
            except Exception as e:
                log(f"[{self.profile} dispatch] {e}")

    def _dispatch(self, msg):
        method = msg.get("method")
        if method:
            if method == "session/update":
                p = msg.get("params", {})
                q = self.turn_q.get(p.get("sessionId"))
                if q:
                    q.put(("update", p.get("update", {})))
                return
            if method == "session/request_permission":
                opts = msg.get("params", {}).get("options", []) or []
                reject = next((o.get("optionId") for o in opts
                               if str(o.get("kind", "")).startswith("reject")), None)
                self._send({"jsonrpc": "2.0", "id": msg.get("id"),
                            "result": ({"outcome": {"outcome": "selected", "optionId": reject}}
                                       if reject else {"outcome": {"outcome": "cancelled"}})})
                return
            if "id" in msg:
                self._send({"jsonrpc": "2.0", "id": msg.get("id"), "result": {}})
            return
        if "id" in msg:
            q = self.id_q.pop(msg["id"], None)
            if q is None:
                for tq in list(self.turn_q.values()):
                    q = tq
                    break
            if q:
                q.put(("response", msg))

    def _send(self, obj):
        if not self.alive():
            raise RuntimeError("agy acp exited")
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def rpc(self, method, params, timeout=120):
        mid = self.next_id
        self.next_id += 1
        q = queue.Queue()
        self.id_q[mid] = q
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                self.id_q.pop(mid, None)
                raise TimeoutError(f"{method} timeout")
            try:
                kind, msg = q.get(timeout=left)
            except queue.Empty:
                self.id_q.pop(mid, None)
                raise TimeoutError(f"{method} timeout")
            if kind != "response":
                continue
            if "error" in msg:
                raise RuntimeError(f"{method}: {msg['error'].get('message')}")
            return msg.get("result", {})

    def chat(self, text, model=None, timeout=None, lock_wait=None, images=None,
             segments=None, suffix="", client_tools=False):
        timeout = timeout or TURN_TIMEOUT
        # A generator holds this lock for the whole turn. If a client disconnects
        # mid-stream the generator must be closed by the caller, otherwise the
        # lock leaks and every later request queues until it times out. Wait a
        # bounded time so a busy worker fails over instead of hanging.
        lock_wait = LOCK_WAIT if lock_wait is None else lock_wait
        if not self.lock.acquire(timeout=lock_wait):
            raise RuntimeError(f"worker busy ({self.profile})")
        self.busy = True
        t_start = time.time()
        sid = None
        sess_key = None
        hashes = None
        reused = False
        finished = False
        t_new = 0.0
        first_chunk = 0.0
        prompt_sent = 0.0
        prompt_text = text
        try:
            try:
                self._cleanup_sessions()
                if segments is not None and SESSION_CACHE:
                    hashes = tuple(message_hash(m) for m in segments)
                    sess_key, cut = self._find_session(hashes, model or "")
                    if sess_key is not None:
                        sid = self.sessions[sess_key]["sid"]
                        self.sessions[sess_key]["last"] = time.time()
                        reused = True
                        self.stats["hit"] += 1
                        prompt_text, images = flatten(segments[cut:])
                    else:
                        prompt_text, images = flatten(segments)
                        self.stats["miss"] += 1
                if suffix:
                    prompt_text += suffix
                if sid is None:
                    t0 = time.time()
                    new = self.rpc("session/new", {"cwd": WORKDIR, "mcpServers": []}, timeout=60)
                    sid = new.get("sessionId")
                    t_new = time.time() - t0
                    if not sid:
                        raise RuntimeError("no sessionId")
                    if model:
                        try:
                            self.rpc("session/set_model", {"sessionId": sid, "modelId": model}, 30)
                        except Exception as e:
                            log(f"[{self.profile}] set_model {model}: {e}")
                    if hashes is not None:
                        sess_key = (model or "", hashes)
                        # same history replayed (e.g. a retry or a fresh client
                        # asking the identical first question): the old session
                        # cannot be extended, so close it instead of leaking it
                        if sess_key in self.sessions:
                            self._drop_session(sess_key, "superseded")
                        self.sessions[sess_key] = {"sid": sid, "model": model or "",
                                                   "hashes": hashes, "last": time.time()}
                        self._cleanup_sessions()
                self._set_tool_mode(sid, client_tools)
                q = queue.Queue()
                self.turn_q[sid] = q
                seen = {}
                thoughts = []
                tools_out = {}
                emitted = False
                retries = 0
                mid = self.next_id
                self.next_id += 1
                self.id_q[mid] = q
                blocks = [{"type": "text", "text": prompt_text}] + list(images or [])
                prompt_sent = time.time()
                self._send({"jsonrpc": "2.0", "id": mid, "method": "session/prompt",
                            "params": {"sessionId": sid, "prompt": blocks}})
                deadline = time.time() + timeout
                last_evt = time.time()
                while True:
                    left = deadline - time.time()
                    if left <= 0:
                        self._send({"jsonrpc": "2.0", "method": "session/cancel",
                                    "params": {"sessionId": sid}})
                        raise TimeoutError("turn timeout")
                    try:
                        kind, msg = q.get(timeout=min(left, 1.0))
                    except queue.Empty:
                        if time.time() - last_evt > STALL_TIMEOUT:
                            # the upstream stream went silent mid-turn; cancel is
                            # advisory, dropping the session in finally is what
                            # actually frees the agent
                            log(f"[{self.profile}] turn stalled "
                                f"{time.time() - last_evt:.0f}s, cancelling {sid[:8]}")
                            self._send({"jsonrpc": "2.0", "method": "session/cancel",
                                        "params": {"sessionId": sid}})
                            raise TimeoutError(f"turn stalled (no event {STALL_TIMEOUT}s)")
                        continue
                    last_evt = time.time()
                    if kind == "update":
                        u = msg
                        kind_name = u.get("sessionUpdate") or "?"
                        seen[kind_name] = seen.get(kind_name, 0) + 1
                        if kind_name in ("tool_call", "tool_call_update"):
                            tc_id = u.get("toolCallId")
                            if tc_id:
                                slot = tools_out.setdefault(tc_id, {
                                    "id": tc_id, "type": "function",
                                    "function": {"name": "", "arguments": ""}})
                                fn = slot["function"]
                                if u.get("name"):
                                    fn["name"] = u["name"]
                                elif u.get("title") and not fn["name"]:
                                    fn["name"] = str(u["title"])[:60]
                                raw = u.get("rawInput")
                                if raw is not None:
                                    fn["arguments"] = json.dumps(raw)
                        if kind_name == "agent_thought_chunk":
                            c = u.get("content") or {}
                            if c.get("type") == "text" and c.get("text"):
                                thoughts.append(c["text"])
                        if u.get("sessionUpdate") == "agent_message_chunk":
                            c = u.get("content") or {}
                            if c.get("type") == "text" and c.get("text"):
                                if not first_chunk:
                                    first_chunk = time.time() - prompt_sent
                                emitted = True
                                yield c["text"]
                        continue
                    if kind == "response":
                        if "error" in msg:
                            raise RuntimeError(msg["error"].get("message"))
                        if not emitted and any(
                                t["function"]["name"] for t in tools_out.values()):
                            # the agent ran its OWN tool instead of emitting the
                            # caller's function. Never surface that: the client would
                            # get a function it never declared, with internal argument
                            # names. Correct it inside the same session (the context is
                            # already there, so this is cheap) and only fail over when
                            # it keeps insisting.
                            if retries < TOOL_RETRIES:
                                retries += 1
                                log(f"[{self.profile}] agent used built-in tools "
                                    f"({'contract' if client_tools else 'plain-text'} "
                                    f"correction, try {retries})")
                                tools_out.clear()
                                seen.clear()
                                thoughts.clear()
                                mid = self.next_id
                                self.next_id += 1
                                self.id_q[mid] = q
                                self._send({"jsonrpc": "2.0", "id": mid,
                                            "method": "session/prompt",
                                            "params": {"sessionId": sid, "prompt": [
                                                {"type": "text",
                                                 "text": TOOL_RETRY_NUDGE if client_tools
                                                 else PLAIN_RETRY_NUDGE}]}})
                                deadline = time.time() + timeout
                                last_evt = time.time()
                                continue
                            if client_tools:
                                log(f"[{self.profile}] agent kept using built-in tools")
                                raise RuntimeError("agent used built-in tools instead of "
                                                   "the caller's functions")
                            # no client tools: a native call is still better than an
                            # empty completion, so fall through and translate it
                            log(f"[{self.profile}] no text after correction, "
                                "translating native call as last resort")
                        # the turn is complete: keep the session and rekey it on
                        # the full incoming history BEFORE any yield, so a client
                        # that stops reading at tool_calls does not orphan it
                        finished = True
                        if hashes is not None and sess_key in self.sessions:
                            entry = self.sessions.pop(sess_key)
                            entry["hashes"] = hashes
                            entry["last"] = time.time()
                            self.sessions[(model or "", hashes)] = entry
                        log(f"[{self.profile}] turn cache="
                            f"{'hit' if reused else 'miss'} sent={len(prompt_text)}B "
                            f"new={t_new * 1000:.0f}ms first={first_chunk:.2f}s "
                            f"total={time.time() - t_start:.2f}s")
                        if not emitted:
                            usable = [t for t in tools_out.values()
                                      if t["function"]["name"]]
                            if usable:
                                # the agent proposed tool calls instead of prose;
                                # hand them to the client as OpenAI tool_calls
                                yield {"__tools__": usable}
                            else:
                                log(f"[{self.profile}] EMPTY turn, updates={seen}")
                                if thoughts:
                                    yield "".join(thoughts)
                        yield {"__done__": True}
                        return
            finally:
                if sid:
                    self.turn_q.pop(sid, None)
                    if not finished or hashes is None:
                        self._drop_session_by_sid(sid)
        finally:
            self.busy = False
            self.lock.release()


# ---------------------------------------------------------------- pool
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profile-state.json")


class ProfilePool:
    def __init__(self):
        self.workers = {}
        self.tested = {}
        self.rr = 0
        self.lock = threading.Lock()
        self._load()

    def _load(self):
        try:
            with open(STATE_FILE) as f:
                self.tested = json.load(f) or {}
        except Exception:
            self.tested = {}

    def _save(self):
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.tested, f, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            log(f"state save: {e}")

    def set_primary(self, profile):
        self.tested.setdefault("_primary", {})["profile"] = profile
        self._save()

    def mark(self, profile, status, detail=""):
        if profile == "_primary":
            return
        self.tested[profile] = {"status": status, "detail": detail[:200],
                                "at": time.time()}
        self._save()

    def cooldown(self, profile, seconds, reason=""):
        """Park a profile for a while after a transient failure (429, timeout)."""
        if profile == "_primary":
            return
        self.tested.setdefault(profile, {})["cooldown_until"] = time.time() + seconds
        self.tested[profile]["cooldown_reason"] = reason[:160]
        self._save()

    def ensure(self, profile):
        with self.lock:
            ws = self.workers.get(profile)
            if ws and all(w.alive() for w in ws):
                return ws
            ws = []
            for i in range(WORKERS_PER_PROFILE):
                try:
                    ws.append(AgyWorker(profile, i))
                except Exception as e:
                    log(f"[{profile}] worker spawn failed: {e}")
            self.workers[profile] = ws
            return ws

    # ---- ordering: one primary, the rest are fallback -------------------
    def primary(self):
        forced = os.environ.get("AGY_PRIMARY_PROFILE", "").strip()
        if forced and forced in list_profiles():
            return forced
        saved = (self.tested.get("_primary") or {}).get("profile")
        if saved and saved in list_profiles() and \
                self.tested.get(saved, {}).get("status") not in ("tos", "verify"):
            return saved
        for p, st in self.tested.items():
            if p != "_primary" and st.get("status") == "ok":
                return p
        return None

    def order(self, prefer=None):
        """Every usable profile, primary first. Fallbacks only run when the
        primary fails, so the same account is not hammered concurrently but
        also does not sit idle while a healthy one exists."""
        bad = {"tos", "verify"}
        names = list_profiles()
        now = time.time()

        def usable(n):
            st = self.tested.get(n, {})
            if st.get("status") in bad:
                return False
            cd = st.get("cooldown_until") or 0
            return cd <= now

        ok = [n for n in names if self.tested.get(n, {}).get("status") == "ok" and usable(n)]
        untested = [n for n in names if self.tested.get(n, {}).get("status") not in bad
                    and self.tested.get(n, {}).get("status") != "ok" and usable(n)]
        prim = self.primary()
        ordered = ([prim] if prim else []) + [n for n in ok if n != prim] + untested
        if prefer:
            ordered = ([prefer] if prefer in ordered else []) + \
                      [n for n in ordered if n != prefer]
        if not ordered:
            raise RuntimeError("no usable profile")
        return ordered

    def pick(self, prefer=None):
        """Primary only — kept for /admin/test and single-shot calls."""
        name = self.order(prefer)[0]
        ws = [w for w in self.ensure(name) if w.alive()]
        if not ws:
            raise RuntimeError(f"profile {name} has no live worker")
        return name, ws[0]


POOL = ProfilePool()


BANNER_TOS = "terms of service"
BANNER_VERIFY = ("verification required", "verify your account")


def turn_with_fallback(prompt, model=None, prefer=None, timeout=None, state=None, images=None,
                       segments=None, suffix="", client_tools=False):
    """Yield text pieces for one turn, walking the profile list as a fallback
    chain. The primary carries the traffic; the next profile is only touched
    when the primary fails *before* delivering usable text.

    Google's ban banners arrive as ordinary assistant text, so the first slice
    of the answer is buffered and inspected: if it is a ToS/verification notice,
    the profile is marked and the turn is retried on the next profile instead of
    handing the banner back to the caller as if it were an answer.
    """
    errors = []
    for name in POOL.order(prefer):
        worker = None
        try:
            ws = [w for w in POOL.ensure(name) if w.alive()]
            if not ws:
                errors.append(f"{name}: no live worker")
                continue
            worker = next((w for w in ws if not w.busy), ws[0])
            if state is not None:
                state["profile"] = name
                state["fallback"] = bool(errors)
            gen = worker.chat(prompt, model=model, timeout=timeout, images=images,
                              segments=segments, suffix=suffix, client_tools=client_tools)
            try:
                yield from _drain(gen, name)
                return
            finally:
                # closing the generator runs its finally block and releases the
                # worker lock even when the caller stops iterating (client gone)
                gen.close()
        except Exception as e:
            msg = str(e)
            low = msg.lower()
            if "tos banner" not in low and "verify banner" not in low:
                log(f"[{name}] turn failed, falling back: {msg[:160]}")
                if "429" in msg or "quota" in low or "rate" in low or "busy" in low:
                    POOL.cooldown(name, 60 if "busy" in low else 300, msg)
            errors.append(f"{name}: {msg[:120]}")
    raise RuntimeError("all profiles failed — " + "; ".join(errors))


def _drain(gen, name):
    """Buffer the head of a turn, reject ban banners, then stream the rest."""
    buf = ""
    settled = False
    for piece in gen:
        if isinstance(piece, dict) and "__tools__" in piece:
            yield piece
            break
        if not isinstance(piece, str):
            break
        if settled:
            yield piece
            continue
        buf += piece
        low = buf.lower()
        if len(buf) >= 160:
            settled = True
        elif any(b in low for b in BANNER_VERIFY):
            settled = True
        elif BANNER_TOS in low:
            settled = True
        if settled and BANNER_TOS in low:
            POOL.mark(name, "tos", buf.strip()[:160])
            raise RuntimeError("tos banner")
        if settled and any(b in low for b in BANNER_VERIFY):
            POOL.mark(name, "verify", buf.strip()[:160])
            raise RuntimeError("verify banner")
        if settled:
            yield buf
    if buf and not settled:
        low = buf.lower()
        if BANNER_TOS in low:
            POOL.mark(name, "tos", buf.strip()[:160])
            raise RuntimeError("tos banner")
        if any(b in low for b in BANNER_VERIFY):
            POOL.mark(name, "verify", buf.strip()[:160])
            raise RuntimeError("verify banner")
        yield buf


# ---------------------------------------------------------------- helpers
TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
TOOL_CALL_JSON_RE = re.compile(
    r"\{\s*\"id\"\s*:\s*\"[^\"]+\"\s*,\s*\"type\"\s*:\s*\"function\"\s*,"
    r"\s*\"function\"\s*:\s*\{.*?\}\s*\}", re.DOTALL)


def tools_prompt(tools, tool_choice=None):
    """Describe the caller's functions and demand OpenAI-shaped tool_call blocks.

    agy only knows its OWN built-in tools, so the caller's schemas cannot be
    handed over through ACP without an MCP bridge. Prompted tool calling works
    with any model. The contract is delimiter-based (<tool_call>...</tool_call>) rather than
    "reply with only JSON": prose and several calls can coexist, and the blocks
    are cut out of the answer so raw JSON never reaches the client as content.
    """
    specs = []
    for t in tools or []:
        fn = (t or {}).get("function") or {}
        if not fn.get("name"):
            continue
        specs.append({"name": fn["name"],
                      "description": fn.get("description", ""),
                      "parameters": fn.get("parameters", {})})
    if not specs:
        return ""
    example = ('<tool_call>{"id":"call_1","type":"function","function":{"name":"FN",'
               '"arguments":"{\\"key\\":\\"value\\"}"}}</tool_call>')
    out = ("\n\n[Available functions]\n" + json.dumps(specs, ensure_ascii=False) +
           "\n\nTo call one, emit <tool_call>...</tool_call> blocks: one JSON object per call, "
           "exactly in OpenAI function-call shape, e.g.\n" + example +
           "\narguments MUST be a JSON string. Several blocks and prose around them "
           "are fine. You have NO built-in tools in this mode: run_command, read_file, "
           "search and every other internal tool are unavailable and must not be used. "
           "The functions above are the only way to act. If no function is needed, "
           "answer in plain text with no tool_call block.]")
    if tool_choice is not None:
        out += "\n[Tool choice hint: " + json.dumps(tool_choice, ensure_ascii=False) + "]"
    return out


def _tool_call_obj(obj, allowed=None):
    """One parsed JSON object -> OpenAI tool_call, or None. Accepts the OpenAI
    shape and the older bare {name, arguments} envelope."""
    if not isinstance(obj, dict):
        return None
    fn = obj.get("function")
    if not isinstance(fn, dict):
        name = obj.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        fn = {"name": name, "arguments": obj.get("arguments", obj.get("args", {}))}
        obj = {}
    name = fn.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    if allowed is not None and name not in allowed:
        log(f"tool call for unknown function {name!r} dropped")
        return None
    args = fn.get("arguments", "{}")
    if not isinstance(args, str):
        args = json.dumps(args, ensure_ascii=False)
    cid = obj.get("id")
    if not isinstance(cid, str) or not cid.strip():
        cid = "call_" + uuid.uuid4().hex[:16]
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": args}}


def _strip_spans(text, spans):
    spans = sorted(spans)
    merged = []
    for s, e in spans:
        if not merged or s > merged[-1][1]:
            merged.append((s, e))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
    parts, cur = [], 0
    for s, e in merged:
        if cur < s:
            parts.append(text[cur:s])
        cur = max(cur, e)
    if cur < len(text):
        parts.append(text[cur:])
    return "\n".join(p.strip() for p in parts if p and p.strip()).strip()


def _parse_bare_calls(text, allowed):
    """Bare {name, arguments} objects — the shape agent CLIs like OpenCode ask
    their models for, ignoring this bridge's tag contract.

    Only accepted when the name matches a declared function, so a JSON-mode
    answer such as {"city":"Jakarta","temp":30} is never mistaken for a call.
    """
    if not allowed or not isinstance(text, str) or not text.strip():
        return None, text
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    base = text.find(s)
    if base < 0:
        base = 0
    cand, span = None, None
    try:
        cand, span = json.loads(s), (base, base + len(s))
    except Exception:
        for op, cl in (("{", "}"), ("[", "]")):
            i, j = s.find(op), s.rfind(cl)
            if i >= 0 and j > i:
                try:
                    cand = json.loads(s[i:j + 1])
                    span = (base + i, base + j + 1)
                    break
                except Exception:
                    continue
    if cand is None:
        return None, text
    items = cand if isinstance(cand, list) else [cand]
    calls = [c for c in (_tool_call_obj(x, allowed) for x in items) if c]
    if not calls:
        return None, text
    if span and (span[0] > 0 or span[1] < len(text)):
        cleaned = (text[:span[0]] + " " + text[span[1]:]).strip()
    else:
        cleaned = ""
    return calls, cleaned


def extract_tool_calls(text, allowed=None):
    """(tool_calls, cleaned_text). Consumed blocks are removed from the answer.

    Order: <tool_call> blocks, then bare OpenAI-shaped JSON, then the legacy
    {"tool_calls":[...]} envelope. The first pattern that yields a call wins.
    """
    if not isinstance(text, str) or not text.strip():
        return [], (text or "").strip()
    calls, spans = [], []
    for pattern, group in ((TOOL_CALL_BLOCK_RE, 1), (TOOL_CALL_JSON_RE, 0)):
        for m in pattern.finditer(text):
            try:
                obj = json.loads(m.group(group))
            except Exception:
                obj = None
            if obj is not None:
                c = _tool_call_obj(obj, allowed)
                if c:
                    calls.append(c)
            spans.append((m.start(), m.end()))
        if calls:
            break
    if calls:
        return calls, _strip_spans(text, spans)
    # legacy envelope: {"tool_calls":[{"name":...,"arguments":{...}}]}
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        body = s[i:j + 1]
        try:
            d = json.loads(body)
        except Exception:
            d = None
        if isinstance(d, dict) and isinstance(d.get("tool_calls"), list):
            out = [c for c in (_tool_call_obj(x, allowed) for x in d["tool_calls"]) if c]
            if out:
                cleaned = (s[:i] + " " + s[j + 1:]).strip()
                return out, cleaned
    # some agent CLIs (OpenCode et al.) put their own tool format in the model's
    # system prompt, so the reply is a bare {name, arguments} object with no tag
    # and no wrapper. Accept it only against the caller's declared names.
    bare, cleaned = _parse_bare_calls(text, allowed)
    if bare:
        return bare, cleaned
    return [], text.strip()


def parse_tool_reply(text):
    """Back-compat wrapper: only the calls, no cleaned text."""
    return extract_tool_calls(text)[0] or None


def apply_output_limits(text, max_tokens=None, stop=None):
    """Emulate stop sequences and max_tokens, which the CLI does not accept."""
    finish = "stop"
    cut = None
    for s in (stop if isinstance(stop, list) else [stop] if stop else []):
        if s and s in text:
            i = text.index(s)
            cut = i if cut is None else min(cut, i)
    if cut is not None:
        text = text[:cut]
    if max_tokens:
        # ~4 chars per token, same approximation the usage field already uses
        limit = int(max_tokens) * 4
        if len(text) > limit:
            text = text[:limit]
            finish = "length"
    return text, finish


def _image_block(part):
    """OpenAI image_url part -> ACP image ContentBlock (base64 + mimeType).

    ACP's ContentBlock is a superset of MCP's: {type:image, data, mimeType}.
    Remote URLs are fetched here because the agent process has no business
    reaching out to arbitrary hosts on the caller's behalf.
    """
    iu = part.get("image_url") or part.get("imageUrl") or {}
    url = iu.get("url") if isinstance(iu, dict) else iu
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        try:
            header, b64 = url.split(",", 1)
            mime = header[5:].split(";")[0] or "image/png"
            return {"type": "image", "data": b64, "mimeType": mime}
        except Exception:
            return None
    if url.startswith(("http://", "https://")):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "agy-acp-bridge"})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                mime = r.headers.get("Content-Type", "image/png").split(";")[0]
            return {"type": "image",
                    "data": base64.b64encode(raw).decode(),
                    "mimeType": mime}
        except Exception as e:
            log(f"image fetch failed: {str(e)[:120]}")
            return None
    # bare base64
    return {"type": "image", "data": url, "mimeType": part.get("mimeType") or "image/png"}


def flatten(messages):
    """Return (prompt_text, image_blocks). Images used to be silently dropped,
    which is why the agent answered that it could not see the picture."""
    sys_parts, convo, images = [], [], []
    for m in messages:
        role = m.get("role", "user")
        c = m.get("content")
        if isinstance(c, list):
            texts = []
            for p in c:
                if not isinstance(p, dict):
                    continue
                kind = p.get("type")
                if kind == "text":
                    texts.append(p.get("text", ""))
                elif kind in ("image_url", "image", "input_image"):
                    blk = _image_block(p)
                    if blk:
                        images.append(blk)
                        texts.append("[image attached]")
            text = "\n".join(texts)
        else:
            text = c or ""
        if role == "system":
            sys_parts.append(text)
        elif role == "assistant":
            convo.append(f"Assistant: {text}")
        else:
            convo.append(f"User: {text}")
    body = "\n\n".join(convo).strip()
    if sys_parts:
        body = "[System instruction]\n" + "\n".join(sys_parts) + "\n\n" + body
    return body, images


_model_cache = {"at": 0, "models": []}


def models_for(profile=None):
    if _model_cache["models"] and time.time() - _model_cache["at"] < 300:
        return _model_cache["models"]
    args = [AGY, "models"]
    p = profile or (list_profiles() or ["default"])[0]
    if p != "default":
        args += ["--app-data-dir", profile_name(p)]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=40,
                             env={**os.environ, "HOME": os.environ.get("HOME", "/root")})
        ms = []
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if not line or line.lower().startswith(("fetching", "error")):
                continue
            tok = line.split("\t")[0].split()[0].strip()
            if tok:
                ms.append(tok)
        ms = list(dict.fromkeys(ms))
        if ms:
            _model_cache.update(at=time.time(), models=ms)
    except Exception as e:
        log(f"models: {e}")
    return _model_cache["models"]


ADMIN_HTML = """<!doctype html><html><head><meta charset=utf-8><title>agy acp bridge</title>
<style>body{font:14px/1.5 system-ui,#111;background:#0b0c10;color:#e8e8ea;margin:0;padding:28px}
h1{font-size:18px;margin:0 0 18px}table{border-collapse:collapse;width:100%;margin-top:10px}
td,th{border-bottom:1px solid #23252c;padding:8px;text-align:left;font-weight:400}th{color:#8b8f9a}
input,button{font:inherit;background:#161821;color:#e8e8ea;border:1px solid #2c2f3a;padding:7px 10px;border-radius:6px}
button{background:#2b6cff;border-color:#2b6cff;cursor:pointer}button:disabled{opacity:.5}
.url{word-break:break-all;background:#11131a;padding:10px;border-radius:6px;margin:10px 0;font-size:12px}
.ok{color:#45d483}.bad{color:#ff6b6b}.muted{color:#8b8f9a}
</style></head><body>
<h1>agy acp bridge &mdash; accounts</h1>
<table id=t><thead><tr><th>profile</th><th>status</th><th>last test</th><th></th></tr></thead><tbody></tbody></table>
<h3>running logins</h3><div id=run class=muted>none</div>
<h3>add account</h3>
<input id=prof placeholder="profile name, e.g. permainan10st"><button onclick=start()>start login</button>
<div id=box></div>
<script>
const j=async(u,b)=>{const r=await fetch(u,{method:b?'POST':'GET',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});return r.json()};
async function load(){const d=await j('/admin/profiles');const tb=document.querySelector('#t tbody');tb.innerHTML='';
for(const p of d.profiles){const tr=document.createElement('tr');
tr.innerHTML='<td>'+(p.primary?'&#9733; ':'')+p.name+'</td><td class="'+(p.status==='ok'?'ok':(p.status==='tos'||p.status==='verify')?'bad':'muted')+'">'+p.status+'</td><td class=muted>'+(p.detail||'')+'</td><td><button onclick="test(\''+p.name+'\')">test turn</button> '+(p.primary?'':'<button onclick="primary(\''+p.name+'\')">make primary</button>')+'</td>';tb.appendChild(tr)}}
async function primary(name){const r=await j('/admin/primary',{profile:name});alert(r.ok?('primary: '+r.primary):('failed: '+r.detail));load()}
async function start(){const name=document.getElementById('prof').value.trim();if(!name)return;
const d=await j('/admin/login/start',{profile:name});const box=document.getElementById('box');
if(!d.ok){box.innerHTML='<div class=bad>'+d.error+'</div>';return}
box.innerHTML='<div class=muted>open this, sign in, paste the code below</div><div class=url>'+(d.url||'waiting...')+'</div><input id=code placeholder="4/0A..."><button onclick="done(\''+d.loginId+'\')">submit code</button><div id=res></div>'}
async function done(id){const code=document.getElementById('code').value.trim();
const r=await j('/admin/login/complete',{loginId:id,code});document.getElementById('res').innerHTML='<span class="'+(r.ok?'ok':'bad')+'">'+(r.detail||'')+'</span>';load()}
async function running(){const d=await j('/admin/logins');const el=document.getElementById('run');
if(!d.logins.length){el.textContent='none';return}
el.innerHTML=d.logins.map(l=>'<div style="margin:8px 0"><b>'+l.profile+'</b><div class=url>'+(l.url||'waiting...')+'</div>'+
'<input id=c_'+l.id+' placeholder="paste code"><button onclick="fill(\''+l.id+'\')">submit</button></div>').join('')}
async function fill(id){const code=document.getElementById('c_'+id).value.trim();
const r=await j('/admin/login/complete',{loginId:id,code});alert(r.ok?('logged in: '+r.detail):('failed: '+r.detail));running();load()}
async function test(name){const r=await j('/admin/test',{profile:name,model:'claude-sonnet-4-6'});alert((r.ok?'OK: ':'FAIL: ')+r.detail);load()}
load();running();setInterval(running,4000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _log_req(self):
        log(f"{self.command} {self.path} from {self.client_address[0]} "
            f"ua={(self.headers.get('User-Agent') or '-')[:40]}")

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _admin_ok(self):
        tok = os.environ.get("AGY_ADMIN_TOKEN", "")
        if not tok:
            return True
        return ("t=" + tok) in (self.path or "")

    def _deny_admin(self):
        self._json(403, {"ok": False, "error": "admin token required"})
        return None

    def do_GET(self):
        if self.path.startswith("/admin") and not self._admin_ok():
            return self._deny_admin()
        if self.path.startswith("/admin/login/"):
            lid = self.path.rsplit("/", 1)[-1]
            lg = LOGINS.get(lid)
            if not lg:
                return self._json(404, {"ok": False})
            return self._json(200, {"ok": True, "url": lg.url, "done": lg.done,
                                    "result": lg.ok, "detail": lg.detail})
        if self.path.startswith("/admin/logins"):
            return self._json(200, {"logins": [
                {"id": k, "profile": v.profile, "url": v.url,
                 "done": v.done, "result": v.ok, "detail": v.detail}
                for k, v in LOGINS.items() if not v.done]})
        if self.path.startswith("/admin/profiles"):
            profs = []
            prim = POOL.primary()
            for p in list_profiles():
                st = POOL.tested.get(p) or {"status": "untested", "detail": ""}
                profs.append({"name": p, "status": st.get("status"),
                              "primary": p == prim,
                              "detail": st.get("detail", "")[:120]})
            return self._json(200, {"profiles": profs, "primary": prim})
        if self.path.startswith("/admin"):
            b = ADMIN_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if self.path.startswith("/health"):
            return self._json(200, {
                "ok": True, "profiles": list_profiles(),
                "workers": {k: [w.alive() for w in v] for k, v in POOL.workers.items()},
                "cache": {k: dict(w.stats, sessions=len(w.sessions)) for k, ws in POOL.workers.items()
                          for w in ws[:1]}})
        if self.path.startswith("/v1/models"):
            return self._json(200, {"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": "antigravity"} for m in models_for()]})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        self._log_req()
        if self.path.startswith("/admin") and not self._admin_ok():
            return self._deny_admin()
        if self.path.startswith("/admin/login/start"):
            b = self._body()
            prof = (b.get("profile") or "").strip().replace("/", "_")[:40]
            if not prof:
                return self._json(400, {"ok": False, "error": "profile required"})
            lg = Login(prof)
            LOGINS[lg.id] = lg
            for _ in range(80):
                if lg.url or lg.done:
                    break
                time.sleep(0.1)
            return self._json(200, {"ok": True, "loginId": lg.id, "url": lg.url})
        if self.path.startswith("/admin/login/complete"):
            b = self._body()
            lg = LOGINS.get(b.get("loginId"))
            if not lg:
                return self._json(404, {"ok": False, "detail": "unknown login"})
            lg.submit(b.get("code") or "")
            for _ in range(300):
                if lg.done:
                    break
                time.sleep(0.1)
            return self._json(200, {"ok": bool(lg.ok), "detail": lg.detail})
        if self.path.startswith("/admin/primary"):
            b = self._body()
            prof = (b.get("profile") or "").strip()
            if prof not in list_profiles():
                return self._json(400, {"ok": False, "detail": "unknown profile"})
            POOL.set_primary(prof)
            log(f"primary profile set to {prof}")
            return self._json(200, {"ok": True, "primary": prof})
        if self.path.startswith("/admin/test"):
            b = self._body()
            prof = b.get("profile") or "default"
            model = b.get("model") or "claude-sonnet-4-6"
            try:
                ws = [w for w in POOL.ensure(prof) if w.alive()]
                if not ws:
                    raise RuntimeError("no live worker")
                text = "".join(x for x in ws[0].chat("Reply with exactly: OK", model=model,
                                                     timeout=120) if isinstance(x, str))
                low = text.lower()
                if "terms of service" in low:
                    status = "tos"
                elif "verification required" in low or "verify your account" in low:
                    status = "verify"     # alive but needs Google account verification
                elif not text.strip():
                    status = "error"
                else:
                    status = "ok"
                POOL.mark(prof, status, text.strip()[:160])
                return self._json(200, {"ok": status == "ok", "status": status,
                                        "detail": text.strip()[:200]})
            except Exception as e:
                POOL.mark(prof, "error", str(e)[:160])
                return self._json(200, {"ok": False, "detail": str(e)})
        if self.path.startswith("/v1/chat/completions"):
            return self._chat()
        return self._json(404, {"error": "not found"})

    def _chat(self):
        body = self._body()
        messages = body.get("messages") or []
        prompt, images = flatten(messages)
        tools = body.get("tools") or []
        max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")
        stop = body.get("stop")
        tools_text = tools_prompt(tools, body.get("tool_choice")) if tools else ""
        allowed = {((t or {}).get("function") or {}).get("name") for t in tools} if tools else None
        if tools_text:
            prompt += tools_text
        raw_model = (body.get("model") or "").strip()
        prefer = None
        if "/" in raw_model:
            prefer, raw_model = raw_model.split("/", 1)
        stream = bool(body.get("stream"))
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        # agy's session default is a slow gemini tier; pin the fast model when
        # callers omit `model`. Explicit model ids still pass through verbatim.
        model = raw_model or "claude-sonnet-4-6"
        prof = prefer or "?"
        state = {}
        gen = None
        try:
            gen = turn_with_fallback(prompt, model=model, prefer=prefer, state=state,
                                     images=images, segments=messages, suffix=tools_text,
                                     client_tools=bool(tools))
            if not stream:
                parts, tcalls = [], []
                for piece in gen:
                    if isinstance(piece, str):
                        parts.append(piece)
                    elif isinstance(piece, dict) and "__tools__" in piece:
                        tcalls = piece["__tools__"]
                text = "".join(parts)
                if tools and not tcalls:
                    tcalls, text = extract_tool_calls(text, allowed)
                text, finish = apply_output_limits(text, max_tokens, stop)
                if tcalls:
                    finish = "tool_calls"
                prof = state.get("profile", prof)
                message = {"role": "assistant", "content": text}
                if tcalls:
                    message["tool_calls"] = tcalls
                return self._json(200, {
                    "id": cid, "object": "chat.completion", "created": created,
                    "model": raw_model or "agy", "profile": prof,
                    "fallback": state.get("fallback", False),
                    "choices": [{"index": 0, "message": message,
                                 "finish_reason": finish}],
                    "usage": {"prompt_tokens": len(prompt) // 4,
                              "completion_tokens": len(text) // 4,
                              "total_tokens": (len(prompt) + len(text)) // 4},
                })
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def sse(o):
                self.wfile.write(("data: " + json.dumps(o) + "\n\n").encode())
                self.wfile.flush()

            sse({"id": cid, "object": "chat.completion.chunk", "created": created,
                 "model": raw_model or "agy",
                 "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
            tcalls = []
            if tools or max_tokens or stop:
                # these need the whole answer before it can be judged: a prompted
                # tool call is only valid once the JSON is complete, and a stop
                # sequence may straddle two chunks. Buffer, then emit.
                parts = []
                for piece in gen:
                    if isinstance(piece, dict) and "__tools__" in piece:
                        tcalls = piece["__tools__"]
                    elif isinstance(piece, str):
                        parts.append(piece)
                text = "".join(parts)
                if tools and not tcalls:
                    tcalls, text = extract_tool_calls(text, allowed)
                text, finish = apply_output_limits(text, max_tokens, stop)
                if tcalls:
                    finish = "tool_calls"
                if text:
                    sse({"id": cid, "object": "chat.completion.chunk", "created": created,
                         "model": raw_model or "agy", "profile": state.get("profile"),
                         "choices": [{"index": 0, "delta": {"content": text},
                                      "finish_reason": None}]})
            else:
                finish = "stop"
                for piece in gen:
                    if isinstance(piece, dict) and "__tools__" in piece:
                        tcalls = piece["__tools__"]
                        continue
                    if not isinstance(piece, str):
                        continue
                    sse({"id": cid, "object": "chat.completion.chunk", "created": created,
                         "model": raw_model or "agy", "profile": state.get("profile"),
                         "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]})
                if tcalls:
                    finish = "tool_calls"
            if tcalls:
                sse({"id": cid, "object": "chat.completion.chunk", "created": created,
                     "model": raw_model or "agy",
                     "choices": [{"index": 0, "delta": {"tool_calls": [
                         {"index": i, **t} for i, t in enumerate(tcalls)]},
                         "finish_reason": None}]})
            sse({"id": cid, "object": "chat.completion.chunk", "created": created,
                 "model": raw_model or "agy",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log("client disconnected mid-turn")
        except Exception as e:
            log(f"turn error: {e}")
            try:
                if stream:
                    self.wfile.write(("data: " + json.dumps({"error": {"message": str(e)}}) +
                                      "\n\n").encode())
                    self.wfile.flush()
                else:
                    self._json(500, {"error": {"message": str(e)}})
            except Exception:
                pass
        finally:
            # releasing the generator releases the worker lock; without this a
            # dropped client leaves the worker locked and later turns time out
            try:
                if gen is not None:
                    gen.close()
            except Exception:
                pass


def prewarm():
    """Spawn the primary profile's workers before the first real request.

    The first session/new inside a fresh agy process costs ~1.5s because the
    CLI creates its workspace; later ones are ~40ms. Paying that at boot turns
    the first user request into a normal-latency one.
    """
    try:
        names = POOL.order()
    except Exception as e:
        log(f"[prewarm] skipped: {e}")
        return
    for name in names[:PREWARM_PROFILES]:
        t0 = time.time()
        try:
            ws = [w for w in POOL.ensure(name) if w.alive()]
            for w in ws:
                w.warm()
            log(f"[prewarm] {name}: {len(ws)} worker(s) ready in {time.time() - t0:.2f}s")
        except Exception as e:
            log(f"[prewarm] {name}: {str(e)[:120]}")


def main():
    log(f"agy-acp-bridge v2 on 127.0.0.1:{PORT}, profiles={list_profiles()}")
    bind = os.environ.get("AGY_BRIDGE_BIND", "127.0.0.1")
    srv = ThreadingHTTPServer((bind, PORT), Handler)
    srv.daemon_threads = True
    if PREWARM:
        threading.Thread(target=prewarm, daemon=True).start()
    log("listening")
    srv.serve_forever()


if __name__ == "__main__":
    main()
