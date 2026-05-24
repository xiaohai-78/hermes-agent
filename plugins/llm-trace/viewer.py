"""llm-trace viewer — 本地 trace 浏览器。

启动：
    python plugins/llm-trace/viewer.py [--dir <abs>] [--port 8765] [--no-open]

默认 trace 目录：``$HERMES_LLM_TRACE_DIR`` 或 ``$HERMES_HOME/llm-traces``。
启动后浏览器自动打开 http://127.0.0.1:8765 。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse


HERE = Path(__file__).parent
INDEX_HTML = HERE / "index.html"


# ---- 路径解析 -----------------------------------------------------------

def resolve_trace_dir(arg: Optional[str]) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("HERMES_LLM_TRACE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    try:
        from hermes_constants import get_hermes_home
        return (get_hermes_home() / "llm-traces").resolve()
    except Exception:
        return (Path.home() / ".hermes" / "llm-traces").resolve()


class State:
    trace_dir: Path = Path()


# ---- 数据读取 -----------------------------------------------------------

def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_ndjson(p: Path) -> list:
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass
    return out


def _summarize_call(call_dir: Path) -> dict:
    req = _read_json(call_dir / "request.json") or {}
    resp = _read_json(call_dir / "response.json") or {}
    return {
        "id": call_dir.name,
        "call_kind": req.get("call_kind"),
        "model": req.get("model"),
        "base_url": req.get("base_url"),
        "streaming": req.get("streaming"),
        "started_at": req.get("started_at"),
        "elapsed_ms": resp.get("elapsed_ms"),
        "chunk_count": resp.get("chunk_count"),
        "has_error": bool(resp.get("error")),
    }


def list_sessions() -> list[dict]:
    base = State.trace_dir / "sessions"
    if not base.is_dir():
        return []
    out = []
    for d in base.iterdir():
        if not d.is_dir():
            continue
        out.append(_summarize_session(d))
    # 最近的 session 排最上面；started_at 缺失时退化成按 id 倒序
    # （session id 自带时间戳前缀 ``YYYYMMDD_HHMMSS_xxx``，字典序即时间序）。
    out.sort(key=lambda x: (x.get("started_at") or 0, x.get("id") or ""), reverse=True)
    return out


def _summarize_session(session_dir: Path) -> dict:
    turns = sorted(
        [d for d in session_dir.iterdir() if d.is_dir() and d.name.startswith("turn-")]
    )
    aux_dir = session_dir / "aux"
    aux_count = 0
    if aux_dir.is_dir():
        aux_count = sum(
            1 for d in aux_dir.iterdir() if d.is_dir() and d.name.startswith("call-")
        )
    # ``turns`` 已按字典序升序：turn-0001 是最早的，最后一个是最近的。
    # session 列表卡片显示"最近 user message"作为预览（更直观）。
    started_at = None
    latest_user_message = ""
    for t in turns:
        meta = _read_json(t / "_turn.json")
        if not meta:
            continue
        if started_at is None:
            started_at = meta.get("started_at")  # 最早 turn 的 started_at == session 开始时间
        if meta.get("user_message_preview"):
            latest_user_message = meta["user_message_preview"]  # 升序遍历，最终值即最新
    main_call_count = 0
    for t in turns:
        for c in t.iterdir():
            if c.is_dir() and c.name.startswith("call-"):
                main_call_count += 1
    return {
        "id": session_dir.name,
        "turn_count": len(turns),
        "main_call_count": main_call_count,
        "aux_count": aux_count,
        "started_at": started_at,
        "preview": latest_user_message,
    }


def session_detail(sid: str) -> Optional[dict]:
    sdir = State.trace_dir / "sessions" / sid
    if not sdir.is_dir():
        return None
    # 排序统一规则：最近的在最上面。turn 目录名形如 ``turn-0001-YYYYMMDD-HHMMSS``、
    # call 目录名形如 ``call-<unix_ms>-<hex>-<kind>``，字典序倒序即时间倒序。
    turns = []
    turn_dirs = sorted(
        [d for d in sdir.iterdir() if d.is_dir() and d.name.startswith("turn-")],
        reverse=True,
    )
    for t in turn_dirs:
        call_dirs = sorted(
            [c for c in t.iterdir() if c.is_dir() and c.name.startswith("call-")],
            reverse=True,
        )
        turns.append({
            "id": t.name,
            "meta": _read_json(t / "_turn.json"),
            "calls": [_summarize_call(c) for c in call_dirs],
        })
    aux_calls = []
    auxd = sdir / "aux"
    if auxd.is_dir():
        aux_dirs = sorted(
            [c for c in auxd.iterdir() if c.is_dir() and c.name.startswith("call-")],
            reverse=True,
        )
        for c in aux_dirs:
            aux_calls.append(_summarize_call(c))
    return {"id": sid, "turns": turns, "aux_calls": aux_calls}


def call_detail(sid: str, parent: str, call_id: str) -> Optional[dict]:
    base = State.trace_dir / "sessions" / sid / parent / call_id
    if not base.is_dir():
        return None
    return {
        "request": _read_json(base / "request.json"),
        "response": _read_json(base / "response.json"),
        "chunks": _read_ndjson(base / "chunks.ndjson"),
    }


# ---- HTTP -----------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静音

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, p: Path, ctype: str) -> None:
        try:
            body = p.read_bytes()
        except Exception:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = unquote(urlparse(self.path).path)

        if path in ("/", "/index.html"):
            return self._static(INDEX_HTML, "text/html; charset=utf-8")
        if path == "/api/sessions":
            return self._json(200, {
                "sessions": list_sessions(),
                "trace_dir": str(State.trace_dir),
            })
        if path.startswith("/api/session/"):
            sid = path[len("/api/session/"):]
            d = session_detail(sid)
            if d is None:
                return self._json(404, {"error": "session not found"})
            return self._json(200, d)
        if path.startswith("/api/call/"):
            parts = path[len("/api/call/"):].split("/")
            if len(parts) != 3:
                return self._json(400, {"error": "expects /api/call/<sid>/<parent>/<call_id>"})
            d = call_detail(*parts)
            if d is None:
                return self._json(404, {"error": "call not found"})
            return self._json(200, d)

        self.send_response(404)
        self.end_headers()


# ---- CLI ----------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="llm-trace-viewer",
        description="本地浏览 hermes llm-trace 数据。",
    )
    parser.add_argument("--dir", default=None,
                        help="trace 目录，默认读 HERMES_LLM_TRACE_DIR 或 ~/.hermes/llm-traces")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-open", action="store_true",
                        help="不自动打开浏览器")
    args = parser.parse_args()

    State.trace_dir = resolve_trace_dir(args.dir)
    if not State.trace_dir.exists():
        print(f"[!] trace 目录不存在: {State.trace_dir}", file=sys.stderr)
        print("    确认插件已经启用并产生过数据，或用 --dir 指定路径。", file=sys.stderr)
        sys.exit(1)
    if not INDEX_HTML.exists():
        print(f"[!] viewer index.html 缺失: {INDEX_HTML}", file=sys.stderr)
        sys.exit(1)

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        print(f"[!] 端口 {args.port} 占用或无法绑定 {args.host}: {e}", file=sys.stderr)
        print("    试试 --port 8766 或者 lsof -i:8765 看占用。", file=sys.stderr)
        sys.exit(1)

    url = f"http://{args.host}:{args.port}/"
    print(
        f"\n  llm-trace viewer"
        f"\n  trace dir : {State.trace_dir}"
        f"\n  url       : {url}"
        f"\n  Ctrl-C 退出\n",
        flush=True,
    )

    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  bye.")
        server.server_close()


if __name__ == "__main__":
    main()
