#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""askme — 两人私享问答 CLI(单文件,纯标准库,跨平台,Python 3.8+)。

配合 askme server(server.py)使用,覆盖多轮问答闭环:
  login / whoami / ask / inbox(show)/ reply / sent / feedback / escalate / kb(...)。
登录一次自动续期:token 过期自动用保存的账号重登。

用法示例:
    python askme.py login --user alice --password 'xxx'
    python askme.py ask bob "这个报错怎么排查?" --img err.png --file app.log
    python askme.py inbox
    python askme.py inbox show 3 --save-attachments ./q3-materials
    python askme.py reply 3 "排查步骤..."          # 回复(被问方)或追问(提问方),身份自动判
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_SERVER = "http://127.0.0.1:8730"
CONFIG_PATH = os.environ.get("ASKME_CONFIG") or os.path.join(
    os.path.expanduser("~"), ".askme", "config.json")
CLI_VERSION = "1.3.0"
WEB_HOME_URL = DEFAULT_SERVER  # toast 点击打开服务状态页
UPGRADE_SKILLS = ("ask-partner", "answer-partner")  # 打包了本 CLI 的 skill,检测时取版本最高者
UPGRADE_CHECK_INTERVAL = 24 * 3600  # 自动检测最小间隔(秒)


# ── 后端选择:server(HTTP) / github(仓库即数据库)────────────

def backend_of(cfg):
    return "github" if cfg.get("backend") == "github" else "server"


def _gh():
    """GitHub 后端模块(延迟导入,server 模式零依赖本模块)。"""
    import askme_gh
    return askme_gh


def gh_ctx(cfg):
    """GitHub 模式上下文:(模块, token, repo, 我的登录名)。"""
    mod = _gh()
    token = cfg.get("gh_token")
    repo = cfg.get("gh_repo")
    if not token or not repo:
        raise ApiError("GitHub 后端未配置: askme login --backend github --gh-token <PAT> "
                       "--gh-repo <owner/repo>")
    me = cfg.get("gh_user")
    if not me:
        try:
            me = mod.get_me(token).get("login")
        except mod.GhError as e:
            raise ApiError("GitHub token 校验失败: %s" % e.message)
        cfg["gh_user"] = me
        save_config(cfg)
    return mod, token, repo, me


def gh_display_names(cfg):
    mod, token, repo, _ = gh_ctx(cfg)
    try:
        return mod.list_users(token, repo)
    except mod.GhError:
        return {}
# ── 配置 ──────────────────────────────────────────────────────

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CONFIG_PATH, 0o600)  # 含密码,POSIX 下收紧权限;Windows 忽略
    except OSError:
        pass


# ── HTTP ──────────────────────────────────────────────────────

class ApiError(Exception):
    pass


IMG_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif"}
TEXT_EXTS = {".txt", ".log", ".md", ".json", ".yaml", ".yml", ".xml", ".csv", ".html",
             ".htm", ".js", ".ts", ".py", ".java", ".sql", ".sh", ".ini", ".conf",
             ".properties", ".stack", ".trace", ".out"}


def server_of(cfg):
    return (os.environ.get("ASKME_SERVER") or cfg.get("server") or DEFAULT_SERVER).rstrip("/")


def http_json(server, method, path, body=None, token=None, timeout=30):
    url = server + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8"))
            msg = err.get("message") or err.get("msg") or str(e)
        except Exception:
            msg = str(e)
        raise ApiError(f"HTTP {e.code}: {msg}")
    except urllib.error.URLError as e:
        raise ApiError(f"连接失败: {e.reason} (server={server})")


def upload_attachment(cfg, path, biz_type="ASK"):
    """上传图片/文本附件,返回嵌入消息的 markdown 引用。GitHub 模式 = 写入仓库 attachments/。"""
    p = os.path.expanduser(path)
    if not os.path.isfile(p):
        raise ApiError(f"附件不存在: {path}")
    ext = os.path.splitext(p)[1].lower()
    if ext in IMG_EXTS:
        ctype = IMG_EXTS[ext]
    elif ext in TEXT_EXTS:
        ctype = "text/plain"
    else:
        raise ApiError(f"不支持的附件类型 {ext}(支持图片 png/jpg/webp/gif 与文本 log/txt/md/json 等)")
    if backend_of(cfg) == "github":
        mod, token, repo, me = gh_ctx(cfg)
        try:
            return mod._upload_attach(token, repo, me, p, biz_type)
        except mod.GhError as e:
            raise ApiError(e.message)
    size = os.path.getsize(p)
    server = server_of(cfg)
    token = fresh_token(cfg)

    boundary = "----askmeAttach" + uuid.uuid4().hex
    fname = os.path.basename(p).encode("ascii", "replace").decode()
    with open(p, "rb") as f:
        content = f.read()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode("utf-8") + content + (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="bizType"\r\n\r\n{biz_type}\r\n'
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    req = urllib.request.Request(server + "/api/attachment", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", "Bearer " + token)
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 1:
                token = relogin(cfg)  # 服务端重启/换密钥后 401 → 静默重登一次
                req = urllib.request.Request(server + "/api/attachment", data=body, method="POST")
                req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
                req.add_header("Authorization", "Bearer " + token)
                continue
            try:
                raise ApiError(json.loads(e.read().decode("utf-8")).get("message") or str(e))
            except json.JSONDecodeError:
                raise ApiError(str(e))
    if data.get("code") != 200:
        raise ApiError(data.get("message") or "上传失败")
    info("[附件] " + os.path.basename(p) + f" ({size}B) 已上传")
    return data["data"]["markdown"]


def attach_refs(cfg, imgs, files, biz_type="ASK"):
    """把 --img/--file 列表转成 markdown 引用串(空则空串)。"""
    refs = []
    for p in (imgs or []):
        refs.append(upload_attachment(cfg, p, biz_type))
    for p in (files or []):
        refs.append(upload_attachment(cfg, p, biz_type))
    return ("\n\n" + "\n".join(refs)) if refs else ""


def token_claims(token):
    """解析自有 token 格式 username|exp|sig,返回 (username, exp);失败 (None, 0)。"""
    try:
        username, exp, _ = token.split("|", 2)
        return username, int(exp)
    except Exception:
        return None, 0


def fresh_token(cfg):
    """拿一个可用 token:缺失/临期 → 用保存的账号静默重登。"""
    token = cfg.get("token")
    _, exp = token_claims(token) if token else (None, 0)
    if token and exp and exp < time.time() + 60:
        token = None
    if not token:
        token = relogin(cfg)
    return token


def api(cfg, method, path, body=None, _retried=False, timeout=30):
    """带自动续期的请求:token 缺失/401 → 用保存的账号静默重登一次。"""
    server = server_of(cfg)
    token = fresh_token(cfg)
    try:
        resp = http_json(server, method, path, body, token, timeout=timeout)
    except ApiError as e:
        if _retried or "HTTP 401" not in str(e):
            raise
        token = relogin(cfg)
        resp = http_json(server, method, path, body, token, timeout=timeout)
    code = resp.get("code", 200)
    if code != 200:
        raise ApiError(resp.get("message") or f"code={code}")
    return resp.get("data")


def relogin(cfg):
    if not cfg.get("username") or not cfg.get("password"):
        raise ApiError("未登录(且没有保存的账号可自动重登): 请先 askme login")
    resp = http_json(server_of(cfg), "POST", "/api/auth/login",
                     {"username": cfg["username"], "password": cfg["password"]})
    data = resp.get("data") or {}
    token = data.get("token")
    if not token:
        raise ApiError("自动重登失败: 登录接口未返回 token,请手动重新 login")
    cfg["token"] = token
    save_config(cfg)
    return token


# ── 自升级(服务器 dist/ 目录即分发广场)─────────────────────

def _version_tuple(v):
    """'1.2.0' → (1, 2, 0);无法解析视为 (0,)。"""
    try:
        return tuple(int(p) for p in str(v or "").strip().lstrip("vV").split("."))
    except ValueError:
        return (0,)


def _upgrade_skills(cfg):
    """检测升级要盯的 skill:env ASKME_UPGRADE_SKILLS / config upgrade_skills 可覆盖。"""
    raw = os.environ.get("ASKME_UPGRADE_SKILLS") or cfg.get("upgrade_skills")
    if raw:
        parts = raw if isinstance(raw, list) else str(raw).split(",")
        names = [p.strip() for p in parts if re.fullmatch(r"[a-z0-9-]+", p.strip() or "x")]
        if names:
            return names
    return list(UPGRADE_SKILLS)


def fetch_latest_skill(cfg, timeout=8):
    """查各 skill 的最新版本,返回 (版本元组, 版本串, skill名);全部失败返回 None。
    /api/cli/* 免鉴权,直连不走 api()(未登录也能升级)。"""
    server = server_of(cfg)
    best = None
    for name in _upgrade_skills(cfg):
        try:
            resp = http_json(server, "GET", f"/api/cli/version?skill={name}", timeout=timeout)
        except ApiError:
            continue
        if resp.get("code") != 200:
            continue
        d = resp.get("data") or {}
        num = _version_tuple(d.get("version"))
        if num > (0,) and (best is None or num > best[0]):
            best = (num, d.get("version"), name)
    return best


def show_change_logs(cfg, skill, baseline=(0,), limit=5):
    """列出比 baseline 新的版本变更(发布时的 changeDesc);取不到则静默。"""
    try:
        resp = http_json(server_of(cfg), "GET", f"/api/cli/logs?skill={skill}", timeout=8)
        logs = resp.get("data") or [] if resp.get("code") == 200 else []
    except ApiError:
        return
    logs.sort(key=lambda l: _version_tuple(l.get("version")), reverse=True)
    fresh = [l for l in logs if _version_tuple(l.get("version")) > baseline]
    shown = fresh or logs[:3]  # 序列对不上时兜底给最近几条
    if not shown:
        return
    print("变更内容:")
    for l in shown[:limit]:
        when = str(l.get("releasedAt") or "")[:16]
        print(f"  v{l.get('version')} {when}: {l.get('changeDesc') or ''}")


def _extract_zip(blob):
    """从 zip 提取白名单文件 {文件名: 字节}(basename 防目录穿越)。"""
    import io
    import zipfile
    allowed = {"askme.py", "askme_gh.py", "SKILL.md"}
    out = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            base = os.path.basename(name)
            if base in allowed and not name.endswith("/"):
                out[base] = zf.read(name)
    return out


def _atomic_replace(path, content):
    tmp = path + ".new"
    with open(tmp, "wb") as f:
        f.write(content)
    os.replace(tmp, path)  # 同目录原子替换;运行中的 .py 未被锁定,Windows 也可替换


def do_upgrade(cfg, skill, remote_ver):
    """下载 zip,替换本脚本(及旁边的 askme_gh.py / SKILL.md,若存在)。返回动作说明列表。"""
    server = server_of(cfg)
    url = f"{server}/api/cli/download?skill={skill}"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            blob = resp.read()
    except Exception as e:
        raise ApiError(f"下载失败: {e}")
    files = _extract_zip(blob)
    new_py = files.get("askme.py")
    if not new_py:
        raise ApiError("zip 中未找到 askme.py,无法升级")
    try:
        compile(new_py, "askme.py", "exec")  # 损坏/截断的包不能把 CLI 换挂
    except SyntaxError as e:
        raise ApiError(f"zip 内 askme.py 语法校验失败({e}),已放弃替换")
    self_path = os.path.abspath(__file__)
    here = os.path.dirname(self_path)
    # 新版模块语法也须先校验,避免换挂 GitHub 后端
    new_gh = files.get("askme_gh.py")
    if new_gh:
        try:
            compile(new_gh, "askme_gh.py", "exec")
        except SyntaxError as e:
            raise ApiError(f"zip 内 askme_gh.py 语法校验失败({e}),已放弃替换")
    with open(self_path, "rb") as f:
        current = f.read()
    actions = []
    if new_py != current:
        _atomic_replace(self_path, new_py)
        actions.append("askme.py 已更新")
    if new_gh:
        gh_path = os.path.join(here, "askme_gh.py")
        old_gh = open(gh_path, "rb").read() if os.path.exists(gh_path) else None
        if old_gh != new_gh:
            _atomic_replace(gh_path, new_gh)
            actions.append("askme_gh.py 已更新" if old_gh else "askme_gh.py 已安装")
    new_md = files.get("SKILL.md")
    md_path = os.path.join(here, "SKILL.md")
    if new_md and os.path.exists(md_path):
        with open(md_path, "rb") as f:
            old_md = f.read()
        if new_md != old_md:  # 先读完关掉句柄再替换:Windows 不容许替换占用中的文件
            _atomic_replace(md_path, new_md)
            actions.append("SKILL.md 已更新")
    cfg["upgrade_seen_version"] = remote_ver
    save_config(cfg)
    if not actions:
        actions.append("文件内容与服务器一致(仅版本号变化),无需替换")
    return actions


def maybe_auto_upgrade(args):
    """命令跑完后的轻量自检测:24h 一次,异常全静默;有新版默认提示,
    config auto_upgrade=true 时直接自动替换。"""
    if getattr(args, "cmd", None) == "upgrade" or getattr(args, "json", False):
        return  # json 输出供 agent 程序消费,不掺检测流量与噪音
    if os.environ.get("ASKME_NO_UPGRADE_CHECK"):
        return
    cfg = load_config()
    if cfg.get("upgrade_check") is False:
        return
    now = time.time()
    if now - (cfg.get("last_upgrade_check") or 0) < UPGRADE_CHECK_INTERVAL:
        return
    cfg["last_upgrade_check"] = now
    save_config(cfg)
    try:
        latest = fetch_latest_skill(cfg, timeout=5)
    except Exception:
        return
    if not latest:
        return
    num, ver, skill = latest
    baseline = max(_version_tuple(CLI_VERSION), _version_tuple(cfg.get("upgrade_seen_version")))
    if num <= baseline:
        return
    if cfg.get("auto_upgrade"):
        try:
            for a in do_upgrade(cfg, skill, ver):
                ok("  " + a)
            ok(f"♻ 已自动升级到 v{ver},下次运行生效。")
        except Exception as e:
            err(f"自动升级失败({e});可稍后手动执行: askme upgrade")
    else:
        info(f"💡 发现 askme CLI 新版本 v{ver}(当前 v{CLI_VERSION});"
             f"查看变更: askme upgrade --check,升级: askme upgrade")


# ── 输出 ──────────────────────────────────────────────────────

def info(msg):
    print(f"\033[36m{msg}\033[0m")


def ok(msg):
    print(f"\033[32m{msg}\033[0m")


def err(msg):
    print(f"\033[31m{msg}\033[0m", file=sys.stderr)


def table(headers, rows):
    cols = [[str(h) for h in headers]]
    cols += [[r[i] if i < len(r) else "" for i in range(len(headers))] for r in rows]
    widths = [max(len(c[i]) for c in cols) for i in range(len(headers))]
    for i, row in enumerate(cols):
        line = "  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip()
        print(line)
        if i == 0:
            print("  ".join("-" * w for w in widths))


def truncate(s, n):
    s = (s or "").replace("\n", " ")
    return s[:n] + "…" if len(s) > n else s


def print_thread_chat(t):
    """打印 thread 往来消息(多轮对话视图)。"""
    me = load_config().get("username")
    for m in t.get("messages") or []:
        sender = m.get("sender") or "?"
        if m.get("role") == "BOT":
            tag = "🤖 分身代答"
        else:
            tag = sender + ("  (我)" if sender == me else "")
        print(f"── [{tag}] {m.get('createdAt', '')} ──")
        print(m.get("content") or "")
        print()


# ── 命令 ──────────────────────────────────────────────────────

def cmd_login(args):
    if args.backend == "github":
        return cmd_login_github(args)
    server = (args.server or os.environ.get("ASKME_SERVER") or DEFAULT_SERVER).rstrip("/")
    cfg = {"server": server, "username": args.user}
    resp = http_json(server, "POST", "/api/auth/login",
                     {"username": args.user, "password": args.password})
    data = resp.get("data") or {}
    token = data.get("token")
    if not token:
        err("登录失败: 接口未返回 token")
        return 1
    cfg["token"] = token
    cfg["password"] = args.password  # 保存用于自动续登;文件权限已收紧
    save_config(cfg)
    user = (data.get("user") or {})
    ok("登录成功!")
    print(f"  用户:   {user.get('displayName') or args.user}")
    print(f"  Server: {server}")
    print(f"  配置:   {CONFIG_PATH}")
    return 0


def cmd_login_github(args):
    mod = _gh()
    token = args.gh_token or os.environ.get("ASKME_GH_TOKEN")
    repo = args.gh_repo or os.environ.get("ASKME_GH_REPO")
    if not token or not repo:
        err("GitHub 后端需要 --gh-token <PAT> 与 --gh-repo <owner/repo>"
            "(或环境变量 ASKME_GH_TOKEN / ASKME_GH_REPO)")
        return 1
    cfg = {"backend": "github", "gh_token": token, "gh_repo": repo}
    try:
        me = mod.get_me(token).get("login")
    except mod.GhError as e:
        err("GitHub token 校验失败: %s" % e.message)
        return 1
    cfg["gh_user"] = me
    # 服务器版字段清掉,避免混杂
    for k in ("server", "username", "password", "token"):
        cfg.pop(k, None)
    save_config(cfg)
    try:
        mod.ensure_user(token, repo, me, me)
    except mod.GhError as e:
        err("⚠ 登录成功,但初始化数据仓库失败(检查 PAT 是否授予该仓库 Contents 读写): %s" % e.message)
        return 1
    ok("登录成功(GitHub 后端)!")
    print(f"  账号:   {me} (GitHub 登录名即用户名)")
    print(f"  仓库:   {repo} (私有仓库即数据库,每条命令一次 commit)")
    print(f"  配置:   {CONFIG_PATH}")
    return 0


def cmd_whoami(args):
    cfg = load_config()
    if backend_of(cfg) == "github":
        mod, token, repo, me = gh_ctx(cfg)
        print("━" * 3, "当前用户", "━" * 3)
        print(f"账号:   {me} (GitHub)")
        print(f"后端:   github · 仓库 {repo}")
        print(f"CLI:    v{CLI_VERSION}")
        return 0
    token = cfg.get("token")
    if not token:
        err("未登录: 请先 askme login")
        return 1
    username, exp = token_claims(token)
    if exp and exp < time.time():
        relogin(cfg)
        username, exp = token_claims(cfg["token"])
    print("━" * 3, "当前用户", "━" * 3)
    print(f"账号:   {cfg.get('username')}")
    me = api(cfg, "GET", "/api/auth/me") or {}
    print(f"昵称:   {me.get('displayName') or '-'}")
    print(f"Server: {cfg.get('server') or DEFAULT_SERVER}")
    print(f"CLI:    v{CLI_VERSION}")
    if exp:
        print(f"过期:   {time.strftime('%Y-%m-%d %H:%M', time.localtime(exp))} (过期自动续登)")
    return 0


def cmd_logout(args):
    cfg = load_config()
    cfg.pop("token", None)
    cfg.pop("password", None)
    save_config(cfg)
    ok("已退出(清除本地凭据)。")
    return 0


def cmd_upgrade(args):
    cfg = load_config()
    local = _version_tuple(CLI_VERSION)
    baseline = max(local, _version_tuple(cfg.get("upgrade_seen_version")))
    print(f"当前版本: v{CLI_VERSION}" + (f" (已确认至 v{'.'.join(map(str, baseline))})" if baseline > local else ""))
    latest = fetch_latest_skill(cfg)
    if not latest:
        err("查询服务器版本失败(未发布/网络),请稍后再试")
        return 1
    num, ver, skill = latest
    print(f"服务器最新: v{ver} ({skill})")
    if num <= baseline:
        ok("已是最新,无需升级。")
        return 0
    show_change_logs(cfg, skill, baseline)  # 升级前先让发布说明可见
    if args.check:
        info(f"💡 有新版本 v{ver},升级: askme upgrade")
        return 0
    for a in do_upgrade(cfg, skill, ver):
        ok("  " + a)
    ok(f"已升级到 v{ver},下次运行生效。")
    return 0


def cmd_ask(args):
    cfg = load_config()
    to = args.to
    question = " ".join(args.question).strip()
    if not question:
        err('用法: askme ask <对方用户名> "问题"')
        return 1
    question += attach_refs(cfg, args.img, args.file)
    if backend_of(cfg) == "github":
        mod, token, repo, me = gh_ctx(cfg)
        try:
            t, candidates = mod.ask(token, repo, me, to, question,
                                    display_names=gh_display_names(cfg))
        except mod.GhError as e:
            err(e.message)
            return 1
        if getattr(args, "json", False):
            print(json.dumps({"thread": _gh_thread_view(t), "candidates": candidates},
                             ensure_ascii=False, indent=2))
            return 0
        _print_ask_result(t, candidates, to)
        return 0
    t = api(cfg, "POST", "/api/threads", {"to": to, "question": question}) or {}
    if getattr(args, "json", False):
        print(json.dumps(t, ensure_ascii=False, indent=2))
        return 0
    _print_ask_result(t, t.get("candidates"), to)
    return 0


def _gh_thread_view(t):
    """GitHub 模式的 thread 视图:字段名对齐服务器版(camelize 常用字段)。"""
    return {"id": t["id"], "asker": t["asker"], "addressee": t["addressee"],
            "title": t.get("title"), "status": t["status"],
            "kbEntryId": t.get("kbEntryId"), "escalated": t.get("escalated"),
            "feedback": t.get("feedback"), "feedbackComment": t.get("feedbackComment"),
            "createdAt": t.get("createdAt"), "updatedAt": t.get("updatedAt"),
            "messages": t.get("messages") or []}


def _print_ask_result(t, candidates, to):
    status = t.get("status")
    tid = t.get("id")
    if status == "AUTO_ANSWERED":
        info(f"🤖 {to} 的分身命中知识库(kb#{t.get('kbEntryId', '-')}),代答:")
        for m in t.get("messages") or []:
            if m.get("role") == "BOT":
                print(m.get("content") or "")
        print()
        info(f"回答来自其知识库。验证后请务必反馈: askme feedback {tid} helpful|not")
        info(f"not 会把命中的知识转待复核;要真人再答直接在本条追问: askme reply {tid} \"补充…\"")
    elif candidates:
        info(f"未强命中,给出 {len(candidates)} 个候选(供你/你的 agent 挑选):")
        for i, c in enumerate(candidates, 1):
            print(f"  [{i}] kb#{c.get('id')} hits={c.get('hitCount', 0)} {truncate(c.get('question'), 50)}")
        print()
        info(f'自行多轮检索: askme kb search "关键词" --owner {to}')
        info(f"都不合适则保持待答(#{tid}),对方稍后回复")
    else:
        info(f"已进入 {to} 的收件箱(问题 #{tid}),等对方回复。")
        info(f'你的 agent 可先自助检索: askme kb search "关键词" --owner {to}')


_PS_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $template.GetElementsByTagName('text')
$t.Item(0).AppendChild($template.CreateTextNode($title)) | Out-Null
$t.Item(1).AppendChild($template.CreateTextNode($body)) | Out-Null
$template.DocumentElement.SetAttribute('activationType', 'protocol')
$template.DocumentElement.SetAttribute('launch', $url)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier(
    '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
).Show([Windows.UI.Notifications.ToastNotification]::new($template))
"""


def _ps_single_quoted(v):
    return "'" + "".join(c for c in v.replace("'", "''") if c >= ' ') + "'"


def _desktop_notify(title, body, url=WEB_HOME_URL):
    """跨平台原生通知(纯 stdlib,失败静默):Windows WinRT toast / macOS osascript / Linux notify-send。"""
    import subprocess
    try:
        if sys.platform.startswith("win"):
            cmd = (f"$title={_ps_single_quoted(title)}; $body={_ps_single_quoted(body)}; "
                   f"$url={_ps_single_quoted(url)}; " + _PS_TOAST)
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", cmd],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x08000000)
        elif sys.platform == "darwin":
            safe_t = title.replace('"', '')
            safe_b = body.replace('"', '')
            subprocess.Popen(["osascript", "-e",
                              f'display notification "{safe_b}" with title "{safe_t}" subtitle "askme"'])
        else:
            subprocess.Popen(["notify-send", "-a", "askme", title, body])
    except Exception:
        pass


def _filter_new_open(items):
    """返回 (新待答列表, 是否首次运行)。last_inbox_id 记在 config,见过的不再提醒。"""
    cfg = load_config()
    first = "last_inbox_id" not in cfg
    last = cfg.get("last_inbox_id") or 0
    new = [t for t in items if t.get("status") == "OPEN" and t.get("id", 0) > last]
    max_id = max((t.get("id", 0) for t in items), default=last)
    if max_id > last:
        cfg["last_inbox_id"] = max_id
        save_config(cfg)
    return new, first


def _my_inbox(cfg, limit=50):
    """收件箱列表(按 backend)。GitHub 模式拉全量后过滤排序。"""
    if backend_of(cfg) == "github":
        mod, token, repo, me = gh_ctx(cfg)
        items = []
        for path, _, _ in mod.list_dir(token, repo, "threads"):
            t, _ = mod._load_json(token, repo, path)
            if t and t.get("addressee") == me:
                items.append(_gh_thread_view(t))
        items.sort(key=lambda t: t.get("updatedAt") or "", reverse=True)
        return items[:limit]
    return api(cfg, "GET", f"/api/threads/inbox?limit={limit}") or []


def _my_sent(cfg, limit=50):
    if backend_of(cfg) == "github":
        mod, token, repo, me = gh_ctx(cfg)
        items = []
        for path, _, _ in mod.list_dir(token, repo, "threads"):
            t, _ = mod._load_json(token, repo, path)
            if t and t.get("asker") == me:
                items.append(_gh_thread_view(t))
        items.sort(key=lambda t: t.get("updatedAt") or "", reverse=True)
        return items[:limit]
    return api(cfg, "GET", f"/api/threads/sent?limit={limit}") or []


def _get_thread(cfg, tid):
    if backend_of(cfg) == "github":
        mod, token, repo, me = gh_ctx(cfg)
        try:
            return _gh_thread_view(mod._find_thread(token, repo, tid))
        except mod.GhError as e:
            raise ApiError(e.message)
    return api(cfg, "GET", f"/api/threads/{tid}") or {}


def _post_message(cfg, tid, content):
    if backend_of(cfg) == "github":
        mod, token, repo, me = gh_ctx(cfg)
        try:
            t, settled, dup = mod.reply(token, repo, me, tid, content)
        except mod.GhError as e:
            raise ApiError(e.message)
        return {"thread": _gh_thread_view(t), "settledKbId": settled, "dupKbId": dup}
    return api(cfg, "POST", f"/api/threads/{tid}/messages", {"content": content}) or {}


def _post_feedback(cfg, tid, helpful, comment):
    if backend_of(cfg) == "github":
        mod, token, repo, me = gh_ctx(cfg)
        try:
            return _gh_thread_view(mod.feedback(token, repo, me, tid, helpful, comment))
        except mod.GhError as e:
            raise ApiError(e.message)
    return api(cfg, "POST", f"/api/threads/{tid}/feedback",
               {"helpful": helpful, "comment": comment})


def _post_escalate(cfg, tid):
    if backend_of(cfg) == "github":
        mod, token, repo, me = gh_ctx(cfg)
        try:
            return _gh_thread_view(mod.escalate(token, repo, me, tid))
        except mod.GhError as e:
            raise ApiError(e.message)
    return api(cfg, "POST", f"/api/threads/{tid}/escalate", {})


def cmd_inbox(args):
    cfg = load_config()
    items = _my_inbox(cfg)
    if getattr(args, "limit", None):
        items = items[: args.limit]
    if getattr(args, "notify", False):
        new, _first = _filter_new_open(items)
        if new:
            info(f"🔔 收到 {len(new)} 个新问题:")
            for t in new:
                print(f"  #{t['id']} {t.get('asker')} {truncate(t.get('title'), 40)}")
            print(f"  终端处理: askme inbox show <id>")
            print('  交给本地 agent: 对它说「处理 askme inbox 新问题: 逐条 inbox show '
                  '--save-attachments 导材料,生成答案草稿给我审,不要自动回复」')
            preview = truncate(new[0].get("title"), 36)
            _desktop_notify(f"askme 收件箱 · {len(new)} 个新问题待答", f"{preview}")
        return 0
    if not items:
        info("收件箱为空: 没有待答问题。")
        return 0
    rows = [[str(t.get("id", "")), t.get("asker", ""), t.get("status", ""),
             truncate(t.get("title"), 40),
             f"kb#{t.get('kbEntryId')}" if t.get("status") == "AUTO_ANSWERED" else "-"]
            for t in items]
    table(["ID", "FROM", "STATUS", "TITLE", "HIT"], rows)
    print()
    print('操作: askme inbox show <id> / reply <id> "答案"')
    return 0


def _parse_attachment_refs(text):
    """从 markdown 解析附件引用 → [(name, key)];兼容两种形式:
    服务器版 /api/attachment/<uuid> 与 GitHub 版 attachment:<uuid.ext>(带扩展名)。"""
    out = []
    pat = r"!?\[([^\]\n]*)\]\((?:/api/attachment/|attachment:)([0-9a-fA-F-]{8,}(?:\.[A-Za-z0-9]+)?)\)"
    for m in re.finditer(pat, text or ""):
        out.append((m.group(1) or m.group(2), m.group(2)))
    return out


def _download_attachment(cfg, name, key, directory):
    if backend_of(cfg) == "github":
        mod, token, repo, _ = gh_ctx(cfg)
        blob = mod.get_blob(token, repo, "attachments/%s" % key)
        if blob is None:
            raise ApiError("附件不存在: %s" % key)
        mani, _ = mod._load_json(token, repo, "attachments/manifest.json")
        name = ((mani or {}).get(key) or {}).get("fileName") or name or key
    else:
        server = server_of(cfg)
        req = urllib.request.Request(f"{server}/api/attachment/{key}")
        with urllib.request.urlopen(req, timeout=60) as resp:
            blob = resp.read()
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:120] or key
    path = os.path.join(directory, safe)
    with open(path, "wb") as f:
        f.write(blob)
    return path


def cmd_inbox_show(args):
    cfg = load_config()
    t = _get_thread(cfg, args.id)
    print(f"ID: {t.get('id')}  STATUS: {t.get('status')}")
    print(f"FROM: {t.get('asker')}  →  TO: {t.get('addressee')}"
          + ("  (已转人工待复核)" if t.get("escalated") else ""))
    if t.get("feedback"):
        print(f"FEEDBACK: {t.get('feedback')}" + (f" ({t.get('feedbackComment')})" if t.get("feedbackComment") else ""))
    print()
    print_thread_chat(t)
    all_text = "\n".join((m.get("content") or "") for m in (t.get("messages") or []))
    refs = _parse_attachment_refs(all_text)
    if refs:
        info(f"附件 {len(refs)} 个:")
        for name, key in refs:
            print(f"  - {name}  (key={key})")
        if getattr(args, "save_attachments", None):
            d = args.save_attachments
            os.makedirs(d, exist_ok=True)
            saved = [_download_attachment(cfg, n, k, d) for n, k in refs]
            info("已导出材料目录(可直接交给你的 AI agent 处理):")
            for p in saved:
                print("  " + p)
    return 0


def cmd_reply(args):
    cfg = load_config()
    content = " ".join(args.content).strip()
    if not content:
        err('用法: askme reply <id> "内容"')
        return 1
    content += attach_refs(cfg, args.img, args.file)
    r = _post_message(cfg, args.id, content)
    t = r.get("thread") or {}
    settled = r.get("settledKbId")
    dup = r.get("dupKbId")
    if settled:
        ok(f"已回复 #{args.id},回答自动沉淀进知识库(kb#{settled}),分身此后可代答同类问题。")
    elif dup:
        ok(f"已回复 #{args.id}。该问题已有知识条目 kb#{dup},本次不重复沉淀;"
           f'合并终版: askme kb edit {dup} -a "完整答案" --status ACTIVE'
           f"(待复核条目修正后需 --status ACTIVE 重新生效)")
    else:
        me = cfg.get("username")
        if t.get("addressee") == me:
            ok(f"已在 #{args.id} 续答(多轮对话不重复沉淀;终版可用 kb edit 更新 kb#{t.get('kbEntryId') or '-'})。")
        else:
            ok(f"已在 #{args.id} 追问,重新进入对方收件箱。")
    return 0


def cmd_feedback(args):
    cfg = load_config()
    helpful = args.verdict == "helpful"
    _post_feedback(cfg, args.id, helpful, args.comment or None)
    if helpful:
        ok(f"已反馈有帮助(#{args.id}),谢谢!")
    else:
        ok(f"已反馈无帮助(#{args.id});命中的知识条目已转待复核,对方会修正。")
    return 0


def cmd_escalate(args):
    cfg = load_config()
    _post_escalate(cfg, args.id)
    ok(f"已转人工,问题 #{args.id} 重新进入对方收件箱;命中条目已标记待复核。")
    return 0


def cmd_sent(args):
    cfg = load_config()
    items = _my_sent(cfg)
    if getattr(args, "limit", None):
        items = items[: args.limit]
    if not items:
        info("还没有提问记录。")
        return 0
    rows = [[str(t.get("id", "")), t.get("addressee", ""), t.get("status", ""),
             truncate(t.get("title"), 32), t.get("feedback") or "-"]
            for t in items]
    table(["ID", "TO", "STATUS", "TITLE", "FEEDBACK"], rows)
    unfed = [t for t in items
             if t.get("status") in ("RESOLVED", "AUTO_ANSWERED") and not t.get("feedback")]
    if unfed:
        print()
        info(f"⚠ {len(unfed)} 条已答未反馈(反馈是流程最后一步,帮对方校准知识):")
        for t in unfed[:5]:
            print(f"  askme feedback {t['id']} helpful|not   #{t['id']} "
                  f"{truncate(t.get('title'), 30)}")
    return 0


def _gh_kb_all(cfg):
    mod, token, repo, _ = gh_ctx(cfg)
    out = []
    for path, _, _ in mod.list_dir(token, repo, "kb"):
        e, _ = mod._load_json(token, repo, path)
        if e and not e.get("deleted"):
            e["id"] = os.path.splitext(os.path.basename(path))[0]
            out.append(e)
    return out


def _kb_list_items(cfg, status=None, limit=50):
    if backend_of(cfg) == "github":
        me = cfg.get("gh_user")
        items = [e for e in _gh_kb_all(cfg) if e.get("owner") == me
                 and (not status or e.get("status") == status)]
        items.sort(key=lambda e: int(e["id"]), reverse=True)
        return items[:limit]
    path = f"/api/kb?limit={limit}" + (f"&status={status}" if status else "")
    return api(cfg, "GET", path) or []


def _kb_search_items(cfg, q, owner=None, limit=10):
    if backend_of(cfg) == "github":
        mod = _gh()
        entries = _gh_kb_all(cfg)
        owner = owner or cfg.get("gh_user")
        return mod._weak_hits(entries, owner, q, limit)
    path = f"/api/kb/search?q={urllib.parse.quote(q)}&limit={limit}"
    if owner:
        path += f"&owner={urllib.parse.quote(owner)}"
    return api(cfg, "GET", path) or []


def _kb_get(cfg, kid):
    if backend_of(cfg) == "github":
        for e in _gh_kb_all(cfg):
            if str(e["id"]) == str(kid):
                return e
        raise ApiError("知识条目 kb#%s 不存在" % kid)
    return api(cfg, "GET", f"/api/kb/{kid}") or {}


def _kb_insert(cfg, body):
    if backend_of(cfg) == "github":
        mod, token, repo, me = gh_ctx(cfg)
        kid = mod._next_id(token, repo, "kb")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        e = {"id": str(kid), "owner": me, "question": body["question"],
             "questionAlts": body.get("questionAlts"), "answer": body.get("answer"),
             "tags": body.get("tags"), "source": "MANUAL", "sourceThreadId": None,
             "status": "ACTIVE", "hitCount": 0, "createdAt": now, "updatedAt": now}
        mod._save_json(token, repo, "kb/%s.json" % kid, e, "[kb] +%s %s" % (kid, e["question"][:40]))
        return e
    return api(cfg, "POST", "/api/kb", body) or {}


def _kb_update(cfg, kid, body):
    if backend_of(cfg) == "github":
        mod, token, repo, me = gh_ctx(cfg)
        e = _kb_get(cfg, kid)
        if e.get("owner") != me:
            raise ApiError("仅条目所有者可修改/删除")
        for k in ("question", "questionAlts", "answer", "tags", "status"):
            if body.get(k) is not None:
                e[k] = body[k]
        e["updatedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
        mod._save_json(token, repo, "kb/%s.json" % e["id"], e, "[kb] edit #%s" % e["id"])
        return e
    return api(cfg, "PUT", f"/api/kb/{kid}", body) or {}


def _kb_delete(cfg, kid):
    if backend_of(cfg) == "github":
        e = _kb_get(cfg, kid)
        return _kb_update(cfg, kid, {"status": e.get("status")}) if False else \
            _kb_update_soft_delete(cfg, kid)
    return api(cfg, "DELETE", f"/api/kb/{kid}")


def _kb_update_soft_delete(cfg, kid):
    """GitHub 模式软删:deleted 标记(保留 git 历史,文件不物理删)。"""
    mod, token, repo, me = gh_ctx(cfg)
    e = _kb_get(cfg, kid)
    if e.get("owner") != me:
        raise ApiError("仅条目所有者可修改/删除")
    e["deleted"] = True
    e["updatedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    mod._save_json(token, repo, "kb/%s.json" % e["id"], e, "[kb] rm #%s" % e["id"])
    return {"deleted": int(kid) if str(kid).isdigit() else kid}


def cmd_kb_push(args):
    cfg = load_config()
    answer = args.answer + attach_refs(cfg, args.img, args.file, biz_type="KB")
    body = {"question": args.question, "answer": answer, "tags": args.tags,
            "questionAlts": args.alts.replace("|", "\n") if args.alts else None}
    e = _kb_insert(cfg, body) or {}
    ok(f"已入库 kb#{e.get('id')}")
    return 0


def cmd_kb_list(args):
    cfg = load_config()
    items = _kb_list_items(cfg, status=args.status, limit=args.limit)
    if not items:
        info("知识库为空。")
        return 0
    rows = [[str(e.get("id", "")), e.get("status", ""), str(e.get("hitCount", 0)),
             truncate(e.get("question"), 42), e.get("tags") or ""]
            for e in items]
    table(["ID", "STATUS", "HITS", "QUESTION", "TAGS"], rows)
    return 0


def cmd_kb_search(args):
    cfg = load_config()
    items = _kb_search_items(cfg, args.q, owner=args.owner, limit=args.limit)
    if getattr(args, "json", False):
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0
    if not items:
        info("无命中。换几个关键词再试(检索无副作用,可多轮)。")
        return 0
    for e in items:
        print(f"kb#{e.get('id')} owner={e.get('owner')} hits={e.get('hitCount', 0)} "
              f"status={e.get('status')}")
        print(f"  Q: {truncate(e.get('question'), 60)}")
        print(f"  A: {truncate(e.get('answer'), 80)}")
        print()
    return 0


def cmd_kb_show(args):
    cfg = load_config()
    e = _kb_get(cfg, args.id)
    if getattr(args, "save", None):
        md = (f"---\nid: {e.get('id')}\nowner: {e.get('owner')}\ntags: {e.get('tags') or ''}\n"
              f"status: {e.get('status')}\nhit_count: {e.get('hitCount')}\n---\n\n"
              f"# {e.get('question')}\n\n{e.get('answer') or ''}\n")
        with open(args.save, "w", encoding="utf-8") as f:
            f.write(md)
        ok(f"已导出 {args.save}")
        return 0
    print(f"kb#{e.get('id')} status={e.get('status')} hits={e.get('hitCount')}")
    print(f"Q: {e.get('question')}")
    print(f"A: {e.get('answer')}")
    if e.get("questionAlts"):
        print(f"变体: {e.get('questionAlts')}")
    return 0


def cmd_kb_edit(args):
    cfg = load_config()
    body = {k: v for k, v in [("question", args.question), ("answer", args.answer),
                              ("tags", args.tags), ("status", args.status)] if v is not None}
    if not body:
        err("至少指定一个要改的字段(-q/-a/--tags/--status)")
        return 1
    _kb_update(cfg, args.id, body)
    ok(f"已更新 kb#{args.id}")
    return 0


def cmd_kb_rm(args):
    cfg = load_config()
    _kb_delete(cfg, args.id)
    ok(f"已删除 kb#{args.id}")
    return 0


# ── 入口 ──────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(prog="askme", description="askme 两人私享问答 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="登录(server 后端)或配置 GitHub 后端(--backend github)")
    s.add_argument("--user", help="server 后端: 用户名")
    s.add_argument("--password", help="server 后端: 密码")
    s.add_argument("--backend", choices=["server", "github"], default="server",
                   help="数据后端: server(自托管服务) / github(仓库即数据库,无需服务器)")
    s.add_argument("--gh-token", dest="gh_token", help="GitHub 后端: fine-grained PAT(Contents 读写)")
    s.add_argument("--gh-repo", dest="gh_repo", help="GitHub 后端: 数据仓库 <owner/repo>(私有)")
    s.add_argument("--server", help=f"server 后端地址,默认 {DEFAULT_SERVER}")
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("whoami", help="当前登录身份")
    s.set_defaults(fn=cmd_whoami)

    s = sub.add_parser("logout", help="清除本地凭据")
    s.set_defaults(fn=cmd_logout)

    s = sub.add_parser("upgrade", help="检测并升级 CLI 自身(从服务器拉最新 zip)")
    s.add_argument("--check", action="store_true", help="只检测并展示变更内容,不执行替换")
    s.set_defaults(fn=cmd_upgrade)

    s = sub.add_parser("ask", help="向对方提问(分身命中秒回,未命中进收件箱)")
    s.add_argument("to", metavar="username", help="对方用户名")
    s.add_argument("question", nargs="+")
    s.add_argument("--img", action="append", help="图片附件路径(可重复)")
    s.add_argument("--file", action="append", help="文本附件路径(可重复,context.md/日志等)")
    s.add_argument("--json", action="store_true", help="输出完整 JSON(供本地 AI agent 消费)")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("inbox", help="我的收件箱(待答问题)")
    inbox_sub = s.add_subparsers(dest="sub")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--notify", action="store_true",
                   help="定时任务模式: 仅新待答时桌面通知(无新则静默,适合 cron)")
    s.set_defaults(fn=cmd_inbox)
    s2 = inbox_sub.add_parser("show", help="看单条问题全文(多轮往来)")
    s2.add_argument("id", type=int)
    s2.add_argument("--save-attachments", dest="save_attachments", metavar="DIR",
                    help="导出附件到目录(材料目录可交给本地 AI agent 看图读日志)")
    s2.set_defaults(fn=cmd_inbox_show)

    s = sub.add_parser("reply", help="回复(被问方)/追问(提问方),身份自动判")
    s.add_argument("id", type=int)
    s.add_argument("content", nargs="+")
    s.add_argument("--img", action="append", help="图片附件路径(可重复)")
    s.add_argument("--file", action="append", help="文本附件路径(可重复)")
    s.set_defaults(fn=cmd_reply)

    s = sub.add_parser("sent", help="我提过的问题与状态")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_sent)

    s = sub.add_parser("feedback", help="反馈答案是否有帮助(流程最后一步,必做)")
    s.add_argument("id", type=int)
    s.add_argument("verdict", choices=["helpful", "not"])
    s.add_argument("--comment", help="补充说明(not 时帮对方定位问题)")
    s.set_defaults(fn=cmd_feedback)

    s = sub.add_parser("escalate", help="对分身代答不满意转人工")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_escalate)

    kb = sub.add_parser("kb", help="个人知识库管理")
    kb_sub = kb.add_subparsers(dest="sub")
    kb.set_defaults(fn=cmd_kb_list, limit=50, status=None)

    s = kb_sub.add_parser("push", help="新增一条知识")
    s.add_argument("-q", "--question", required=True)
    s.add_argument("-a", "--answer", required=True)
    s.add_argument("--tags")
    s.add_argument("--alts", help="变体问题,| 分隔")
    s.add_argument("--img", action="append", help="答案图片附件(可重复)")
    s.add_argument("--file", action="append", help="答案文本附件(可重复)")
    s.set_defaults(fn=cmd_kb_push)

    s = kb_sub.add_parser("list", help="我的知识库")
    s.add_argument("--status", choices=["ACTIVE", "NEEDS_REVIEW", "ARCHIVED"])
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_kb_list)

    s = kb_sub.add_parser("search", help="检索(--owner 检索对方)")
    s.add_argument("q")
    s.add_argument("--owner", help="检索对方的知识库(分身代答同款暴露面)")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--json", action="store_true", help="输出 JSON(供本地 AI agent 消费)")
    s.set_defaults(fn=cmd_kb_search)

    s = kb_sub.add_parser("show", help="查看单条全文/导出 md")
    s.add_argument("id", type=int)
    s.add_argument("--save", metavar="FILE", help="导出为 md 文件")
    s.set_defaults(fn=cmd_kb_show)

    s = kb_sub.add_parser("edit", help="编辑一条知识")
    s.add_argument("id", type=int)
    s.add_argument("-q", "--question")
    s.add_argument("-a", "--answer")
    s.add_argument("--tags")
    s.add_argument("--status", choices=["ACTIVE", "NEEDS_REVIEW", "ARCHIVED"])
    s.set_defaults(fn=cmd_kb_edit)

    s = kb_sub.add_parser("rm", help="删除一条知识")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_kb_rm)

    return p


def main(argv):
    if os.name == "nt":
        os.system("")  # 启用 Windows 终端 ANSI
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    try:
        rc = args.fn(args) or 0
    except ApiError as e:
        err(str(e))
        return 1
    if rc == 0:
        try:
            maybe_auto_upgrade(args)
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
