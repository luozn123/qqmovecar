#!/usr/bin/env python3
"""
挪车提醒服务 (VPS 单文件, 仅用 Python 标准库, 无需 Flask)

流程:
  路人扫车上二维码 -> 打开 https://你的域名/ -> 点"通知车主挪车"
  -> 本服务用 QQ 开放平台机器人 API, 以机器人身份给车主发 C2C 私信
  -> 路人全程看不到车主 QQ 号 / openid, 二维码里也只有一个网址

运行前必须用环境变量 (不要写进代码, 不要提交到仓库):
  QQ_APPID        机器人 AppID
  QQ_SECRET       机器人密钥 (与旧机器人复用同一套凭据即可)
  OWNER_OPENID    车主在这个机器人下的 openid
                  (已知的: 你现有机器人白名单里的那一个, 换机器人要重新从入站消息里抄)
  PORT            默认 8000, 建议前面挂 Nginx/Caddy 做 HTTPS, 不要直接把 8000 暴露出去

关键限制 (2026-10-31 更正, 上线前必测, 不要假设):
  上一版这里写"每月 4 条"是不准确的, 那是旧文档里的规则。
  最新文档顶部的提示是: 主动推送能力于 2025 年 4 月 21 日起不再提供,
  接口调用时会收到错误信息 (公告原文见 2025-04-16《关于QQ机器人消息推送策略调整通知》)。
  也就是说, 对新做的挪车服务, QQ 机器人主动私信不能当作主通道,
  只能当作"实测一下, 通了算赚到"的备选。
  先跑一次 POST /notify 实测, 返回 200 才算通; 若报错,
  主通道必须换成 ntfy / Bark / Server酱 / 短信等 VPS 可直接推手机的通道,
  扫码页本身不用变, 只换 send_qq_to_owner() 的后端。

防刷 (额度宝贵, 必须有):
  - 全局冷却: 上一次成功推送后 GLOBAL_COOLDOWN 秒内不再真的发 QQ, 只回"已通知"
  - 单 IP 冷却: 同一 IP IP_COOLDOWN 秒内只发一次
  - 留言限长 200 字, 仅做展示, 不执行、不存手机号等隐私
"""
import hashlib
import html
import json
import os
import secrets
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APPID = os.environ.get("QQ_APPID", "")
SECRET = os.environ.get("QQ_SECRET", "")
OWNER_OPENID = os.environ.get("OWNER_OPENID", "")
PORT = int(os.environ.get("PORT", "8000"))
# 有域名 + 反代时保持 127.0.0.1; 无域名走 IP 直连时设 HOST=0.0.0.0, 并用非标端口 (如 8080, 避开 80)
HOST = os.environ.get("HOST", "127.0.0.1")
GLOBAL_COOLDOWN = int(os.environ.get("GLOBAL_COOLDOWN", "300"))   # 全局 5 分钟
IP_COOLDOWN = int(os.environ.get("IP_COOLDOWN", "600"))           # 单 IP 10 分钟
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "movecar.log")

_token_cache = {"token": "", "expires_at": 0}
_last_success_at = 0.0
_last_by_ip: dict[str, float] = {}
# Button-only gate: each GET / issues a single-use token embedded in the
# form; POST /notify without a valid unused token never sends QQ. This
# stops direct curl POSTs to /notify and replayed/refresh-resubmitted POSTs.
_tokens: dict[str, float] = {}
TOKEN_TTL = 1800  # 30 minutes, long enough for a passer-by to read and tap


def _new_token() -> str:
    now = time.time()
    for t, exp in list(_tokens.items()):
        if exp <= now:
            del _tokens[t]
    tok = secrets.token_urlsafe(24)
    _tokens[tok] = now + TOKEN_TTL
    return tok


def _consume_token(tok: str) -> bool:
    if not tok:
        return False
    exp = _tokens.pop(tok, None)
    return exp is not None and exp > time.time()


def log(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print(line, end="")


def get_access_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    body = json.dumps({"appId": APPID, "clientSecret": SECRET}).encode()
    req = urllib.request.Request(
        "https://bots.qq.com/app/getAppAccessToken",
        data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    token = data.get("access_token", "")
    expires = int(data.get("expires_in", 7200))
    _token_cache.update({"token": token, "expires_at": now + expires})
    return token


def send_qq_to_owner(note: str) -> tuple[bool, str]:
    """主动 C2C 消息: 不带 event_id / msg_id, 带了反而可能 400。"""
    token = get_access_token()
    content = "🚗 挪车提醒：有人扫了你车上的挪车码，请尽快挪车。"
    if note:
        content += f"\n留言：{note}"
    body = json.dumps({
        "msg_type": 0,
        "content": content,
        "msg_seq": int(time.time()) % 100000,
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"https://api.sgroup.qq.com/v2/users/{OWNER_OPENID}/messages",
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"QQBot {token}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = resp.read().decode()
        log(f"QQ OK {result[:200]}")
        return True, result
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:500]
        log(f"QQ FAIL {e.code} {detail}")
        return False, f"{e.code} {detail}"


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>请挪一下车</title>
<style>*{box-sizing:border-box}body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:#fafafa;color:#111}.wrap{max-width:420px;margin:0 auto;padding:72px 24px 40px;text-align:center}h1{font-size:30px;font-weight:650;letter-spacing:.02em;margin:0 0 12px}.sub{color:#777;font-size:15px;margin:0 0 48px}textarea{width:100%;height:44px;padding:10px 2px;font-size:16px;border:0;border-bottom:1px solid #ddd;background:transparent;outline:none;resize:none;text-align:center}textarea::placeholder{color:#bbb}button{width:100%;margin-top:40px;padding:17px;font-size:17px;font-weight:600;color:#fff;background:#111;border:0;border-radius:999px;cursor:pointer}.msg{margin-top:28px;color:#555;font-size:15px;line-height:1.8}</style></head><body>
<div class="wrap">
<h1>请挪一下车</h1>
<p class="sub">点一下，车主会收到提醒。</p>
<form method="post" action="/notify">
<input type="hidden" name="token" value="__TOKEN__">
<textarea name="note" maxlength="200" placeholder="留言（可选）"></textarea>
<button type="submit">通知车主</button></form>
__MSG__
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, code: int = 200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _render(self, msg: str = ""):
        page = PAGE.replace("__TOKEN__", _new_token())
        page = page.replace("__MSG__", f'<div class="msg">{msg}</div>' if msg else "")
        return page

    def _redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/notify":
            # Opening /notify directly never sends: send the visitor to the
            # form page, where a fresh single-use token is issued.
            self._redirect("/"); return
        if parsed.path in ("/", "/index.html"):
            done = parse_qs(parsed.query).get("done", [""])[0]
            msgs = {
                "ok": "✅ 已通知车主, 谢谢。",
                "cooldown": "已通知车主, 请稍等, 不要重复点击。",
                "ip": "您刚才已通知过, 请稍等。",
                "fail": "发送失败, 请稍后再试一次。",
                "token": "请从首页点按钮发送。",
            }
            self._send(self._render(msgs.get(done, ""))); return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        global _last_success_at
        if self.path != "/notify":
            self.send_response(404); self.end_headers(); return
        if not APPID or not SECRET or not OWNER_OPENID:
            self._send(self._render("服务未配置好, 请联系车主。"), 500); return
        length = int(self.headers.get("Content-Length", 0) or 0)
        fields = parse_qs(self.rfile.read(length).decode("utf-8", "ignore"))
        note = (fields.get("note", [""])[0] or "").strip()[:200]
        ip = self.client_address[0]
        ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:12]
        now = time.time()

        # Button-only gate: no valid single-use token = no QQ send, ever.
        # Direct curl POSTs, bookmarked /notify pages and replayed POSTs
        # all land here.
        if not _consume_token((fields.get("token", [""])[0] or "")):
            log(f"reject-notoken ip_hash={ip_hash} note={note!r}")
            self._redirect("/?done=token"); return

        # 防刷: 先查冷却, 冷却期内不消耗 QQ 额度, 但给路人一个"已通知"的反馈
        if now - _last_success_at < GLOBAL_COOLDOWN:
            log(f"cooldown-global ip_hash={ip_hash} note={note!r}")
            self._redirect("/?done=cooldown"); return
        if now - _last_by_ip.get(ip_hash, 0) < IP_COOLDOWN:
            log(f"cooldown-ip ip_hash={ip_hash}")
            self._redirect("/?done=ip"); return

        ok, detail = send_qq_to_owner(note)
        if ok:
            _last_success_at = now
            _last_by_ip[ip_hash] = now
            # POST-Redirect-GET: result page lives at /, so refreshing it
            # cannot resubmit the POST.
            self._redirect("/?done=ok")
        else:
            # QQ 失败不记入冷却, 让路人可以换个方式 (如过一会儿再试)
            self._redirect("/?done=fail")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"movecar listening on {HOST}:{PORT} (有域名时请用 Nginx/Caddy 反代 + HTTPS; 无域名时用 http://公网IP:{PORT}/ 直连)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
