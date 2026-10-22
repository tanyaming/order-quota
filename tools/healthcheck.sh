#!/usr/bin/env bash
# 下单额度管理 - 线上健康体检 / 故障取证
#
#   ./tools/healthcheck.sh              单次体检，异常时退出码非 0
#   ./tools/healthcheck.sh -w 60        每 60 秒巡检一次，异常时自动落盘
#   ./tools/healthcheck.sh --quiet      只在异常时输出
#
# 结果同时追加到 logs/healthcheck-YYYYMMDD.log，出问题时把这个文件发出来即可定位。

set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INDEX="$DIR/index.html"
HOSTNAME_APP="order-quota.app.workbuddy.host"
BASE="https://$HOSTNAME_APP"
SDK_URL="https://cdn.jsdelivr.net/npm/@tencent-ai/workbuddy-cloud-sdk@dev/lib/index.global.js"
LOGDIR="$DIR/logs"
LOG="$LOGDIR/healthcheck-$(date +%Y%m%d).log"

QUIET=0
WATCH=0
while [ $# -gt 0 ]; do
  case "$1" in
    -w|--watch) WATCH="${2:-60}"; shift 2 ;;
    --quiet)    QUIET=1; shift ;;
    -h|--help)  sed -n '2,10p' "$0"; exit 0 ;;
    *) shift ;;
  esac
done

mkdir -p "$LOGDIR"

# 密钥从 index.html 现读，脚本内不落明文，可安全提交到仓库
KEY="$(sed -n 's/.*publishableKey: *"\(wbpk_[^"]*\)".*/\1/p' "$INDEX" | head -1)"
FAIL=0
WARN=0

say()  { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; printf '%s\n' "$*" >>"$LOG"; }
pass() { say "  [PASS] $*"; }
bad()  { FAIL=$((FAIL+1)); say "  [FAIL] $*"; }
warn() { WARN=$((WARN+1)); say "  [WARN] $*"; }

section() { say ""; say "== $* =="; }

# ---------- 1. DNS 多线路一致性 ----------
check_dns() {
  section "DNS 解析（多线路一致性）"
  local ns ip uniq=""
  OKD=0
  for ns in 223.5.5.5 119.29.29.29 114.114.114.114 180.76.76.76 1.1.1.1; do
    ip="$(dig +short +time=3 +tries=1 @"$ns" "$HOSTNAME_APP" A 2>/dev/null | grep -E '^[0-9]+\.' | tail -1)"
    if [ -z "$ip" ]; then
      say "         @$ns -> 超时"
      continue
    fi
    OKD=$((OKD + 1))
    case " $uniq " in *" $ip "*) ;; *) uniq="$uniq $ip" ;; esac
    say "         @$ns -> $ip"
  done
  local cnt
  cnt=$(printf '%s\n' $uniq | grep -c .)
  if [ "$OKD" -eq 0 ]; then
    bad "全部 DNS 线路无响应 ← 本地网络或 DNS 故障"
  elif [ "$cnt" -gt 1 ]; then
    bad "解析出多个不同 IP，疑似污染或分线路:$uniq"
  elif [ "$OKD" -lt 3 ]; then
    warn "仅 ${OKD} 条线路有响应，其余超时，解析为 $uniq"
  else
    pass "${OKD} 条线路解析一致：$uniq"
  fi
}

# ---------- 2. TLS 证书 ----------
check_tls() {
  section "TLS 证书"
  local end
  end="$(echo | openssl s_client -servername "$HOSTNAME_APP" -connect "$HOSTNAME_APP:443" 2>/dev/null \
        | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)"
  if [ -z "$end" ]; then
    bad "无法读取 TLS 证书（443 不通或证书异常）"
    return
  fi
  local endsec nowsec days
  endsec=$(date -j -f "%b %d %H:%M:%S %Y %Z" "$end" +%s 2>/dev/null || echo 0)
  nowsec=$(date +%s)
  if [ "$endsec" -gt 0 ]; then
    days=$(( (endsec - nowsec) / 86400 ))
    if [ "$days" -lt 0 ]; then
      bad "证书已过期（${end}）"
    elif [ "$days" -lt 14 ]; then
      warn "证书将在 ${days} 天后过期（${end}）"
    else
      pass "证书有效，剩余 ${days} 天（${end}）"
    fi
  else
    pass "证书到期时间 $end"
  fi
}

# ---------- 3. 静态页 ----------
check_static() {
  section "静态页 https://$HOSTNAME_APP/"
  local body="/tmp/hc_body.$$"
  local out
  out="$(curl -s -o "$body" -w "%{http_code} %{time_total} %{size_download}" --max-time 25 "$BASE/" 2>/dev/null)"
  local code t size
  code=$(printf '%s' "$out" | awk '{print $1}')
  t=$(printf '%s' "$out" | awk '{print $2}')
  size=$(printf '%s' "$out" | awk '{print $3}')
  if [ "$code" != "200" ]; then
    bad "HTTP ${code}（耗时 ${t}s）← 页面打不开就是这个"
    rm -f "$body"; return
  fi
  local r l
  r=$(md5 -q "$body" 2>/dev/null)
  l=$(md5 -q "$INDEX" 2>/dev/null)
  if [ "$r" = "$l" ]; then
    pass "HTTP 200，${t}s，${size}B，内容与本地一致"
  else
    warn "HTTP 200，${t}s，但线上内容与本地不同（线上 ${r:0:8} / 本地 ${l:0:8}）← 可能被回滚或未重新发布"
  fi
  rm -f "$body"
}

# ---------- 4. 数据平面 ----------
check_api() {
  section "数据平面 /.cloud/database/rest/rpc/fn_api"
  if [ -z "$KEY" ]; then
    bad "未能从 index.html 解析出 publishableKey"
    return
  fi
  local body code
  body="$(curl -s -w '\n%{http_code}' --max-time 25 -X POST "$BASE/.cloud/database/rest/rpc/fn_api" \
        -H "x-wb-webapp-access-key: $KEY" -H "Content-Type: application/json" \
        -d '{"p_token":"__healthcheck__","p_action":"accounts","p_payload":{}}' 2>/dev/null)"
  code=$(printf '%s' "$body" | tail -1)
  local payload
  payload=$(printf '%s' "$body" | sed '$d' | head -c 160)
  case "$code" in
    200)
      case "$payload" in
        *'"code": "AUTH"'*|*'"code":"AUTH"'*)
          pass "HTTP 200，鉴权层正常（预期返回 AUTH）" ;;
        *) warn "HTTP 200，但返回不是预期的 AUTH：$payload" ;;
      esac ;;
    401) bad "HTTP 401 ← 网关鉴权失败，publishableKey 可能已失效" ;;
    5*)  bad "HTTP $code ← 后端故障，这就是打不开的原因：$payload" ;;
    *)   bad "HTTP ${code}：$payload" ;;
  esac
}

# ---------- 5. 前端 SDK 单点 ----------
check_sdk() {
  section "前端 SDK（页面能跑起来的前提）"
  local out code t size
  out="$(curl -s -o /dev/null -w "%{http_code} %{time_total} %{size_download}" --max-time 20 "$SDK_URL" 2>/dev/null)"
  code=$(printf '%s' "$out" | awk '{print $1}')
  t=$(printf '%s' "$out" | awk '{print $2}')
  size=$(printf '%s' "$out" | awk '{print $3}')
  if [ "$code" = "200" ] && [ "${size:-0}" -gt 10000 ]; then
    if [ "$(printf '%s' "$t" | cut -d. -f1)" -ge 5 ]; then
      warn "SDK 可加载但极慢（${t}s）← 国内访问 jsdelivr 抖动时会白屏"
    else
      pass "SDK 可加载，${t}s，${size}B"
    fi
  else
    bad "SDK 加载失败 HTTP $code / ${size}B ← 此时页面会停在「初始化失败」，用户表现为打不开"
  fi
}

# ---------- 6. 并发 ----------
check_concurrency() {
  section "并发 10 路（模拟客户集中下单）"
  local res
  res="$(seq 10 | xargs -P 10 -I{} curl -s -o /dev/null -w "%{http_code}\n" --max-time 25 "$BASE/" 2>/dev/null)"
  local c200 other
  c200=$(printf '%s\n' "$res" | grep -c '^200$')
  other=$(printf '%s\n' "$res" | grep -vc '^200$')
  if [ "$other" -eq 0 ]; then
    pass "10/10 全部 200"
  else
    bad "$other/10 请求失败（非 200），并发下会挂"
  fi
}

# ---------- 主流程 ----------
run_once() {
  say ""
  say "############################################################"
  say "# 体检时间 $(date '+%Y-%m-%d %H:%M:%S %Z')"
  say "############################################################"
  check_dns
  check_tls
  check_static
  check_api
  check_sdk
  check_concurrency
  say ""
  if [ "$FAIL" -eq 0 ]; then
    say "结论：正常（警告 $WARN 项）"
  else
    say "结论：异常 $FAIL 项，警告 $WARN 项 —— 日志已写入 $LOG"
  fi
  return "$FAIL"
}

if [ "$WATCH" -gt 0 ]; then
  say "巡检模式：每 ${WATCH}s 一轮，Ctrl+C 结束。日志 $LOG"
  while true; do
    FAIL=0; WARN=0
    run_once
    sleep "$WATCH"
  done
else
  run_once
  exit $?
fi
