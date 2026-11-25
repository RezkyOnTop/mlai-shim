#!/usr/bin/env python3
"""
mlai-shim — OpenAI-compatible bridge for ML AI Code, using the persistent daemon.

Fast path: talks to `mlai serve` over its local socket (/v1/sessions +
/v1/sessions/{id}/messages + SSE /events). No CLI process spawn per request.

Fallback: spawns `mlai -p ... --output-format stream-json` when the daemon is
not reachable.

    GET  /v1/models
    POST /v1/chat/completions     (stream + non-stream)
    GET  /health

Run:  python3 shim.py            # http://127.0.0.1:8787/v1
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
DEFAULT_MODEL = os.environ.get("MLAI_SHIM_MODEL", "pixis-opus")
EFFORT = os.environ.get("MLAI_SHIM_EFFORT", "low")

# Real model IDs exposed by the ml.ai daemon (verified via /v1/models on the socket).
MODELS = {
    "pixis-opus": "Opus 5.5R",
    "pixis": "Pixis",
    "pixis-sol": "GPT 6.1R Sol",
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


# ---------------------------------------------------------------- daemon client
class DaemonError(Exception):
    pass


def _sock_http(method, path, body=None, timeout=30, raw=False):
    """Minimal HTTP/1.1 over the daemon's unix socket."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCKET)
    except OSError as e:
        s.close()
        raise DaemonError("socket %s: %s" % (SOCKET, e))
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
    if raw:
        return head, payload
    # strip chunked framing
    if payload and "\r\n" in payload:
        payload = "".join(l for l in payload.split("\r\n")
                          if not re.fullmatch(r"[0-9a-fA-F]+", l.strip() or "x"))
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        raise DaemonError("bad JSON from daemon: %s" % payload[:200])


_daemon_state = {"ok": False, "checked": 0.0}
_state_lock = threading.Lock()


def daemon_alive(ttl=30):
    """Cached daemon probe — avoids an extra round trip on every request."""
    now = time.time()
    with _state_lock:
        if now - _daemon_state["checked"] < ttl:
            return _daemon_state["ok"]
    try:
        _sock_http("GET", "/v1/models", timeout=3)
        ok = True
    except Exception:
        ok = False
    with _state_lock:
        _daemon_state["ok"] = ok
        _daemon_state["checked"] = time.time()
    return ok


def _new_session():
    d = _sock_http("POST", "/v1/sessions", {"cwd": WORKDIR}, timeout=20)
    return d["id"]


def daemon_ask(prompt, model, on_partial=None):
    """Send one turn to the daemon and return the final answer text."""
    sid = _new_session()
    body = {"task": prompt, "model": model, "mode": "ask"}
    if EFFORT:
        body["effort"] = EFFORT

    started = time.time()
    r = _sock_http("POST", "/v1/sessions/%s/messages" % sid, body, timeout=30)
    if not r.get("ok"):
        raise DaemonError(str(r.get("error") or r))

    seen = 0
    last_poll = 0.0
    while time.time() - started < TIMEOUT:
        # Adaptive polling: fast at first (first token matters), then back off.
        elapsed = time.time() - started
        gap = 0.08 if elapsed < 4 else (0.25 if elapsed < 12 else 0.6)
        if time.time() - last_poll < gap:
            time.sleep(0.02)
            continue
        last_poll = time.time()
        tr = _sock_http("GET", "/v1/sessions/%s/transcript" % sid, timeout=30)
        if not tr:
            time.sleep(0.2)
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
            answer = ""
            for ev in events:
                if ev.get("type") == "answer":
                    answer = ev.get("content") or ""
            if state in ("error", "failed"):
                raise DaemonError(str((last.get("status") or {}).get("error") or "run failed"))
            if not answer:
                for ev in events:
                    if ev.get("type") == "assistant":
                        answer = ev.get("content") or ""
            return answer
    raise DaemonError("timeout after %ds" % TIMEOUT)


# ------------------------------------------------------------------ CLI fallback
def cli_ask(prompt, model):
    args = [MLAI_BIN, "-p", prompt, "--mode", "ask", "--output-format", "stream-json",
            "--no-session-persistence", "-m", model]
    if EFFORT:
        args += ["--effort", EFFORT]
    args += EXTRA_ARGS
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT, cwd=WORKDIR)
    except FileNotFoundError:
        raise DaemonError("mlai CLI not found: npm install -g mlai-code")
    except subprocess.TimeoutExpired:
        raise DaemonError("mlai CLI timed out after %ds" % TIMEOUT)

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
        raise DaemonError(str(err))
    if answer is None:
        raise DaemonError((p.stderr or "no answer from mlai").strip().splitlines()[-1][:400])
    return answer


# ---------------------------------------------------------------------- helpers
def flatten(messages):
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
    parts = system_parts + [GUARD]
    return ("\n\n".join(parts) + "\n\n" + "\n\n".join(convo)).strip()


def resolve_model(name):
    if not name:
        return DEFAULT_MODEL
    if name.startswith("mlai/"):
        name = name[len("mlai/"):]
    return name if name in MODELS else DEFAULT_MODEL


def ask(prompt, model, on_partial=None):
    if daemon_alive():
        try:
            return daemon_ask(prompt, model, on_partial), "daemon"
        except DaemonError as e:
            sys.stderr.write("[shim] daemon failed (%s), falling back to CLI\n" % e)
    return cli_ask(prompt, model), "cli"


# ------------------------------------------------------------------------ server
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mlai-shim/2.0"

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
                                    "socket": SOCKET, "cli": MLAI_BIN,
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

        model = resolve_model(req.get("model"))
        prompt = flatten(req.get("messages") or [])
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

        sent = {"n": 0}

        def emit(text):
            if not stream or not text:
                return
            if sent["n"] == 0:
                self.wfile.write(("data: " + json.dumps(chunk(role="assistant")) + "\n\n").encode())
            sent["n"] += 1
            self.wfile.write(("data: " + json.dumps(chunk(content=text)) + "\n\n").encode())
            self.wfile.flush()

        try:
            text, via = ask(prompt, model, emit if stream else None)
        except DaemonError as e:
            if stream:
                err = {"error": {"message": str(e), "type": "upstream_error", "code": "mlai_error"}}
                self.wfile.write(("data: " + json.dumps(err) + "\n\ndata: [DONE]\n\n").encode())
                self.wfile.flush()
                return
            return self._json(502, {"error": {"message": str(e), "type": "upstream_error",
                                              "code": "mlai_error"}})

        elapsed = time.time() - started
        sys.stderr.write("[shim] model=%s via=%s chars=%d %.1fs stream=%s\n"
                         % (model, via, len(prompt), elapsed, stream))
        sys.stderr.flush()

        usage = {"prompt_tokens": max(1, len(prompt) // 4),
                 "completion_tokens": max(1, len(text) // 4),
                 "total_tokens": max(2, (len(prompt) + len(text)) // 4)}

        if stream:
            if sent["n"] == 0:      # nothing streamed (e.g. CLI fallback) -> send once
                self.wfile.write(("data: " + json.dumps(chunk(role="assistant")) + "\n\n").encode())
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
    print("mlai-shim on http://%s:%d/v1  (daemon socket: %s, cli: %s)"
          % (HOST, PORT, SOCKET, MLAI_BIN), flush=True)
    print("daemon reachable: %s" % daemon_alive(), flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
