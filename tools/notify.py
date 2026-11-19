#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
告警投递 —— 把探活结果推到手机能收到的地方。

现状约束（2026-11）：
  * 平台侧没有短信网关，短信必须走阿里云/腾讯云等第三方服务商（需实名 + 签名报备）
  * 163 邮箱是最快能落地的告警通道：手机装网易邮箱大师/系统邮件 App 即可推送

用法（由 workflow 调用，也可本机手工发）：
  SMTP_USER=xxx@163.com SMTP_PASS=授权码 python3 tools/notify.py \
      --subject "标题" --body "正文"

  # 或从环境变量拼内容（workflow 用这种方式，避免命令行转义地狱）
  STATE=down REASONS="首页 HTTP 403" TS="2026-11-19 21:00:00" python3 tools/notify.py

凭据来源优先级：环境变量 > .env.alert（本机调试用，已在 .gitignore）
任何情况下都不把凭据写进仓库。
"""

import os
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = "https://order-quota.app.workbuddy.host/?sharecode=" + os.environ.get("APP_SHARE_CODE", "UUGXDCLHTG")


def load_env_file():
    """.env.alert 只用于本机调试，缺失属正常情况。"""
    path = os.path.join(ROOT, ".env.alert")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:  # noqa: BLE001
        pass


def build_message():
    """优先用命令行 + 环境变量；都没有时给一条通用的自检邮件。"""
    argv = sys.argv[1:]
    subject = body = ""
    i = 0
    while i < len(argv) - 1:
        if argv[i] == "--subject":
            subject = argv[i + 1]
            i += 2
        elif argv[i] == "--body":
            body = argv[i + 1]
            i += 2
        else:
            i += 1

    state = os.environ.get("STATE", "").strip()
    if state and not subject:
        icon = {"up": "[恢复]", "degraded": "[降级]", "down": "[故障]"}.get(state, "[告警]")
        ts = os.environ.get("TS", time.strftime("%Y-%m-%d %H:%M:%S"))
        subject = "%s 下单额度管理 · %s · %s" % (icon, state.upper(), ts)

    if not body and state:
        ts = os.environ.get("TS", "")
        reasons = os.environ.get("REASONS", "-")
        body = "\n".join([
            "站点状态：%s" % state.upper(),
            "检测时间：%s（北京时间）" % ts,
            "异常原因：%s" % reasons,
            "首页 HTTP：%s（%s ms）" % (os.environ.get("S_CODE", "-"), os.environ.get("S_MS", "-")),
            "数据平面 HTTP：%s" % os.environ.get("R_CODE", "-"),
            "",
            "线上地址：%s" % PAGE,
            "",
            "排查顺序：",
            "1. 浏览器打开上面的链接，看是不是「链接不完整」→ sharecode 失效，需要重新发布",
            "2. 本机跑 ./tools/healthcheck.sh 复核 DNS / 证书 / 静态页 / 数据平面",
            "3. 看 monitor/incidents.log 最近几行，确认故障窗口",
            "",
            "—— 由 uptime 探针自动发送",
        ])

    if not subject:
        subject = "下单额度管理 · 告警通道测试"
    if not body:
        body = "这是一封测试邮件，收到说明告警链路可用。\n\n线上地址：%s\n" % PAGE
    return subject, body


def main():
    load_env_file()

    host = os.environ.get("SMTP_HOST", "smtp.163.com").strip()
    port = int(os.environ.get("SMTP_PORT", "465").strip() or 465)
    user = os.environ.get("SMTP_USER", "").strip()
    pwd = os.environ.get("SMTP_PASS", "").strip()
    to = [x.strip() for x in os.environ.get("ALERT_TO", "tym_maile@163.com").split(",") if x.strip()]

    if not user or not pwd:
        # 未配置凭据时明确跳过，绝不让 workflow 变红
        print("SKIP 邮件告警：SMTP_USER / SMTP_PASS 未配置")
        print("     配置方式见 README「告警通道」一节")
        return 0

    subject, body = build_message()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("下单额度管理 · 探针", user))
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg["X-Priority"] = "1" if os.environ.get("STATE") == "down" else "3"
    msg.set_content(body)

    last_err = None
    for attempt in range(1, 4):
        try:
            ctx = ssl.create_default_context()
            if port == 465:
                with smtplib.SMTP_SSL(host, port, timeout=25, context=ctx) as s:
                    s.login(user, pwd)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=25) as s:
                    s.starttls(context=ctx)
                    s.login(user, pwd)
                    s.send_message(msg)
            print("OK 已发送 -> %s | %s" % (", ".join(to), subject))
            return 0
        except Exception as e:  # noqa: BLE001
            last_err = e
            print("第 %d 次发送失败：%s: %s" % (attempt, type(e).__name__, str(e)[:200]))
            time.sleep(3)

    print("FAIL 邮件告警三次均失败：%s" % last_err)
    # 退出 0：告警失败不应把 workflow 标红，否则失败通知本身会刷屏
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
