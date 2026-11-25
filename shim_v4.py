#!/usr/bin/env python3
"""
mlai-shim 4.0 — OpenAI-compatible bridge for ML AI Code.

Fixes over 3.0:
  * serialized daemon access (daemon rejects concurrent completions)
  * long prompts truncated in the middle instead of failing
  * max_tokens honoured (truncate + finish_reason=length)
  * unknown model -> 404 model_not_found
  * CORS + OPTIONS
  * client-disconnect (BrokenPipe) handled
  * bounded queue -> 429 when overloaded
  * empty messages -> 400

    GET  /v1/models
    POST /v1/chat/completions
    GET  /health
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("MLAI_SHIM_HOST", "127.0.0.1")
PORT = int(os.environ.get("MLAI_SHIM_PORT", "8787"))
TIMEOUT = int(os.environ.get("MLAI_SHIM_TIMEOUT", "600"))
MLAI_BIN = os.environ.get("MLAI_BIN") or shutil.which("mlai") or "mlai"
SOCKET = os.environ.get("MLAI_SOCKET",
                        os.path.join(os.path.expanduser("~"), ".mlai-shim", "daemon.sock"))
EXTRA_ARGS = os.environ.get("MLAI_SHIM_ARGS", "").split()
WORKDIR = os.environ.get("MLAI_SHIM_WORKDIR",
                         os.path.join(os.path.expanduser("~"), "mlai-shim", "scratch"))
os.makedirs(WORKDIR, exist_ok=True)
DEFAULT_MODEL = os.environ.get("MLAI_SHIM_MODEL", "pixis")
EFFORT = os.environ.get("MLAI_SHIM_EFFORT", "low")
TIER = os.environ.get("MLAI_SHIM_TIER", "fast")
# Above this many characters the prompt is trimmed in the middle.
# Measured: the daemon answers reliably up to ~6k chars and fails from ~8k.
MAX_PROMPT_CHARS = int(os.environ.get("MLAI_SHIM_MAX_PROMPT", "6000"))
# How many requests may wait for the daemon before we answer 429.
MAX_QUEUE = int(os.environ.get("MLAI_SHIM_MAX_QUEUE", "16"))
CORS_ORIGIN = os.environ.get("MLAI_SHIM_CORS", "*")

MODELS = {
    "pixis": "Pixis (cepat)",
    "pixis-sol": "GPT 6.1R Sol",
    "pixis-opus": "Opus 5.5R",
    "ml-ai-forge": "ML.ai Forge",
    "gpt-6-astra": "GPT 6 Astra",
    "grok-4.7": "Grok 4.7",
    "kimi-k3": "ML.ai Coder",
    "glm-5.3": "GLM 5.3",
    "claude-sonnet-5.5": "Claude Sonnet 5.5",
    "claude-fable-5.2": "Claude Fable 5.2",
}

GUARD = ("You are answering a plain chat request through an API. "
         "Do not read, list or inspect any files, and do not talk about the folder. "
         "Just answer the request directly and reply with the answer text only.")

# The daemon drops a completion when several arrive at once, so every daemon
# call goes through this lock. Measured: 5 parallel -> 1 ok; serialized -> 5 ok.
_DAEMON_LOCK = threading.Lock()
_DAEMON_LAST = [0.0]
# Even when serialized, calls fired back-to-back make the daemon answer empty.
# Measured with a 0.35s floor between calls: 16/16 succeed on the first attempt.
DAEMON_MIN_GAP = float(os.environ.get("MLAI_SHIM_MIN_GAP", "0.35"))
_QUEUE = threading.BoundedSemaphore(MAX_QUEUE)

_state = {"ok": False, "checked": 0.0, "cooldown": 0.0}
_state_lock = threading.Lock()


class _daemon_slot:
    """Serialise daemon access and keep a minimum gap between calls.

    The gap grows after a failed completion: a burst of traffic leaves the
    daemon answering empty for a while, and backing off is what recovers it.
    """

    def __enter__(self):
        _DAEMON_LOCK.acquire()
        wait = DAEMON_MIN_GAP - (time.time() - _DAEMON_LAST[0])
        if wait > 0:
            time.sleep(wait)
        return self

    def __exit__(self, *exc):
        _DAEMON_LAST[0] = time.time()
        _DAEMON_LOCK.release()
        return False


def _cool_down(seconds):
    """Make every thread wait: the daemon needs a pause after empty answers."""
    with _state_lock:
        _state["cooldown"] = max(_state.get("cooldown", 0.0), time.time() + seconds)


def _wait_cooldown():
    with _state_lock:
        until = _state.get("cooldown", 0.0)
    delay = until - time.time()
    if delay > 0:
        time.sleep(min(delay, 5.0))


class ShimError(Exception):
    def __init__(self, msg, code="upstream_error", status=502):
        super().__init__(msg)
        self.code = code
        self.status = status


# ------------------------------------------------------------------ socket HTTP
def _sock_http(method, path, body=None, timeout=30):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCKET)
    except OSError as e:
        s.close()
        raise ShimError("daemon socket %s: %s" % (SOCKET, e))
    data = json.dumps(body).encode() if body is not None else b""
    req = ("%s %s HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
           "Content-Length: %d\r\nConnection: close\r\n\r\n" % (method, path, len(data)))
    s.sendall(req.encode() + data)
    buf = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        pass
    finally:
        s.close()

    text = buf.decode("utf-8", "replace")
    _, _, payload = text.partition("\r\n\r\n")
    if payload and "\r\n" in payload:
        payload = "".join(l for l in payload.split("\r\n")
                          if not re.fullmatch(r"[0-9a-fA-F]+", l.strip() or "x"))
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        raise ShimError("bad JSON from daemon: %s" % payload[:200])


def daemon_alive(ttl=30):
    """True when the daemon socket accepts a connection.

    This uses a bare connect (no HTTP request) so the health probe never
    competes with a real completion for the daemon slot: an extra request
    between completions was enough to make the next one come back empty.

    A negative result is cached only briefly — at boot the shim starts before
    the daemon is listening, and caching that `false` for the full TTL made
    every request fall back to the slow CLI tier.
    """
    now = time.time()
    with _state_lock:
        age = now - _state["checked"]
        if _state["ok"] and age < ttl:
            return True
        if not _state["ok"] and age < 3:
            return False
    ok = False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(SOCKET)
        s.close()
        ok = True
    except OSError:
        ok = False
    with _state_lock:
        _state["ok"] = ok
        _state["checked"] = time.time()
    return ok


# ------------------------------------------------------- tier 1: raw completion
def fast_complete(prompt, model, attempts=4):
    """Raw model completion: no agent loop, no intent pass.

    The prompt must end with an `Assistant:` marker or the daemon answers with
    no items. About 1 in 6 prompts still comes back empty and an identical retry
    always fails too — nudging the tail unsticks it.

    The whole retry loop holds one daemon slot: releasing it between attempts
    lets other threads fire bursts at the daemon, which then answers empty.
    """
    tails = ["", "\n", " Jawab:", "\n\n"]
    _wait_cooldown()
    with _daemon_slot():
        for i in range(attempts):
            body = {"prefix": prompt + tails[i % len(tails)], "model": model,
                    "stop": ["\nUser:", "\nSystem:"]}
            r = _sock_http("POST", "/v1/complete", body, timeout=TIMEOUT)
            items = r.get("items") or []
            text = "".join(i2.get("text", "") for i2 in items if isinstance(i2, dict))
            if text.strip():
                return text.strip()
            if i < attempts - 1:
                time.sleep(0.2 * (1.6 ** i))
    # Every attempt came back empty: pause all callers so the daemon can settle.
    _cool_down(float(os.environ.get("MLAI_SHIM_COOLDOWN", "3")))
    raise ShimError("empty completion from daemon after %d attempts" % attempts)


# ------------------------------------------------------- tier 2: agent session
def agent_turn(prompt, model, on_partial=None):
    with _daemon_slot():
        sid = _sock_http("POST", "/v1/sessions", {"cwd": WORKDIR}, timeout=20)["id"]
    body = {"task": prompt, "model": model, "mode": "ask"}
    if EFFORT:
        body["effort"] = EFFORT
    started = time.time()
    with _daemon_slot():
        r = _sock_http("POST", "/v1/sessions/%s/messages" % sid, body, timeout=30)
    if not r.get("ok"):
        raise ShimError(str(r.get("error") or r))

    seen = 0
    last_poll = 0.0
    while time.time() - started < TIMEOUT:
        elapsed = time.time() - started
        gap = 0.08 if elapsed < 4 else (0.25 if elapsed < 12 else 0.6)
        if time.time() - last_poll < gap:
            time.sleep(0.02)
            continue
        last_poll = time.time()
        with _daemon_slot():
            tr = _sock_http("GET", "/v1/sessions/%s/transcript" % sid, timeout=30)
        if not tr:
            continue
        last = tr[-1]
        events = last.get("events") or []
        if on_partial and len(events) > seen:
            for ev in events[seen:]:
                if ev.get("type") == "inference_progress" and ev.get("content"):
                    on_partial(ev["content"])
            seen = len(events)
        state = (last.get("status") or {}).get("state")
        if state in ("done", "completed", "error", "failed"):
            if state in ("error", "failed"):
                raise ShimError(str((last.get("status") or {}).get("error") or "run failed"))
            answer = ""
            for ev in events:
                if ev.get("type") == "answer":
                    answer = ev.get("content") or ""
            if not answer:
                for ev in events:
                    if ev.get("type") == "assistant":
                        answer = ev.get("content") or ""
            return answer
    raise ShimError("timeout after %ds" % TIMEOUT)


# ------------------------------------------------------------- tier 3: CLI
def cli_turn(prompt, model):
    args = [MLAI_BIN, "-p", prompt, "--mode", "ask", "--output-format", "stream-json",
            "--no-session-persistence", "-m", model]
    if EFFORT:
        args += ["--effort", EFFORT]
    args += EXTRA_ARGS
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT, cwd=WORKDIR)
    except FileNotFoundError:
        raise ShimError("mlai CLI not found: npm install -g mlai-code")
    except subprocess.TimeoutExpired:
        raise ShimError("mlai CLI timed out after %ds" % TIMEOUT)

    answer, err = None, None
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") == "answer":
            answer = d.get("content")
        if d.get("type") == "result":
            err = d.get("error")
            if answer is None:
                answer = d.get("text")
    if err:
        raise ShimError(str(err))
    if answer is None:
        tail = (p.stderr or "no answer from mlai").strip().splitlines()
        raise ShimError(tail[-1][:400] if tail else "no answer from mlai")
    return answer


# ---------------------------------------------------------------------- helpers
def text_of(content):
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def flatten(messages):
    system_parts, convo = [], []
    for m in messages:
        content = text_of(m.get("content")).strip()
        if not content:
            continue
        if m.get("role") == "system":
            system_parts.append(content)
        elif m.get("role") == "assistant":
            convo.append("Assistant: " + content)
        else:
            convo.append("User: " + content)
    return "\n\n".join(system_parts + [GUARD] + convo).strip()


def build_chat_prefix(messages):
    """ChatML-ish transcript that /v1/complete continues (needs trailing marker)."""
    lines = []
    if not any(m.get("role") == "system" for m in messages):
        lines.append("System: " + GUARD)
    for m in messages:
        content = text_of(m.get("content")).strip()
        if not content:
            continue
        label = {"system": "System", "assistant": "Assistant"}.get(m.get("role"), "User")
        lines.append("%s: %s" % (label, content))
    lines.append("Assistant:")
    return "\n\n".join(lines)


def trim_prompt(prefix):
    """Long prompts make the daemon answer with no items; keep head and tail."""
    if len(prefix) <= MAX_PROMPT_CHARS:
        return prefix, False
    head = int(MAX_PROMPT_CHARS * 0.65)
    tail = MAX_PROMPT_CHARS - head - 80
    cut = "\n\n[... bagian tengah percakapan dipotong agar muat ...]\n\n"
    return prefix[:head] + cut + prefix[-tail:], True


def clip_text(text, max_tokens):
    """Honour max_tokens: the daemon always answers in full."""
    if not max_tokens or max_tokens <= 0:
        return text, False
    limit = int(max_tokens * 3.6)      # rough chars-per-token for mixed text
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    if " " in cut[int(limit * 0.85):]:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(), True


def resolve_model(name):
    if not name:
        return DEFAULT_MODEL
    if name.startswith("mlai/"):
        name = name[len("mlai/"):]
    if name in MODELS:
        return name
    raise ShimError("model %r is not available; use one of: %s"
                    % (name, ", ".join(sorted(MODELS))),
                    code="model_not_found", status=404)


def ask(prompt, model, chat_prefix=None, on_partial=None):
    if TIER == "fast":
        order = ["fast"]
    elif TIER == "agent":
        order = ["agent"]
    elif TIER == "cli":
        order = ["cli"]
    else:
        order = ["fast", "agent", "cli"]
    if not daemon_alive():
        order = ["cli"]
    # A prompt too long for raw completion goes straight to the agent, which
    # handles a large context without dropping the request.
    if len(chat_prefix or prompt) > MAX_PROMPT_CHARS and "agent" not in order:
        order = ["agent"]
    last_err = None
    for tier in order:
        # After a cooldown the daemon usually answers again, so the fast tier
        # gets one more chance before the request is reported as failed.
        tries = 2 if tier == "fast" else 1
        for attempt in range(tries):
            try:
                if tier == "fast":
                    return fast_complete(chat_prefix or prompt, model), "fast"
                if tier == "agent":
                    return agent_turn(prompt, model, on_partial), "agent"
                return cli_turn(prompt, model), "cli"
            except ShimError as e:
                last_err = e
                sys.stderr.write("[shim] tier %s attempt %d failed: %s\n"
                                 % (tier, attempt + 1, e))
                if attempt + 1 < tries:
                    _wait_cooldown()
    raise last_err or ShimError("all tiers failed")


# ----------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mlai-shim/4.0"

    def log_message(self, fmt, *a):
        sys.stderr.write("[shim] %s - %s\n" % (self.address_string(), fmt % a))
        sys.stderr.flush()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, code, payload):
        data = json.dumps(payload).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/v1/models", "/models"):
            now = int(time.time())
            return self._json(200, {"object": "list", "data": [
                {"id": k, "object": "model", "created": now,
                 "owned_by": "ml.ai", "description": v} for k, v in MODELS.items()]})
        if path in ("/health", ""):
            return self._json(200, {"ok": True, "daemon": daemon_alive(), "tier": TIER,
                                    "socket": SOCKET, "default_model": DEFAULT_MODEL,
                                    "max_prompt_chars": MAX_PROMPT_CHARS,
                                    "inflight": MAX_QUEUE - _QUEUE._value})
        self._json(404, {"error": {"message": "not found", "type": "not_found"}})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path not in ("/v1/chat/completions", "/chat/completions"):
            return self._json(404, {"error": {"message": "not found", "type": "not_found"}})

        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": {"message": "invalid JSON",
                                              "type": "invalid_request_error"}})

        messages = req.get("messages") or []
        if not messages or not any(text_of(m.get("content")).strip() for m in messages):
            return self._json(400, {"error": {"message": "messages must contain text",
                                              "type": "invalid_request_error"}})
        try:
            model = resolve_model(req.get("model"))
        except ShimError as e:
            return self._json(e.status, {"error": {"message": str(e), "type": "invalid_request_error",
                                                   "code": e.code}})

        prompt = flatten(messages)
        prefix, trimmed = trim_prompt(build_chat_prefix(messages))
        max_tokens = req.get("max_tokens") or req.get("max_completion_tokens")

        if not _QUEUE.acquire(blocking=False):
            return self._json(429, {"error": {"message": "too many requests in flight",
                                              "type": "rate_limit_error",
                                              "code": "queue_full"}})
        try:
            cid = "chatcmpl-" + uuid.uuid4().hex[:24]
            created = int(time.time())
            started = time.time()
            stream = bool(req.get("stream"))

            def chunk(content=None, role=None, finish=None):
                delta = {}
                if role:
                    delta["role"] = role
                if content is not None:
                    delta["content"] = content
                return {"id": cid, "object": "chat.completion.chunk", "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

            def write(s):
                try:
                    self.wfile.write(s.encode())
                    self.wfile.flush()
                    return True
                except (BrokenPipeError, ConnectionResetError):
                    return False

            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self._cors()
                self.end_headers()
                if not write("data: " + json.dumps(chunk(role="assistant")) + "\n\n"):
                    return

            sent = {"n": 0}

            def emit(text):
                if not stream or not text:
                    return
                sent["n"] += 1
                write("data: " + json.dumps(chunk(content=text)) + "\n\n")

            try:
                text, tier = ask(prompt, model, prefix, emit if stream else None)
            except ShimError as e:
                if stream:
                    err = {"error": {"message": str(e), "type": "upstream_error", "code": e.code}}
                    write("data: " + json.dumps(err) + "\n\ndata: [DONE]\n\n")
                    return
                return self._json(e.status, {"error": {"message": str(e),
                                                       "type": "upstream_error", "code": e.code}})

            text, clipped = clip_text(text, max_tokens)
            elapsed = time.time() - started
            sys.stderr.write("[shim] model=%s tier=%s chars=%d %.2fs stream=%s%s%s\n"
                             % (model, tier, len(prompt), elapsed, stream,
                                " trimmed" if trimmed else "",
                                " clipped" if clipped else ""))
            sys.stderr.flush()

            finish = "length" if clipped else "stop"
            usage = {"prompt_tokens": max(1, len(prompt) // 4),
                     "completion_tokens": max(1, len(text) // 4),
                     "total_tokens": max(2, (len(prompt) + len(text)) // 4)}

            if stream:
                if sent["n"] == 0:
                    write("data: " + json.dumps(chunk(content=text)) + "\n\n")
                done = chunk(finish=finish)
                done["usage"] = usage
                write("data: " + json.dumps(done) + "\n\ndata: [DONE]\n\n")
                return

            self._json(200, {"id": cid, "object": "chat.completion", "created": created,
                             "model": model,
                             "choices": [{"index": 0,
                                          "message": {"role": "assistant", "content": text},
                                          "finish_reason": finish}],
                             "usage": usage})
        finally:
            _QUEUE.release()


if __name__ == "__main__":
    print("mlai-shim 4.0 on http://%s:%d/v1  (tier=%s, socket=%s)"
          % (HOST, PORT, TIER, SOCKET), flush=True)
    print("daemon reachable: %s" % daemon_alive(), flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
