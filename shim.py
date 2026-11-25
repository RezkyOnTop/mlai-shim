#!/usr/bin/env python3
"""
mlai-shim — OpenAI-compatible bridge in front of the official ML AI Code CLI.

Exposes:
    GET  /v1/models
    POST /v1/chat/completions      (stream + non-stream)

Each request is executed with the documented headless mode of the CLI:

    mlai -p "<prompt>" --mode ask --tools "" --output-format json

so the agent behaves like a plain chat model (no file edits, no shell).
Requires: the CLI installed and signed in (`npm i -g mlai-code && mlai login`).

Run:  python3 shim.py            # listens on 127.0.0.1:8787
"""

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("MLAI_SHIM_HOST", "127.0.0.1")
PORT = int(os.environ.get("MLAI_SHIM_PORT", "8787"))
TIMEOUT = int(os.environ.get("MLAI_SHIM_TIMEOUT", "600"))
MLAI_BIN = os.environ.get("MLAI_BIN") or shutil.which("mlai") or "mlai"
EXTRA_ARGS = os.environ.get("MLAI_SHIM_ARGS", "").split()
# Run the CLI in an empty scratch folder so the agent never touches a real project.
WORKDIR = os.environ.get("MLAI_SHIM_WORKDIR", os.path.join(os.path.expanduser("~"), "mlai-shim", "scratch"))
os.makedirs(WORKDIR, exist_ok=True)

# Advertised model IDs. The CLI accepts -m <id>; anything else is passed through.
MODELS = {
    "pixis-opus": "pixis-opus",   # default, strongest
    "pixis": "pixis",             # fast
    "mlai-auto": None,            # let the CLI pick
}
DEFAULT_MODEL = os.environ.get("MLAI_SHIM_MODEL", "pixis-opus")


def flatten(messages):
    """Turn an OpenAI message array into one prompt for the CLI."""
    system_parts, convo = [], []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, list):  # multimodal / parts
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        content = (content or "").strip()
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
        elif role == "assistant":
            convo.append("Assistant: " + content)
        else:
            convo.append("User: " + content)

    body = "\n\n".join(convo)
    guard = ("You are answering a plain chat request through an API. "
             "Do not read, list or inspect any files, and do not talk about the folder. "
             "Just answer the request directly and reply with the answer text only.")
    parts = system_parts + [guard]
    return ("\n\n".join(parts) + "\n\n" + body).strip()


def run_cli(prompt, model_id):
    args = [MLAI_BIN, "-p", prompt, "--mode", "ask",
            "--output-format", "json", "--no-session-persistence"]
    cli_model = MODELS.get(model_id, model_id)
    if cli_model:
        args += ["-m", cli_model]
    args += EXTRA_ARGS
    started = time.time()
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT, cwd=WORKDIR)
    except FileNotFoundError:
        return None, "mlai CLI not found. Install it: npm install -g mlai-code", 0
    except subprocess.TimeoutExpired:
        return None, "mlai CLI timed out", 0

    raw = (p.stdout or "").strip()
    # The CLI may print a progress notice on stderr before the JSON result.
    obj = None
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            break
        except json.JSONDecodeError:
            continue

    elapsed = time.time() - started
    if obj is None:
        err = (p.stderr or raw or "empty output from mlai").strip()[:800]
        return None, err, elapsed

    if obj.get("error") or obj.get("state") == "error":
        msg = str(obj.get("error") or "mlai returned an error")
        if "Not signed in" in msg:
            msg = "ML AI Code is not signed in on this machine. Run: mlai login"
        return None, msg, elapsed
    if obj.get("exitCode") not in (0, None):
        return None, "mlai exited %s (state=%s)" % (obj.get("exitCode"), obj.get("state")), elapsed

    text = obj.get("text")
    if text is None:
        text = obj.get("result") or obj.get("output") or ""
    if not isinstance(text, str):
        text = json.dumps(text)
    return text, None, elapsed


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mlai-shim/1.0"

    def log_message(self, fmt, *a):
        sys.stderr.write("[shim] %s - %s\n" % (self.address_string(), fmt % a))

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
            return self._json(200, {
                "object": "list",
                "data": [
                    {"id": m, "object": "model", "created": now, "owned_by": "ml.ai"}
                    for m in MODELS
                ],
            })
        if path in ("/health", "/"):
            return self._json(200, {"ok": True, "cli": MLAI_BIN})
        self._json(404, {"error": {"message": "not found", "type": "not_found"}})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path not in ("/v1/chat/completions", "/chat/completions"):
            return self._json(404, {"error": {"message": "not found", "type": "not_found"}})

        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": {"message": "invalid JSON", "type": "invalid_request_error"}})

        model_id = req.get("model") or DEFAULT_MODEL
        prompt = flatten(req.get("messages") or [])
        if not prompt:
            return self._json(400, {"error": {"message": "no messages", "type": "invalid_request_error"}})

        text, err, elapsed = run_cli(prompt, model_id)
        sys.stderr.write("[shim] model=%s chars=%d %.1fs %s\n"
                         % (model_id, len(prompt), elapsed, "ERROR" if err else "ok"))
        sys.stderr.flush()
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())

        if err:
            return self._json(502, {"error": {"message": err, "type": "upstream_error", "code": "mlai_error"}})

        usage = {
            "prompt_tokens": max(1, len(prompt) // 4),
            "completion_tokens": max(1, len(text) // 4),
            "total_tokens": max(2, (len(prompt) + len(text)) // 4),
        }

        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created,
                     "model": model_id,
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                  "finish_reason": None}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            done = {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": usage}
            self.wfile.write(("data: " + json.dumps(done) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        self._json(200, {
            "id": cid, "object": "chat.completion", "created": created, "model": model_id,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": usage,
        })


if __name__ == "__main__":
    print("mlai-shim listening on http://%s:%d/v1  (cli: %s)" % (HOST, PORT, MLAI_BIN), flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
