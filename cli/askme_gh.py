#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""askme_gh — askme 的 GitHub 托管后端(仓库即数据库)。

与 askme.py(服务器版 CLI)配合:askme.py 通过 backend 选择 server/github,
github 后端由本模块实现。无服务器:数据 = 一个私有 GitHub 仓库,
每条命令产生一次 commit(问题历史即 git log),Contents API 乐观锁防覆盖。

仓库布局:
    users.json                    {"alice": {"displayName": "Alice"}, ...}
    threads/0001.json             {"id":1, ..., "messages":[...]}
    kb/0001.json                  知识条目
    attachments/<uuid>.<ext>      附件原件
    attachments/manifest.json     {"<uuid>": {"fileName":..., "contentType":...}}

身份 = GitHub 登录名(token 决定);协作者各持 fine-grained PAT(Contents RW)。
"""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"

ATTACH_RE = re.compile(r"!?\[[^\]\n]*\]\(attachment:([0-9a-fA-F-]{8,})\)")

IMG_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif"}
TEXT_EXTS = {".txt", ".log", ".md", ".json", ".yaml", ".yml", ".xml", ".csv", ".html",
             ".htm", ".js", ".ts", ".py", ".java", ".sql", ".sh", ".ini", ".conf",
             ".properties", ".stack", ".trace", ".out"}


class GhError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ── 底层 GitHub API(纯 stdlib)─────────────────────────────

def gh_req(token, method, path, body=None, raw=None, content_type=None, timeout=30):
    url = path if path.startswith("http") else API + path
    data = raw if raw is not None else (
        json.dumps(body).encode("utf-8") if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", content_type or "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            blob = resp.read()
            return resp.status, (json.loads(blob) if blob and blob[:1] in (b"{", b"[") else blob)
    except urllib.error.HTTPError as e:
        blob = e.read()
        try:
            msg = json.loads(blob.decode("utf-8")).get("message") or str(e)
        except Exception:
            msg = blob.decode("utf-8", "replace")[:200] or str(e)
        raise GhError(e.code, msg)
    except urllib.error.URLError as e:
        raise GhError(0, "连接失败: %s" % e.reason)


def _get_json(token, path, timeout=30):
    _, data = gh_req(token, "GET", path, timeout=timeout)
    return data


def get_blob(token, repo, path, ref="main"):
    """取文件原文(bytes);不存在返回 None。"""
    try:
        _, data = gh_req(token, "GET",
                         "/repos/%s/contents/%s?ref=%s" % (repo, path, ref))
    except GhError as e:
        if e.status == 404:
            return None
        raise
    if isinstance(data, dict) and data.get("content") is not None:
        return base64.b64decode(data.get("content") or "")
    return None


def put_blob(token, repo, path, content_bytes, message, sha=None, branch="main"):
    """写文件(带 sha 乐观锁);返回新 sha。冲突抛 GhError(409)。"""
    body = {"message": message, "content": base64.b64encode(content_bytes).decode("ascii"),
            "branch": branch}
    if sha:
        body["sha"] = sha
    _, data = gh_req(token, "PUT", "/repos/%s/contents/%s" % (repo, path), body=body)
    return data["content"]["sha"]


def list_dir(token, repo, dirpath, ref="main"):
    """列目录 → [(path, sha, size)];不存在返回 []。"""
    try:
        _, data = gh_req(token, "GET",
                         "/repos/%s/contents/%s?ref=%s" % (repo, dirpath, ref))
    except GhError as e:
        if e.status == 404:
            return []
        raise
    if isinstance(data, list):
        return [(d["path"], d.get("sha"), d.get("size", 0)) for d in data]
    return []


def get_me(token):
    _, u = gh_req(token, "GET", "/user")
    return u


# ── 文档读写(commit message = 动作日志)────────────────────

def _load_json(token, repo, path):
    blob = get_blob(token, repo, path)
    return (json.loads(blob.decode("utf-8")), None) if blob else (None, None)


def _save_json(token, repo, path, obj, message):
    blob = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    sha = None
    try:
        _, meta = gh_req(token, "GET", "/repos/%s/contents/%s" % (repo, path))
        sha = meta.get("sha")
    except GhError:
        pass
    return put_blob(token, repo, path, blob, message, sha=sha)


def strip_refs(text):
    """删除附件引用行(uuid 每次不同,不剥会污染检索;与服务器版 stripAttachmentRefs 对齐)。"""
    kept = [ln for ln in (text or "").splitlines() if not ATTACH_RE.search(ln)]
    return "\n".join(kept).strip()


# ── 业务逻辑(状态机在客户端执行,写回结算后的完整状态)─────

def make_title(text, n=60):
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln:
            return ln[:n]
    return "(空)"


def is_prefix_match(a, b):
    a = (a or "").strip().casefold()
    b = (b or "").strip().casefold()
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


def _kb_all(token, repo):
    out = []
    for path, _, _ in list_dir(token, repo, "kb"):
        e, _ = _load_json(token, repo, path)
        if e and not e.get("deleted"):
            e["id"] = os.path.splitext(os.path.basename(path))[0]
            out.append(e)
    return out


def _strong_hit(entries, owner, question, active_only=True):
    for e in entries:
        if e.get("owner") != owner or (active_only and e.get("status") != "ACTIVE"):
            continue
        if is_prefix_match(question, e.get("question")):
            return e
        for alt in (e.get("questionAlts") or "").splitlines():
            if alt.strip() and is_prefix_match(question, alt):
                return e
    return None


def _weak_hits(entries, owner, question, limit=5):
    q = (question or "").casefold()
    scored = []
    for e in entries:
        if e.get("owner") != owner or e.get("status") != "ACTIVE":
            continue
        hay = " ".join([e.get("question") or "", e.get("questionAlts") or "",
                        e.get("tags") or ""]).casefold()
        if q and q in hay:
            pre = 0 if any((e.get("question") or "").casefold().startswith(q)
                           for _ in (0,)) else 1
            scored.append((pre, -e.get("hitCount", 0), e))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [e for _, _, e in scored[:limit]]


def _next_id(token, repo, kind):
    """threads/kb 的下一个编号 = 目录里最大 + 1。"""
    items = list_dir(token, repo, kind)
    nums = []
    for path, _, _ in items:
        m = re.fullmatch(r"%s/(\d+)\.json" % kind, path)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def _upload_attach(token, repo, me, path, biz_type):
    ext = os.path.splitext(path)[1].lower()
    if ext in IMG_EXTS:
        ctype = IMG_EXTS[ext]
    elif ext in TEXT_EXTS:
        ctype = "text/plain"
    else:
        raise GhError(400, "不支持的附件类型 %s" % ext)
    with open(os.path.expanduser(path), "rb") as f:
        content = f.read()
    if len(content) > (5 << 20 if ctype.startswith("image/") else 2 << 20):
        raise GhError(413, "附件超过大小限制")
    key = "%s%s" % (__import__("uuid").uuid4(), ext)
    put_blob(token, repo, "attachments/" + key, content,
             "[attach] %s (%dB)" % (os.path.basename(path), len(content)))
    md = "![%s](attachment:%s)" % (os.path.basename(path), key)
    # manifest 记录文件名(下载还原用)
    mani, _ = _load_json(token, repo, "attachments/manifest.json")
    mani = mani or {}
    mani[key] = {"fileName": os.path.basename(path), "contentType": ctype,
                 "sizeBytes": len(content), "bizType": biz_type, "owner": me}
    _save_json(token, repo, "attachments/manifest.json", mani,
               "[attach] manifest += %s" % key)
    return md


def ask(token, repo, me, to, content, display_names=None):
    display_names = display_names or {}
    clean = strip_refs(content)
    if not clean.strip():
        raise GhError(400, "问题内容不能为空")
    if to == me:
        raise GhError(400, "不能向自己提问")
    if to not in display_names and display_names != {}:
        raise GhError(404, "用户不存在: %s" % to)
    tid = _next_id(token, repo, "threads")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    t = {"id": tid, "asker": me, "addressee": to, "title": make_title(clean),
         "status": "OPEN", "kbEntryId": None, "escalated": False,
         "feedback": None, "feedbackComment": None,
         "createdAt": now, "updatedAt": now, "messages": []}
    t["messages"].append({"sender": me, "role": "USER", "content": content,
                          "createdAt": now})

    entries = _kb_all(token, repo)
    candidates = None
    hit = _strong_hit(entries, to, clean)
    if hit:
        t["messages"].append({"sender": to, "role": "BOT", "content": hit.get("answer") or "",
                              "createdAt": now})
        t["status"] = "AUTO_ANSWERED"
        t["kbEntryId"] = hit["id"]
        hit["hitCount"] = hit.get("hitCount", 0) + 1
        _save_json(token, repo, "kb/%s.json" % hit["id"], hit,
                   "[auto-answer] kb#%s hits+1 (thread #%d)" % (hit["id"], tid))
    else:
        candidates = _weak_hits(entries, to, clean)

    _save_json(token, repo, "threads/%04d.json" % tid, t, "[ask] #%d %s -> %s: %s"
               % (tid, me, to, t["title"]))
    return t, candidates


def _find_thread(token, repo, tid):
    t, _ = _load_json(token, repo, "threads/%04d.json" % tid)
    if t is None:
        m = re.fullmatch(r"(\d+)", str(tid))
        if m:  # 兼容非补零文件名
            t, _ = _load_json(token, repo, "threads/%s.json" % tid)
    if t is None:
        raise GhError(404, "问题 #%s 不存在" % tid)
    return t


def reply(token, repo, me, tid, content):
    t = _find_thread(token, repo, tid)
    if me not in (t["asker"], t["addressee"]):
        raise GhError(404, "问题 #%s 不存在" % tid)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    settled_kb = dup_kb = None
    if me == t["addressee"]:
        first_answer = not any(m["sender"] == me and m["role"] == "USER"
                               for m in t["messages"])
        if first_answer:
            first_q = next((m["content"] for m in t["messages"]
                            if m["sender"] == t["asker"] and m["role"] == "USER"), "")
            q_clean = strip_refs(first_q)[:500]
            if t.get("kbEntryId"):
                dup_kb = t["kbEntryId"]  # 本 thread 已关联条目(代答后被追问):合并原条
            else:
                entries = _kb_all(token, repo)
                dup = _strong_hit(entries, me, q_clean, active_only=False)
                if dup:
                    t["kbEntryId"] = dup["id"]
                    dup_kb = dup["id"]
                else:
                    kid = _next_id(token, repo, "kb")
                    kb = {"id": str(kid), "owner": me, "question": q_clean,
                          "questionAlts": None, "answer": strip_refs(content),
                          "tags": None, "source": "ASK", "sourceThreadId": tid,
                          "status": "ACTIVE", "hitCount": 0,
                          "createdAt": now, "updatedAt": now}
                    _save_json(token, repo, "kb/%s.json" % kid, kb,
                               "[settle] kb#%s <- thread #%d" % (kid, tid))
                    settled_kb = str(kid)
                    t["kbEntryId"] = str(kid)
    t["messages"].append({"sender": me, "role": "USER", "content": content,
                          "createdAt": now})
    if me == t["addressee"]:
        t["status"] = "RESOLVED"
    else:
        if t["status"] == "AUTO_ANSWERED":
            t["escalated"] = True
            if t.get("kbEntryId"):
                e, _ = _load_json(token, repo, "kb/%s.json" % t["kbEntryId"])
                if e:
                    e["status"] = "NEEDS_REVIEW"
                    e["updatedAt"] = now
                    _save_json(token, repo, "kb/%s.json" % e["id"], e,
                               "[needs-review] kb#%s (follow-up on #%d)" % (e["id"], tid))
        t["status"] = "OPEN"
    t["updatedAt"] = now
    _save_json(token, repo, "threads/%04d.json" % t["id"], t, "[reply] #%d by %s"
               % (t["id"], me))
    return t, settled_kb, dup_kb


def feedback(token, repo, me, tid, helpful, comment):
    t = _find_thread(token, repo, tid)
    if me != t["asker"]:
        raise GhError(403, "仅提问人可反馈")
    if t["status"] not in ("RESOLVED", "AUTO_ANSWERED"):
        raise GhError(400, "问题还没有答案,无可反馈")
    t["feedback"] = "HELPFUL" if helpful else "NOT"
    t["feedbackComment"] = comment
    if not helpful and t.get("kbEntryId"):
        e, _ = _load_json(token, repo, "kb/%s.json" % t["kbEntryId"])
        if e:
            e["status"] = "NEEDS_REVIEW"
            e["updatedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save_json(token, repo, "kb/%s.json" % e["id"], e,
                       "[needs-review] kb#%s (feedback on #%d)" % (e["id"], tid))
    t["updatedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_json(token, repo, "threads/%04d.json" % t["id"], t,
               "[feedback] #%d %s" % (t["id"], t["feedback"]))
    return t


def escalate(token, repo, me, tid):
    t = _find_thread(token, repo, tid)
    if me != t["asker"]:
        raise GhError(403, "仅提问人可转人工")
    if t["status"] != "AUTO_ANSWERED":
        raise GhError(400, "仅分身代答的问题可转人工")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    t["status"] = "OPEN"
    t["escalated"] = True
    if t.get("kbEntryId"):
        e, _ = _load_json(token, repo, "kb/%s.json" % t["kbEntryId"])
        if e:
            e["status"] = "NEEDS_REVIEW"
            e["updatedAt"] = now
            _save_json(token, repo, "kb/%s.json" % e["id"], e,
                       "[needs-review] kb#%s (escalate #%d)" % (e["id"], tid))
    t["updatedAt"] = now
    _save_json(token, repo, "threads/%04d.json" % t["id"], t, "[escalate] #%d" % t["id"])
    return t


def ensure_user(token, repo, me, display):
    users, _ = _load_json(token, repo, "users.json")
    users = users or {}
    if me not in users or users[me].get("displayName") != display:
        users[me] = {"displayName": display}
        _save_json(token, repo, "users.json", users, "[user] %s joined" % me)
    return users


def list_users(token, repo):
    users, _ = _load_json(token, repo, "users.json")
    return users or {}
