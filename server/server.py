#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""askme server — 两人私享问答服务(单文件,纯标准库 + SQLite,零第三方依赖)。

A lightweight, git-friendly re-creation of the classic "ask an expert + knowledge base" pattern, upgraded with multi-turn threads:
  - thread(问题)下双方可多轮追加消息;回答方最后发言 = RESOLVED,提问方最后发言 = OPEN
  - 回答方首条回复自动沉淀知识库(source=ASK);KB 强命中由分身代答(AUTO_ANSWERED)
  - feedback not / escalate / 代答后追问 → 关联 KB 条目转 NEEDS_REVIEW
  - 附件 = 消息正文内嵌 capability-URL markdown 引用;GET 免鉴权(uuid 不可枚举)

API contract: envelope {code,message,data}, Authorization: Bearer,
HTTP 401 触发客户端自动重登。

用法:
    python3 server.py                        # 启动(默认 127.0.0.1:8730,对外走反代)
    python3 server.py adduser <名字>          # 创建账号(交互输密码,或 --password)
    python3 server.py serve --port 9000 --data-dir ./data
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import traceback
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

VERSION = "1.0.0"

DEFAULT_HOST = "127.0.0.1"   # 只绑本机,对外由 caddy/nginx 反代终结 TLS
DEFAULT_PORT = 8730
TOKEN_TTL = 7 * 24 * 3600
MAX_BODY = 8 * 1024 * 1024          # 请求体上限(附件图 5MB + multipart 开销)
IMG_MAX = 5 * 1024 * 1024
TEXT_MAX = 2 * 1024 * 1024

IMG_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif"}
TEXT_EXTS = {".txt", ".log", ".md", ".json", ".yaml", ".yml", ".xml", ".csv", ".html",
             ".htm", ".js", ".ts", ".py", ".java", ".sql", ".sh", ".ini", ".conf",
             ".properties", ".stack", ".trace", ".out"}

DATA_DIR = os.environ.get("ASKME_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "askme.db")
FILES_DIR = os.path.join(DATA_DIR, "files")
SECRET_PATH = os.path.join(DATA_DIR, "secret.key")

ATTACH_RE = re.compile(r"!?\[[^\]\n]*\]\(/api/attachment/[0-9a-fA-F-]{8,}\)")
STARTED_AT = time.time()


# ── 工具 ──────────────────────────────────────────────────────

class ApiErr(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def now_s():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def strip_refs(text):
    """删除附件引用行(uuid 每次不同,不剥会污染检索;stripped before KB indexing/retrieval)。"""
    kept = [ln for ln in (text or "").splitlines() if not ATTACH_RE.search(ln)]
    return "\n".join(kept).strip()


def make_title(text, n=60):
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln:
            return ln[:n]
    return "(空)"


def is_prefix_match(a, b):
    """互为前缀即强命中(大小写不敏感;同 AskService.isStrongMatch)。"""
    a = (a or "").strip().casefold()
    b = (b or "").strip().casefold()
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


def esc_like(s):
    return (s or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ── 存储 ──────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS t_user (
    username      TEXT PRIMARY KEY,
    display_name  TEXT,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS t_thread (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asker           TEXT NOT NULL,
    addressee       TEXT NOT NULL,
    title           TEXT,
    status          TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN/RESOLVED/AUTO_ANSWERED
    kb_entry_id     INTEGER,
    escalated       INTEGER NOT NULL DEFAULT 0,
    feedback        TEXT,                            -- HELPFUL/NOT
    feedback_comment TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_thread_addressee ON t_thread(addressee, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_thread_asker     ON t_thread(asker, updated_at DESC);
CREATE TABLE IF NOT EXISTS t_message (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id  INTEGER NOT NULL REFERENCES t_thread(id),
    sender     TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'USER',        -- USER/BOT(分身代答)
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_message_thread ON t_message(thread_id, id);
CREATE TABLE IF NOT EXISTS t_kb_entry (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    owner            TEXT NOT NULL,
    question         TEXT NOT NULL,
    question_alts    TEXT,
    answer           TEXT,
    tags             TEXT,
    source           TEXT NOT NULL DEFAULT 'MANUAL', -- MANUAL/ASK
    source_thread_id INTEGER,
    status           TEXT NOT NULL DEFAULT 'ACTIVE', -- ACTIVE/NEEDS_REVIEW/ARCHIVED
    hit_count        INTEGER NOT NULL DEFAULT 0,
    deleted          INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kb_owner ON t_kb_entry(owner, status);
CREATE TABLE IF NOT EXISTS t_attachment (
    att_key       TEXT PRIMARY KEY,
    owner         TEXT NOT NULL,
    biz_type      TEXT,
    file_name     TEXT,
    content_type  TEXT,
    size_bytes    INTEGER,
    created_at    TEXT NOT NULL
);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_store():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(FILES_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    if not os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, "w", encoding="utf-8") as f:
            f.write(secrets.token_hex(32))
        try:
            os.chmod(SECRET_PATH, 0o600)
        except OSError:
            pass


def load_secret():
    with open(SECRET_PATH, "r", encoding="utf-8") as f:
        return f.read().strip()


# ── 账号与 token ──────────────────────────────────────────────

def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), 200_000).hex()
    return salt + "$" + digest


def verify_password(password, stored):
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def make_token(secret, username, ttl=TOKEN_TTL):
    payload = "%s|%d" % (username, int(time.time()) + ttl)
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return payload + "|" + sig


def parse_token(secret, token):
    """返回 username;无效/过期返回 None。"""
    try:
        username, exp, sig = token.split("|", 2)
        if int(exp) < time.time():
            return None
        want = hmac.new(secret.encode("utf-8"),
                        ("%s|%s" % (username, exp)).encode("utf-8"),
                        hashlib.sha256).hexdigest()
        return username if hmac.compare_digest(sig, want) else None
    except Exception:
        return None


def get_user(conn, username):
    return conn.execute("SELECT * FROM t_user WHERE username=?", (username,)).fetchone()


# ── 行 → JSON(camelCase,camelCase convention)────────────────

def thread_dict(t, messages=None, candidates=None):
    d = {"id": t["id"], "asker": t["asker"], "addressee": t["addressee"],
         "title": t["title"], "status": t["status"],
         "kbEntryId": t["kb_entry_id"], "escalated": bool(t["escalated"]),
         "feedback": t["feedback"], "feedbackComment": t["feedback_comment"],
         "createdAt": t["created_at"], "updatedAt": t["updated_at"]}
    if messages is not None:
        d["messages"] = [{
            "id": m["id"], "threadId": m["thread_id"], "sender": m["sender"],
            "role": m["role"], "content": m["content"], "createdAt": m["created_at"],
        } for m in messages]
    if candidates is not None:
        d["candidates"] = candidates
    return d


def kb_dict(e):
    return {"id": e["id"], "owner": e["owner"], "question": e["question"],
            "questionAlts": e["question_alts"], "answer": e["answer"],
            "tags": e["tags"], "source": e["source"],
            "sourceThreadId": e["source_thread_id"], "status": e["status"],
            "hitCount": e["hit_count"], "createdAt": e["created_at"],
            "updatedAt": e["updated_at"]}


# ── 业务 ──────────────────────────────────────────────────────

def strong_hit(conn, owner, question):
    """强命中(代答用):仅 ACTIVE 条目;返回命中行或 None。"""
    return _strong_hit(conn, owner, question, active_only=True)


def strong_hit_any(conn, owner, question):
    """强命中(查重用):含 NEEDS_REVIEW/ARCHIVED(追问把条目转待复核后,真人补答仍应合并原条)。"""
    return _strong_hit(conn, owner, question, active_only=False)


def _strong_hit(conn, owner, question, active_only):
    where = "owner=? AND deleted=0" + (" AND status='ACTIVE'" if active_only else "")
    for e in conn.execute(
            "SELECT * FROM t_kb_entry WHERE " + where, (owner,)).fetchall():
        if is_prefix_match(question, e["question"]):
            return e
        for alt in (e["question_alts"] or "").splitlines():
            if alt.strip() and is_prefix_match(question, alt):
                return e
    return None


def weak_hits(conn, owner, question, limit=5):
    """弱命中:ILIKE question/alts/tags,前缀命中优先,hit_count 次之。"""
    pat = "%" + esc_like(question) + "%"
    pre = esc_like(question) + "%"
    rows = conn.execute(
        """SELECT * FROM t_kb_entry
           WHERE owner=? AND status='ACTIVE' AND deleted=0
             AND (question LIKE ? ESCAPE '\\'
               OR question_alts LIKE ? ESCAPE '\\'
               OR tags LIKE ? ESCAPE '\\')
           ORDER BY (CASE WHEN question LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END),
                    hit_count DESC LIMIT ?""",
        (owner, pat, pat, pat, pre, limit)).fetchall()
    return [kb_dict(e) for e in rows]


def mark_needs_review(conn, kb_id):
    if kb_id:
        conn.execute("UPDATE t_kb_entry SET status='NEEDS_REVIEW', updated_at=? "
                     "WHERE id=? AND deleted=0", (now_s(), kb_id))


def create_thread(conn, asker, to, content):
    if not (content or "").strip():
        raise ApiErr(400, "问题内容不能为空")
    if to == asker:
        raise ApiErr(400, "不能向自己提问")
    if not get_user(conn, to):
        raise ApiErr(404, "用户不存在: %s" % to)
    now = now_s()
    clean = strip_refs(content)
    cur = conn.execute(
        "INSERT INTO t_thread(asker,addressee,title,status,created_at,updated_at) "
        "VALUES(?,?,?,'OPEN',?,?)", (asker, to, make_title(clean), now, now))
    tid = cur.lastrowid
    conn.execute("INSERT INTO t_message(thread_id,sender,role,content,created_at) "
                 "VALUES(?,?,'USER',?,?)", (tid, asker, content, now))

    hit = strong_hit(conn, to, clean)
    candidates = None
    if hit:
        conn.execute("INSERT INTO t_message(thread_id,sender,role,content,created_at) "
                     "VALUES(?,?,'BOT',?,?)", (tid, to, hit["answer"], now))
        conn.execute("UPDATE t_thread SET status='AUTO_ANSWERED', kb_entry_id=?, "
                     "updated_at=? WHERE id=?", (hit["id"], now, tid))
        conn.execute("UPDATE t_kb_entry SET hit_count=hit_count+1 WHERE id=?", (hit["id"],))
    else:
        candidates = weak_hits(conn, to, clean)
    conn.commit()
    t = conn.execute("SELECT * FROM t_thread WHERE id=?", (tid,)).fetchone()
    msgs = thread_messages(conn, tid)
    return thread_dict(t, messages=msgs, candidates=candidates)


def thread_messages(conn, tid):
    return conn.execute("SELECT * FROM t_message WHERE thread_id=? ORDER BY id",
                        (tid,)).fetchall()


def get_thread(conn, tid):
    t = conn.execute("SELECT * FROM t_thread WHERE id=?", (tid,)).fetchone()
    if not t:
        raise ApiErr(404, "问题 #%s 不存在" % tid)
    return t


def append_message(conn, user, tid, content):
    """双方追加消息:回答方回复→RESOLVED(首答自动沉淀 KB);提问方追问→OPEN。"""
    if not (content or "").strip():
        raise ApiErr(400, "消息内容不能为空")
    t = get_thread(conn, tid)
    if user not in (t["asker"], t["addressee"]):
        raise ApiErr(404, "问题 #%s 不存在" % tid)
    now = now_s()
    settled_kb = None
    dup_kb = None
    if user == t["addressee"]:
        first_answer = conn.execute(
            "SELECT 1 FROM t_message WHERE thread_id=? AND sender=? AND role='USER' LIMIT 1",
            (tid, user)).fetchone()
        if not first_answer:  # 首答:沉淀知识库(same as classic reply → createFromAnswer)
            first_q = conn.execute(
                "SELECT content FROM t_message WHERE thread_id=? AND sender=? "
                "AND role='USER' ORDER BY id LIMIT 1", (tid, t["asker"])).fetchone()
            q_clean = strip_refs(first_q["content"])[:500]
            if t["kb_entry_id"]:
                # 本 thread 已关联条目(代答命中后被追问转待复核):合并原条而非新建
                dup_kb = t["kb_entry_id"]
            else:
                dup = strong_hit_any(conn, user, q_clean)
                if dup:
                    # 同一问题在其他 thread 沉淀过:不重复建条,关联原条由人决定是否合并
                    conn.execute("UPDATE t_thread SET kb_entry_id=? WHERE id=?",
                                 (dup["id"], tid))
                    dup_kb = dup["id"]
                else:
                    now2 = now_s()
                    cur = conn.execute(
                        "INSERT INTO t_kb_entry(owner,question,answer,source,source_thread_id,"
                        "status,created_at,updated_at) VALUES(?,?,?,'ASK',?,'ACTIVE',?,?)",
                        (user, q_clean, strip_refs(content), tid, now2, now2))
                    settled_kb = cur.lastrowid
                    conn.execute("UPDATE t_thread SET kb_entry_id=? WHERE id=?",
                                 (settled_kb, tid))
    conn.execute("INSERT INTO t_message(thread_id,sender,role,content,created_at) "
                 "VALUES(?,?,'USER',?,?)", (tid, user, content, now))
    if user == t["addressee"]:
        conn.execute("UPDATE t_thread SET status='RESOLVED', updated_at=? WHERE id=?",
                     (now, tid))
    else:
        # 提问方发言 = 追问;对代答回答追问 = escalate 联动(命中条目转待复核)
        if t["status"] == "AUTO_ANSWERED":
            conn.execute("UPDATE t_thread SET escalated=1 WHERE id=?", (tid,))
            mark_needs_review(conn, t["kb_entry_id"])
        conn.execute("UPDATE t_thread SET status='OPEN', updated_at=? WHERE id=?",
                     (now, tid))
    conn.commit()
    t = conn.execute("SELECT * FROM t_thread WHERE id=?", (tid,)).fetchone()
    return thread_dict(t, messages=thread_messages(conn, tid)), settled_kb, dup_kb


def feedback_thread(conn, user, tid, helpful, comment):
    t = get_thread(conn, tid)
    if user != t["asker"]:
        raise ApiErr(403, "仅提问人可反馈")
    if t["status"] not in ("RESOLVED", "AUTO_ANSWERED"):
        raise ApiErr(400, "问题还没有答案,无可反馈")
    conn.execute("UPDATE t_thread SET feedback=?, feedback_comment=? WHERE id=?",
                 ("HELPFUL" if helpful else "NOT", comment, tid))
    if not helpful:
        mark_needs_review(conn, t["kb_entry_id"])
    conn.commit()
    return thread_dict(conn.execute("SELECT * FROM t_thread WHERE id=?", (tid,)).fetchone())


def escalate_thread(conn, user, tid):
    t = get_thread(conn, tid)
    if user != t["asker"]:
        raise ApiErr(403, "仅提问人可转人工")
    if t["status"] != "AUTO_ANSWERED":
        raise ApiErr(400, "仅分身代答的问题可转人工")
    conn.execute("UPDATE t_thread SET status='OPEN', escalated=1, updated_at=? "
                 "WHERE id=?", (now_s(), tid))
    mark_needs_review(conn, t["kb_entry_id"])
    conn.commit()
    return thread_dict(conn.execute("SELECT * FROM t_thread WHERE id=?", (tid,)).fetchone())


# ── 附件 ──────────────────────────────────────────────────────

def save_attachment(conn, owner, biz_type, filename, content):
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in IMG_EXTS:
        ctype, limit = IMG_EXTS[ext], IMG_MAX
    elif ext in TEXT_EXTS:
        ctype, limit = "text/plain", TEXT_MAX
    else:
        raise ApiErr(400, "不支持的附件类型 %s(支持图片 png/jpg/webp/gif 与文本 log/txt/md/json 等)" % ext)
    if len(content) > limit:
        raise ApiErr(413, "附件超过大小限制(%dMB)" % (limit // 1024 // 1024))
    key = str(uuid.uuid4())
    with open(os.path.join(FILES_DIR, key), "wb") as f:
        f.write(content)
    conn.execute("INSERT INTO t_attachment VALUES(?,?,?,?,?,?,?)",
                 (key, owner, biz_type or "ASK", filename, ctype, len(content), now_s()))
    conn.commit()
    md = "![%s](/api/attachment/%s)" % (filename, key) if ctype.startswith("image/") \
        else "[%s](/api/attachment/%s)" % (filename, key)
    return {"key": key, "fileName": filename, "contentType": ctype,
            "sizeBytes": len(content), "markdown": md}


# ── HTTP ──────────────────────────────────────────────────────

SECRET = None


class Handler(BaseHTTPRequestHandler):
    server_version = "askme/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s [%s] %s\n" % (self.address_string(),
                                           datetime.now().strftime("%H:%M:%S"), fmt % args))

    # ---- 基础 ----

    def _send(self, status, data=None, message="ok", code=None, raw=None,
              content_type="application/json; charset=utf-8", extra_headers=None):
        body = raw if raw is not None else json.dumps(
            {"code": code if code is not None else status,
             "message": message, "data": data}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > MAX_BODY:
            raise ApiErr(413, "请求体过大(上限 %dMB)" % (MAX_BODY // 1024 // 1024))
        return self.rfile.read(length)

    def _read_json(self):
        raw = self._read_body()
        if not raw:
            return {}
        try:
            v = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiErr(400, "请求体不是合法 JSON")
        if not isinstance(v, dict):
            raise ApiErr(400, "请求体应为 JSON 对象")
        return v

    def _read_multipart(self):
        """解析 multipart/form-data → (fields, files);files: [(field,filename,ctype,bytes)]。"""
        m = re.search(r'boundary="?([^";]+)"?', self.headers.get("Content-Type", ""))
        if not m:
            raise ApiErr(400, "expected multipart/form-data")
        boundary = m.group(1).encode("utf-8")
        body = self._read_body()
        fields, files = {}, []
        head_marker = b"--" + boundary + b"\r\n"
        if body.startswith(head_marker):
            body = body[len(head_marker):]
        segs = body.split(b"\r\n--" + boundary + b"\r\n")
        if segs and segs[-1].endswith(b"--" + boundary + b"--\r\n"):
            segs[-1] = segs[-1][: -len(b"--" + boundary + b"--\r\n")]
        elif segs and segs[-1].endswith(b"--" + boundary + b"--"):
            segs[-1] = segs[-1][: -len(b"--" + boundary + b"--")]
        for seg in segs:
            if not seg:
                continue
            if b"\r\n\r\n" not in seg:
                continue
            raw_head, content = seg.split(b"\r\n\r\n", 1)
            head = raw_head.decode("utf-8", "replace")
            name_m = re.search(r'name="([^"]*)"', head)
            file_m = re.search(r'filename="([^"]*)"', head)
            ct_m = re.search(r"Content-Type:\s*([^\r\n]+)", head, re.I)
            field = name_m.group(1) if name_m else ""
            if file_m is not None:
                files.append((field, file_m.group(1),
                              (ct_m.group(1).strip() if ct_m else "application/octet-stream"),
                              content))
            else:
                fields[field] = content.decode("utf-8", "replace")
        return fields, files

    def _auth_user(self):
        """校验 Bearer token;失败抛 401(HTTP 401 触发客户端自动重登)。"""
        auth = self.headers.get("Authorization") or ""
        token = auth[7:] if auth.startswith("Bearer ") else ""
        username = parse_token(SECRET, token) if token else None
        if not username:
            raise ApiErr(401, "未登录或登录已过期")
        conn = db()
        try:
            u = get_user(conn, username)
            if not u:
                raise ApiErr(401, "账号不存在")
            return u
        finally:
            conn.close()

    # ---- 路由 ----

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method):
        try:
            self._dispatch(method)
        except ApiErr as e:
            self._send(e.status, None, e.message, code=e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            self._send(500, None, "internal error: %s" % e, code=500)

    def _dispatch(self, method):
        url = urlparse(self.path)
        path = url.path
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}

        # 免鉴权:CLI 分发广场(版本/变更日志/zip 下载,服务器 dist/ 目录即发布产物)
        if method == "GET" and path in ("/api/cli/version", "/api/cli/logs",
                                        "/api/cli/download"):
            return self._cli_dist(path, qs)

        # 免鉴权:健康检查 / 附件下载(capability-URL)/ 登录 / 状态页
        if method == "GET" and path == "/":
            return self._status_page()
        if method == "GET" and path == "/api/health":
            return self._send(200, {"status": "ok", "version": VERSION,
                                    "uptimeSec": int(time.time() - STARTED_AT)})
        if method == "POST" and path == "/api/auth/login":
            return self._login()
        if method == "GET" and path.startswith("/api/attachment/"):
            return self._download(path[len("/api/attachment/"):] )

        user = self._auth_user()
        conn = db()
        try:
            # ── auth ──
            if method == "GET" and path == "/api/auth/me":
                return self._send(200, {"username": user["username"],
                                        "displayName": user["display_name"]})

            # ── threads ──
            if method == "POST" and path == "/api/threads":
                body = self._read_json()
                t = create_thread(conn, user["username"],
                                  (body.get("to") or "").strip(), body.get("question") or "")
                return self._send(200, t)
            if method == "GET" and path == "/api/threads/inbox":
                limit = min(int(qs.get("limit", 20) or 20), 100)
                rows = conn.execute(
                    "SELECT * FROM t_thread WHERE addressee=? ORDER BY updated_at DESC LIMIT ?",
                    (user["username"], limit)).fetchall()
                return self._send(200, [thread_dict(t) for t in rows])
            if method == "GET" and path == "/api/threads/sent":
                limit = min(int(qs.get("limit", 50) or 50), 100)
                rows = conn.execute(
                    "SELECT * FROM t_thread WHERE asker=? ORDER BY updated_at DESC LIMIT ?",
                    (user["username"], limit)).fetchall()
                return self._send(200, [thread_dict(t) for t in rows])
            m = re.fullmatch(r"/api/threads/(\d+)", path)
            if m:
                t = get_thread(conn, int(m.group(1)))
                if user["username"] not in (t["asker"], t["addressee"]):
                    raise ApiErr(404, "问题 #%s 不存在" % m.group(1))
                return self._send(200, thread_dict(
                    t, messages=thread_messages(conn, t["id"])))
            m = re.fullmatch(r"/api/threads/(\d+)/messages", path)
            if m and method == "POST":
                body = self._read_json()
                t, settled_kb, dup_kb = append_message(
                    conn, user["username"], int(m.group(1)), body.get("content") or "")
                return self._send(200, {"thread": t, "settledKbId": settled_kb,
                                        "dupKbId": dup_kb})
            m = re.fullmatch(r"/api/threads/(\d+)/feedback", path)
            if m and method == "POST":
                body = self._read_json()
                if not isinstance(body.get("helpful"), bool):
                    raise ApiErr(400, "helpful 必须为 true/false")
                return self._send(200, feedback_thread(
                    conn, user["username"], int(m.group(1)),
                    body["helpful"], body.get("comment")))
            m = re.fullmatch(r"/api/threads/(\d+)/escalate", path)
            if m and method == "POST":
                return self._send(200, escalate_thread(conn, user["username"], int(m.group(1))))

            # ── kb ──
            if method == "GET" and path == "/api/kb/search":
                q = (qs.get("q") or "").strip()
                if not q:
                    raise ApiErr(400, "缺少参数 q")
                owner = (qs.get("owner") or user["username"]).strip()
                limit = min(int(qs.get("limit", 10) or 10), 20)
                return self._send(200, weak_hits(conn, owner, q, limit))
            if method == "GET" and path == "/api/kb":
                limit = min(int(qs.get("limit", 50) or 50), 200)
                status = qs.get("status")
                sql = "SELECT * FROM t_kb_entry WHERE owner=? AND deleted=0"
                args = [user["username"]]
                if status:
                    sql += " AND status=?"
                    args.append(status)
                sql += " ORDER BY id DESC LIMIT ?"
                args.append(limit)
                return self._send(200, [kb_dict(e) for e in conn.execute(sql, args).fetchall()])
            if method == "POST" and path == "/api/kb":
                body = self._read_json()
                if not (body.get("question") or "").strip():
                    raise ApiErr(400, "question 不能为空")
                now = now_s()
                cur = conn.execute(
                    "INSERT INTO t_kb_entry(owner,question,question_alts,answer,tags,source,"
                    "status,created_at,updated_at) VALUES(?,?,?,?,?,'MANUAL','ACTIVE',?,?)",
                    (user["username"], body["question"].strip(), body.get("questionAlts"),
                     body.get("answer"), body.get("tags"), now, now))
                conn.commit()
                return self._send(200, kb_dict(conn.execute(
                    "SELECT * FROM t_kb_entry WHERE id=?", (cur.lastrowid,)).fetchone()))
            m = re.fullmatch(r"/api/kb/(\d+)", path)
            if m:
                kid = int(m.group(1))
                e = conn.execute("SELECT * FROM t_kb_entry WHERE id=? AND deleted=0",
                                 (kid,)).fetchone()
                if not e:
                    raise ApiErr(404, "知识条目 kb#%s 不存在" % kid)
                if method == "GET":
                    return self._send(200, kb_dict(e))
                if e["owner"] != user["username"]:
                    raise ApiErr(403, "仅条目所有者可修改/删除")
                if method == "PUT":
                    body = self._read_json()
                    cols = {"question": "question", "questionAlts": "question_alts",
                            "answer": "answer", "tags": "tags", "status": "status"}
                    sets, args = [], []
                    for k, col in cols.items():
                        if k in body and body[k] is not None:
                            sets.append("%s=?" % col)
                            args.append(body[k])
                    if not sets:
                        raise ApiErr(400, "没有要更新的字段")
                    sets.append("updated_at=?")
                    args.append(now_s())
                    args.append(kid)
                    conn.execute("UPDATE t_kb_entry SET %s WHERE id=?" % ", ".join(sets), args)
                    conn.commit()
                    return self._send(200, kb_dict(conn.execute(
                        "SELECT * FROM t_kb_entry WHERE id=?", (kid,)).fetchone()))
                if method == "DELETE":
                    conn.execute("UPDATE t_kb_entry SET deleted=1, updated_at=? WHERE id=?",
                                 (now_s(), kid))
                    conn.commit()
                    return self._send(200, {"deleted": kid})

            # ── attachment upload ──
            if method == "POST" and path == "/api/attachment":
                fields, files = self._read_multipart()
                up = [f for f in files if f[0] == "file"]
                if not up:
                    raise ApiErr(400, "缺少 file 字段(multipart/form-data)")
                _, filename, _, content = up[0]
                info = save_attachment(conn, user["username"], fields.get("bizType"),
                                       os.path.basename(filename), content)
                return self._send(200, info)

            raise ApiErr(404, "接口不存在: %s %s" % (method, path))
        finally:
            conn.close()

    # ---- 免鉴权端点 ----

    def _cli_dist(self, path, qs):
        """CLI 分发广场:dist/<skill>.json(manifest+history) + dist/<skill>.zip。"""
        skill = (qs.get("skill") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,32}", skill):
            raise ApiErr(400, "skill 参数无效")
        dist_dir = os.path.join(os.path.dirname(DATA_DIR), "dist")
        manifest_path = os.path.join(dist_dir, skill + ".json")
        if not os.path.isfile(manifest_path):
            raise ApiErr(404, "该 skill 尚未发布: %s" % skill)
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        if path == "/api/cli/version":
            return self._send(200, {k: manifest.get(k) for k in
                                     ("skill", "version", "changeDesc", "releasedAt")})
        if path == "/api/cli/logs":
            return self._send(200, manifest.get("history") or [])
        zip_path = os.path.join(dist_dir, skill + ".zip")
        if not os.path.isfile(zip_path):
            raise ApiErr(404, "zip 不存在: %s" % skill)
        with open(zip_path, "rb") as f:
            blob = f.read()
        self._send(200, raw=blob, content_type="application/zip",
                   extra_headers={"Content-Disposition": 'attachment; filename="%s.zip"' % skill})

    def _login(self):
        body = self._read_json()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        conn = db()
        try:
            u = get_user(conn, username)
            if not u or not verify_password(password, u["password_hash"]):
                raise ApiErr(401, "用户名或密码错误")
            token = make_token(SECRET, username)
            return self._send(200, {"token": token,
                                    "user": {"username": u["username"],
                                             "displayName": u["display_name"]}})
        finally:
            conn.close()

    def _download(self, key):
        if not re.fullmatch(r"[0-9a-fA-F-]{8,}", key or ""):
            raise ApiErr(404, "附件不存在")
        conn = db()
        try:
            a = conn.execute("SELECT * FROM t_attachment WHERE att_key=?", (key,)).fetchone()
        finally:
            conn.close()
        path = os.path.join(FILES_DIR, key)
        if not a or not os.path.isfile(path):
            raise ApiErr(404, "附件不存在")
        with open(path, "rb") as f:
            blob = f.read()
        fname = quote(a["file_name"] or key)
        self._send(200, raw=blob, content_type=a["content_type"] or "application/octet-stream",
                   extra_headers={
                       "Content-Disposition": "inline; filename*=UTF-8''%s" % fname,
                       "Cache-Control": "private, max-age=86400, immutable"})

    def _status_page(self):
        html = ("<!doctype html><meta charset='utf-8'><title>askme</title>"
                "<body style=\"font-family:system-ui;max-width:40em;margin:4em auto\">"
                "<h1>askme <small style='color:#2a7'>✅</small></h1>"
                "<p>两人私享问答服务 · version %s</p>"
                "<p>用 CLI(<code>askme.py</code>)交互:<code>askme ask &lt;对方&gt; "
                "\"问题\"</code> / <code>askme inbox</code></p></body>" % VERSION)
        self._send(200, raw=html.encode("utf-8"), content_type="text/html; charset=utf-8")


# ── CLI 子命令:adduser / serve ───────────────────────────────

def cmd_adduser(args):
    import getpass
    init_store()
    username = args.username.strip()
    if not re.fullmatch(r"[0-9A-Za-z_.-]{2,32}", username):
        print("✗ 用户名限 2-32 位字母数字 _ . -(CLI 里当对方标识用,建议简短)")
        return 1
    password = args.password or getpass.getpass("为 %s 设置密码: " % username)
    if len(password) < 4:
        print("✗ 密码至少 4 位")
        return 1
    conn = db()
    try:
        if get_user(conn, username):
            if args.password:
                conn.execute("UPDATE t_user SET display_name=?, password_hash=? WHERE username=?",
                             (args.display or username, hash_password(password), username))
                conn.commit()
                print("✓ 用户 %s 已存在,密码已重置" % username)
                return 0
            print("✗ 用户 %s 已存在(重置密码用 --password)" % username)
            return 1
        conn.execute("INSERT INTO t_user VALUES(?,?,?,?)",
                     (username, args.display or username, hash_password(password), now_s()))
        conn.commit()
        print("✓ 已创建用户 %s" % username)
        return 0
    finally:
        conn.close()


def cmd_serve(args):
    global SECRET
    init_store()
    SECRET = load_secret()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("askme %s serving on http://%s:%d (data: %s)" % (VERSION, args.host, args.port, DATA_DIR))
    print("对外请经 caddy/nginx 反代终结 TLS;先创建账号: python3 %s adduser <名字>"
          % os.path.basename(__file__))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv):
    p = argparse.ArgumentParser(prog="server.py", description="askme 单文件服务端")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="启动服务")
    s.add_argument("--host", default=DEFAULT_HOST)
    s.add_argument("--port", type=int, default=int(os.environ.get("ASKME_PORT", DEFAULT_PORT)))
    s.add_argument("--data-dir", default=None)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("adduser", help="创建/重置账号")
    s.add_argument("username")
    s.add_argument("--display", help="显示名(默认同用户名)")
    s.add_argument("--password", help="不交互直接指定(脚本部署用)")
    s.set_defaults(fn=cmd_adduser)
    args = p.parse_args(argv)
    global DATA_DIR, DB_PATH, FILES_DIR, SECRET_PATH
    if getattr(args, "data_dir", None):
        DATA_DIR = args.data_dir
        DB_PATH = os.path.join(DATA_DIR, "askme.db")
        FILES_DIR = os.path.join(DATA_DIR, "files")
        SECRET_PATH = os.path.join(DATA_DIR, "secret.key")
    return args.fn(args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main(sys.argv[1:]))
