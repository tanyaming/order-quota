#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
线上探活探针 —— 由 GitHub Actions 每 5 分钟跑一次。

为什么需要它：容器挂掉时页面里的 JavaScript 根本不会执行，客户端埋点必然缺失，
这类"任何人都打不开"的故障只能靠外部探针记录。

设计取舍：
  * 正常时只写 job summary，不产生任何提交（避免 288 次/天污染仓库历史）
  * 只在状态发生变化时写 monitor/incidents.log 与 monitor/state.json，由 workflow 提交
  * 探针从 GitHub 的机房出网，与国内用户网络互为参照，可区分"全网挂"与"局部线路问题"
  * 只依赖标准库，无需安装任何东西
"""

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://order-quota.app.workbuddy.host"
HOST = "order-quota.app.workbuddy.host"
RPC = BASE + "/.cloud/database/rest/rpc/fn_api"
SDK = "https://cdn.jsdelivr.net/npm/@tencent-ai/workbuddy-cloud-sdk/lib/index.global.js"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MON = os.path.join(ROOT, "monitor")
TZ = timezone(timedelta(hours=8))


def now():
    return datetime.now(TZ)


def http(url, data=None, headers=None, timeout=20, read=400):
    """返回 (状态码, 耗时毫秒, 响应体片段)。状态码 0 表示网络层失败。"""
    req = urllib.request.Request(url, data=data, headers=headers or {})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read() if not read else r.read(read)
            body = raw.decode("utf-8", "replace")
            return r.status, int((time.time() - t0) * 1000), body
    except urllib.error.HTTPError as e:
        try:
            body = (e.read() if not read else e.read(read)).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, int((time.time() - t0) * 1000), body
    except Exception as e:  # noqa: BLE001 网络层任何异常都算不可达
        return 0, int((time.time() - t0) * 1000), "ERR %s: %s" % (type(e).__name__, str(e)[:120])


def publishable_key():
    """从仓库里的 index.html 现读，避免另存密钥。"""
    try:
        with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
            m = re.search(r'publishableKey:\s*"(wbpk_[^"]+)"', f.read())
            return m.group(1) if m else ""
    except Exception:  # noqa: BLE001
        return ""


def dns_ok():
    try:
        socket.getaddrinfo(HOST, 443)
        return True
    except Exception:  # noqa: BLE001
        return False


def main():
    ts = now()
    dns = dns_ok()

    s_code, s_ms, s_body = http(BASE + "/", read=None)
    static_bytes = len(s_body.encode("utf-8"))

    key = publishable_key()
    r_code, r_ms, r_body = 0, 0, ""
    if key:
        r_code, r_ms, r_body = http(
            RPC,
            data=json.dumps({"p_token": "__probe__", "p_action": "accounts", "p_payload": {}}).encode(),
            headers={"Content-Type": "application/json", "x-wb-webapp-access-key": key},
        )
    sdk_code, sdk_ms, _ = http(SDK)

    rpc_ok = r_code == 200 and "AUTH" in r_body
    if not dns or s_code != 200:
        state = "down"
    elif not rpc_ok:
        state = "degraded"
    else:
        state = "up"

    reasons = []
    if not dns:
        reasons.append("DNS 解析失败")
    if s_code != 200:
        reasons.append("首页 HTTP %s（%dms）" % (s_code, s_ms))
    if key and not rpc_ok:
        reasons.append("数据平面 HTTP %s %s" % (r_code, r_body[:120].replace("\n", " ")))
    if not key:
        reasons.append("未能从 index.html 解析出 publishableKey")

    # 与上次状态比对
    os.makedirs(MON, exist_ok=True)
    state_path = os.path.join(MON, "state.json")
    prev = {}
    try:
        with open(state_path, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:  # noqa: BLE001
        prev = {}

    changed = prev.get("state") != state
    event = ""
    if changed:
        if not prev.get("state"):
            event = "first_run"
        elif state == "up":
            event = "recovered"
        else:
            event = "state_%s_to_%s" % (prev.get("state"), state)

    summary = [
        "# 探活 %s (UTC+8)" % ts.strftime("%Y-%m-%d %H:%M:%S"),
        "",
        "| 项目 | 结果 |",
        "|---|---|",
        "| 总体状态 | **%s** |" % state.upper(),
        "| DNS | %s |" % ("OK" if dns else "FAIL"),
        "| 首页 | HTTP %s · %dms · %dB |" % (s_code, s_ms, static_bytes),
        "| 数据平面 | HTTP %s · %dms |" % (r_code, r_ms),
        "| SDK(jsdelivr) | HTTP %s · %dms |" % (sdk_code, sdk_ms),
        "",
    ]
    if reasons:
        summary.append("**异常原因**：" + "；".join(reasons))
    if changed:
        summary.append("")
        summary.append("状态变化：`%s` → `%s`（事件 %s）" % (prev.get("state", "none"), state, event))
    with open(os.path.join(MON, "last_summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(summary) + "\n")

    if changed:
        line = "\t".join([
            ts.strftime("%Y-%m-%d %H:%M:%S"),
            prev.get("state", "none"),
            state,
            event,
            str(s_code),
            str(s_ms),
            str(r_code),
            "; ".join(reasons).replace("\t", " ") or "-",
        ])
        with open(os.path.join(MON, "incidents.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({
                "state": state,
                "since": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "last_check": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "reasons": reasons,
            }, f, ensure_ascii=False, indent=2)
    else:
        try:
            prev["last_check"] = ts.strftime("%Y-%m-%d %H:%M:%S")
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(prev, f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass

    out = os.environ.get("GITHUB_OUTPUT")
    kv = {
        "state": state,
        "changed": "true" if changed else "false",
        "event": event,
        "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "reasons": "; ".join(reasons) or "-",
        "static_code": str(s_code),
        "static_ms": str(s_ms),
        "rpc_code": str(r_code),
    }
    if out:
        with open(out, "a", encoding="utf-8") as f:
            for k, v in kv.items():
                f.write("%s=%s\n" % (k, str(v).replace("\n", " ")))

    print("\n".join(summary))
    print("state=%s changed=%s event=%s" % (state, changed, event))
    # 永远以 0 退出：探活失败不应让 workflow 标红（状态通过 issue 告警），
    # 否则 GitHub 会在失败通知里刷屏。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
