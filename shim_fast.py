#!/usr/bin/env python3
"""
mlai-shim 3.0 — OpenAI-compatible bridge for ML AI Code (fast path).

Three tiers, fastest first:

  1. POST /v1/complete on the `mlai serve` daemon
     Raw completion against the model — no agent loop, no intent classifier.
     Measured 0.45-1.1s per reply.

  2. POST /v1/sessions + /messages + poll /transcript
     Full agent turn (intent -> skills -> model -> answer). Measured 6-8s.
     Used when the prompt needs the agent (long context, code work).

  3. `mlai -p ... --output-format stream-json`
     Spawns the CLI. Measured 13-14s. Last resort when no daemon is running.

    GET  /v1/models
    POST /v1/chat/completions      (stream + non-stream)
    GET  /health

Run:  python3 shim.py
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
SOCKET = os.environ.get("MLAI_SOCKET", "/tmp/mlai-shim.sock")
EXTRA_ARGS = os.environ.get("MLAI_SHIM_ARGS", "").split()
WORKDIR = os.environ.get("MLAI_SHIM_WORKDIR",
                         os.path.join(os.path.expanduser("~"), "mlai-shim", "scratch"))
os.makedirs(WORKDIR, exist_ok=True)
DEFAULT_MODEL = os.environ.get("MLAI_SHIM_MODEL", "pixis")
EFFORT = os.environ.get("MLAI_SHIM_EFFORT", "low")
# Tier 1 (raw completion) unless MLAI_SHIM_TIER=agent|cli
TIER = os.environ.get("MLAI_SHIM_TIER", "fast")

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


class ShimError(Exception):
    pass


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
    head, _, payload = text.partition("\r\n\r\n")
    if payload and "\r\n" in payload:
        payload = "".join(l for l in payload.split("\r\n")
                          if not re.fullmatch(r"[0-9a-fA-F]+", l.strip() or "x"))
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        raise ShimError("bad JSON from daemon: %s" % payload[:200])


_state = {"ok": False, "checked": 0.0}
_state_lock = threading.Lock()


def daemon_alive(ttl=30):
    now = time.time()
    with _state_lock:
        if now - _state["checked"] < ttl:
            return _state["ok"]
    try:
        _sock_http("GET", "/v1/models", timeout=3)
        ok = True
    except Exception:
        ok = False
    with _state_lock:
        _state["ok"] = ok
        _state["checked"] = time.time()
    return ok


# ------------------------------------------------------- tier 1: raw completion
def fast_complete(prompt, model, attempts=4):
    """Raw model completion against the daemon: no agent loop, no intent pass.

    Two things learned from the daemon:
      * the prompt must end with an `Assistant:` marker, else it answers nothing;
      * roughly 1 in 6 prompts comes back with no items and an identical retry
        always fails too — nudging the tail (`\\n`, ` Jawab:`) unsticks it.
    """
    tails = ["", "\n", " Jawab:", "\n\n"]
    for i in range(attempts):
        body = {"prefix": prompt + tails[i % len(tails)], "model": model,
                "stop": ["\nUser:", "\nSystem:"]}
        r = _sock_http("POST", "/v1/complete", body, timeout=TIMEOUT)
        items = r.get("items") or []
        text = "".join(i2.get("text", "") for i2 in items if isinstance(i2, dict))
        if text.strip():
            return text.strip()
        if i < attempts - 1:
            time.sleep(0.05)
    raise ShimError("empty completion from daemon after %d attempts" % attempts)


# ------------------------------------------------------- tier 2: agent session
def agent_turn(prompt, model, on_partial=None):
    """Full agent turn; slower but handles long prompts and repo context."""
    sid = _sock_http("POST", "/v1/sessions", {"cwd": WORKDIR}, timeout=20)["id"]
    body = {"task": prompt, "model": model, "mode": "ask"}
    if EFFORT:
        body["effort"] = EFFORT
    started = time.time()
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
def flatten(messages, system_only=False):
    system_parts, convo = [], []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        content = (content or "").strip()
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
        elif role == "assistant":
            convo.append("Assistant: " + content)
        else:
            convo.append("User: " + content)
    if system_only:
        return "\n\n".join(system_parts)
    return "\n\n".join(system_parts + [GUARD] + convo).strip()


def build_chat_prefix(messages):
    """ChatML-ish transcript that /v1/complete continues.

    The trailing `Assistant:` marker is required — without it the daemon
    returns no items at all.
    """
    lines = []
    has_system = any(m.get("role") == "system" for m in messages)
    if not has_system:
        lines.append("System: " + GUARD)
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        content = (content or "").strip()
        if not content:
            continue
        label = {"system": "System", "assistant": "Assistant"}.get(role, "User")
        lines.append("%s: %s" % (label, content))
    lines.append("Assistant:")
    return "\n\n".join(lines)


def resolve_model(name):
    if not name:
        return DEFAULT_MODEL
    if name.startswith("mlai/"):
        name = name[len("mlai/"):]
    return name if name in MODELS else DEFAULT_MODEL


def ask(prompt, model, chat_prefix=None, on_partial=None):
    """Try tiers in order; returns (text, tier_used).

    TIER=fast  -> raw completion only (no daily task-start limit, ~1s).
    TIER=auto  -> fast, then agent, then CLI.
    """
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
    last_err = None
    for tier in order:
        try:
            if tier == "fast":
                return fast_complete(chat_prefix or prompt, model), "fast"
            if tier == "agent":
                return agent_turn(prompt, model, on_partial), "agent"
            return cli_turn(prompt, model), "cli"
        except ShimError as e:
            last_err = e
            sys.stderr.write("[shim] tier %s failed: %s\n" % (tier, e))
    raise last_err or ShimError("all tiers failed")


# ----------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mlai-shim/3.0"

    def log_message(self, fmt, *a):
        sys.stderr.write("[shim] %s - %s\n" % (self.address_string(), fmt % a))
        sys.stderr.flush()

    def _json(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/v1/models", "/models"):
            now = int(time.time())
            return self._json(200, {"object": "list", "data": [
                {"id": k, "object": "model", "created": now,
                 "owned_by": "ml.ai", "description": v} for k, v in MODELS.items()]})
        if path in ("/health", ""):
            return self._json(200, {"ok": True, "daemon": daemon_alive(),
                                    "tier": TIER, "socket": SOCKET,
                                    "default_model": DEFAULT_MODEL})
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
        model = resolve_model(req.get("model"))
        prompt = flatten(messages)
        chat_prefix = build_chat_prefix(messages)
        if not prompt:
            return self._json(400, {"error": {"message": "no messages",
                                              "type": "invalid_request_error"}})

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

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(("data: " + json.dumps(chunk(role="assistant")) + "\n\n").encode())
            self.wfile.flush()

        sent = {"n": 0}

        def emit(text):
            if not stream or not text:
                return
            sent["n"] += 1
            self.wfile.write(("data: " + json.dumps(chunk(content=text)) + "\n\n").encode())
            self.wfile.flush()

        try:
            text, tier = ask(prompt, model, chat_prefix, emit if stream else None)
        except ShimError as e:
            if stream:
                err = {"error": {"message": str(e), "type": "upstream_error", "code": "mlai_error"}}
                self.wfile.write(("data: " + json.dumps(err) + "\n\ndata: [DONE]\n\n").encode())
                self.wfile.flush()
                return
            return self._json(502, {"error": {"message": str(e), "type": "upstream_error",
                                              "code": "mlai_error"}})

        elapsed = time.time() - started
        sys.stderr.write("[shim] model=%s tier=%s chars=%d %.2fs stream=%s\n"
                         % (model, tier, len(prompt), elapsed, stream))
        sys.stderr.flush()

        usage = {"prompt_tokens": max(1, len(prompt) // 4),
                 "completion_tokens": max(1, len(text) // 4),
                 "total_tokens": max(2, (len(prompt) + len(text)) // 4)}

        if stream:
            if sent["n"] == 0:
                self.wfile.write(("data: " + json.dumps(chunk(content=text)) + "\n\n").encode())
            done = chunk(finish="stop")
            done["usage"] = usage
            self.wfile.write(("data: " + json.dumps(done) + "\n\ndata: [DONE]\n\n").encode())
            self.wfile.flush()
            return

        self._json(200, {"id": cid, "object": "chat.completion", "created": created,
                         "model": model,
                         "choices": [{"index": 0,
                                      "message": {"role": "assistant", "content": text},
                                      "finish_reason": "stop"}],
                         "usage": usage})


if __name__ == "__main__":
    print("mlai-shim 3.0 on http://%s:%d/v1  (tier=%s, socket=%s)"
          % (HOST, PORT, TIER, SOCKET), flush=True)
    print("daemon reachable: %s" % daemon_alive(), flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
