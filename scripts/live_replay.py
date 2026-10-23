#!/usr/bin/env python3
"""Local live viewer: start, publish committed replay snapshots, finish and stop.

Python 3.10+, standard library only. The judge is the sole publisher. This
module reuses the offline renderer and never synthesizes player decisions.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
import uuid

from render_replay import (Renderer, Validator, escape,
                           reject_duplicate_keys, reject_nonfinite)

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                      object_pairs_hook=reject_duplicate_keys, parse_constant=reject_nonfinite)


def atomic_write(path, text):
    """Readers see either the old complete file or the new complete file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".publish-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, allow_nan=False))


@contextmanager
def writer_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".writer.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def check_extension(old, new):
    """Freeze committed history; only the current chapter may grow."""
    for key in ("title", "subtitle", "players", "rules", "ruleset", "voting_mode"):
        if old.get(key) != new.get(key):
            raise ValueError(f"已发布的 {key} 不可变更；修订历史请另建观战目录")
    for key in ("records",):
        if new[key][:len(old[key])] != old[key]:
            raise ValueError(f"{key} 必须追加，不能删除、重排或改写")
    previous = old["chapters"]
    current = new["chapters"]
    if len(current) < len(previous):
        raise ValueError("已发布章节不能删除")
    for index, chapter in enumerate(previous):
        candidate = current[index]
        if {k: v for k, v in chapter.items() if k != "events"} != {k: v for k, v in candidate.items() if k != "events"}:
            raise ValueError("已发布章节标题/摘要不能回填；新结算请追加摘要事件")
        events = chapter["events"]
        if candidate["events"][:len(events)] != events:
            raise ValueError("已发布事件不能删除、重排或改写")
        if index < len(previous) - 1 and candidate["events"] != events:
            raise ValueError("已结束章节不能补插事件")
    if old.get("winner") and old.get("winner") != new.get("winner"):
        raise ValueError("已结算胜方不能变更")
    if "postgame" in old:
        post = new.get("postgame", {})
        prior = old["postgame"]
        if post.get("summary_id") != prior["summary_id"] or post.get("reviewers", [])[:len(prior["reviewers"])] != prior["reviewers"]:
            raise ValueError("已确认的总结回执和感言名单不能改写")


def make_snapshot(data, progress, old=None, final_html=None, final_path=None):
    Validator(data).validate()
    if old:
        check_extension(old["replay"], data)
    renderer = Renderer(data)
    chapters = []
    number = 0
    # Reuse the exact offline chapter markup, splitting out only its header.
    for index, chapter in enumerate(data["chapters"], 1):
        events = []
        for event in renderer.visible_events(chapter):
            number += 1
            events.append({"id": event["id"], "html": renderer.event(event, number)})
        header = (f'<header class="chapter-header"><span class="chapter-index">CHAPTER {index:02d}</span>'
                  f'<h2>{escape(chapter["title"])}</h2></header><div class="chapter-events"></div>')
        chapters.append({"id": chapter["id"], "anchor": f"chapter-{index}", "header_html": header, "events": events})
    finished = final_html is not None
    return {
        "game_id": old["game_id"] if old else str(uuid.uuid4()),
        "revision": old["revision"] + 1 if old else 1,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "status": "finished" if finished else "running", "progress": progress,
        "replay": data, "hero_html": renderer.hero(), "chapters": chapters,
        "appendix_html": renderer.postgame() + renderer.identities() + renderer.rules() if finished else "",
        "final_html": final_html, "final_path": str(final_path) if final_path else None,
    }


def publish(directory, data, progress, *, final_path=None):
    old_path = directory / "snapshot.json"
    old = read_json(old_path) if old_path.exists() else None
    if old and old["status"] == "finished":
        if final_path and data == old["replay"] and str(final_path) == old["final_path"]:
            atomic_write(final_path, old["final_html"])
            return old
        raise ValueError("对局已完成；新对局请使用新目录")
    if old and not final_path and data == old["replay"] and progress == old["progress"]:
        return old  # Identical retry is idempotent.
    Validator(data).validate()
    if old:
        check_extension(old["replay"], data)
    rendered = Renderer(data).render(ASSETS / "replay.html") if final_path else None
    snapshot = make_snapshot(data, progress, old, rendered, final_path)
    if final_path:
        # Never stop a server until the validated offline artifact exists.
        atomic_write(final_path, rendered)
    write_json(old_path, snapshot)
    return snapshot


def request(metadata, route, *, token=False):
    headers = {"Authorization": "Bearer " + metadata["token"]} if token else {}
    req = Request(metadata["url"] + route, data=b"" if token else None, headers=headers)
    with urlopen(req, timeout=1) as response:
        return json.load(response)


def alive(metadata):
    try:
        health = request(metadata, "health")
        return health["instance"] == metadata["instance"]
    except (OSError, ValueError, KeyError, URLError):
        return False


def stop(directory):
    path = directory / "server.json"
    if not path.exists():
        return
    meta = read_json(path)
    if not alive(meta):
        # Do not kill a PID: it may have been reused by an unrelated process.
        meta["running"] = False
        write_json(path, meta)
        return
    request(meta, "shutdown", token=True)
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if not alive(meta):
            return
        time.sleep(0.1)
    raise RuntimeError("停止服务超时；请保留 server.json 并再次执行 stop")


def serve(directory):
    instance = secrets.token_hex(16)
    token = secrets.token_urlsafe(32)
    final_seen = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, content, kind="application/json; charset=utf-8"):
            body = content if isinstance(content, bytes) else content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self, *, navigation=False):
            expected = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != expected:
                return False
            if self.headers.get("Sec-Fetch-Site") != "cross-site":
                return True
            # Browser tools and links may open the shell from another site.
            # Permit only document navigation, never cross-site data or control.
            return navigation and self.headers.get("Sec-Fetch-Mode") == "navigate"

        def do_GET(self):
            if not self.allowed(navigation=self.path.split("?", 1)[0] == "/"):
                self.send(403, '{}')
                return
            route = self.path.split("?", 1)[0]
            if route == "/health":
                self.send(200, json.dumps({"instance": instance, "final_seen": final_seen.is_set()}))
            elif route == "/":
                # Use the offline stylesheet unchanged, with a separate live CSP.
                import re
                template = (ASSETS / "replay.html").read_text(encoding="utf-8")
                css = re.search(r"<style>(.*?)</style>", template, re.S).group(1)
                page = (ASSETS / "live.html").read_text(encoding="utf-8").replace("/* REPLAY_STYLES */", css)
                self.send(200, page, "text/html; charset=utf-8")
            elif route == "/live.js":
                self.send(200, (ASSETS / "live.js").read_bytes(), "text/javascript; charset=utf-8")
            elif route == "/snapshot.json":
                try:
                    self.send(200, (directory / "snapshot.json").read_bytes())
                except OSError:
                    self.send(503, '{"error":"snapshot unavailable"}')
            else:
                self.send(404, '{}')

        def do_POST(self):
            if not self.allowed():
                self.send(403, '{}')
                return
            if self.path.startswith("/ack-final?revision="):
                snapshot = read_json(directory / "snapshot.json")
                if snapshot["status"] == "finished" and self.path == f"/ack-final?revision={snapshot['revision']}":
                    final_seen.set()
                    self.send(200, '{}')
                else:
                    self.send(409, '{}')
            elif self.path == "/shutdown" and secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send(200, '{"stopping":true}')
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send(403, '{}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    metadata = {"url": f"http://127.0.0.1:{server.server_port}/", "pid": os.getpid(),
                "instance": instance, "token": token, "running": True}
    write_json(directory / "server.json", metadata)
    # Signals are handled without calling shutdown from the serve_forever thread.
    def interrupted(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        metadata["running"] = False
        write_json(directory / "server.json", metadata)


def start(directory, data, progress):
    meta_path = directory / "server.json"
    if meta_path.exists() and alive(read_json(meta_path)):
        raise ValueError("此目录已有服务；使用 publish 更新，或先 stop")
    publish(directory, data, progress)
    meta_path.unlink(missing_ok=True)
    with (directory / "server.log").open("a") as log:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_serve", "--directory", str(directory)],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if meta_path.exists():
            meta = read_json(meta_path)
            if alive(meta):
                return meta
        if child.poll() is not None:
            raise RuntimeError(f"服务未启动，见 {directory / 'server.log'}")
        time.sleep(0.1)
    child.terminate()
    child.wait(timeout=3)
    raise RuntimeError("服务启动超时，已终止启动的子进程")


def progress_from(args):
    if args.received is not None or args.expected is not None:
        if args.received is None or args.expected is None or not 0 <= args.received <= args.expected:
            raise ValueError("进度需同时提供 --received 和 --expected，且 0 ≤ received ≤ expected")
    return {"stage": args.stage, "received": args.received, "expected": args.expected, "message": args.message}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "publish", "finish", "stop", "_serve"])
    parser.add_argument("--directory", required=True, type=Path, help="本局独占的实时状态目录")
    parser.add_argument("--input", type=Path, help="已提交的规范化 replay.json")
    parser.add_argument("--output", type=Path, help="finish 必填：最终离线 HTML")
    parser.add_argument("--stage", default="等待法官推进")
    parser.add_argument("--received", type=int)
    parser.add_argument("--expected", type=int)
    parser.add_argument("--message", default="")
    args = parser.parse_args()
    directory = args.directory.resolve()
    try:
        if args.command == "_serve":
            serve(directory)
            return 0
        with writer_lock(directory):
            if args.command == "stop":
                stop(directory)
                print("临时 HTTP 服务已关闭；已保存的快照与离线文件保留。")
                return 0
            if not args.input:
                parser.error("start / publish / finish 需要 --input")
            data = read_json(args.input)
            progress = progress_from(args)
            if args.command == "start":
                meta = start(directory, data, progress)
                print(json.dumps({k: meta[k] for k in ("url", "pid", "running")}, ensure_ascii=False))
            elif args.command == "publish":
                snapshot = publish(directory, data, progress)
                print(f"已发布 revision={snapshot['revision']} game_id={snapshot['game_id']}")
            else:
                if not args.output:
                    parser.error("finish 需要 --output")
                output = args.output.resolve()
                if output == args.input.resolve() or output.is_relative_to(directory):
                    raise ValueError("离线输出不得覆盖输入，也不得放进临时服务状态目录")
                if not data.get("winner"):
                    raise ValueError("最终复盘需要实际结算的 winner；未结束对局请用 stop 保留现场")
                snapshot = publish(directory, data, {"stage": "最终复盘已完成", "received": None, "expected": None, "message": ""}, final_path=output)
                print(f"离线复盘已生成：{output}", flush=True)
                try:
                    meta = read_json(directory / "server.json")
                    deadline = time.monotonic() + 8
                    while alive(meta) and time.monotonic() < deadline:
                        if request(meta, "health").get("final_seen"):
                            break
                        time.sleep(0.1)
                except (OSError, ValueError, URLError):
                    pass
                finally:
                    stop(directory)
                print(f"临时 HTTP 服务已关闭；终局 revision={snapshot['revision']}。请打开上面的离线文件。")
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"实时复盘操作失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
