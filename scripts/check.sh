#!/usr/bin/env bash
#
# 一键验收脚本（PLAN.md §1.3）。
#
#   1. Python 核心单测（含端到端与"零网络/零依赖"守护）
#   2. TS 适配层的 tsc 类型检查 + 对真实 defineTool 的契约测试
#   3. 幂等验收：同一份输入连跑两次，report 与 ledger 逐字节一致
#
# 用法：bash scripts/check.sh
# 退出码：0 全绿；非 0 表示有环节失败。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then PYTHON="$candidate"; break; fi
  done
fi
if [[ -z "$PYTHON" ]]; then
  echo "找不到 python3.11+ 解释器；核心只依赖标准库，装一个即可" >&2
  exit 1
fi

FAILED=0
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
ok()   { printf '   \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '   \033[31m✗\033[0m %s\n' "$1"; FAILED=1; }

step "1/6 Python 核心单测（${PYTHON}）"
if "$PYTHON" -m unittest discover -s tests -t . 2>&1 | tail -4; then
  ok "Python 单测通过"
else
  bad "Python 单测失败"
fi

step "2/6 TS 适配层（tsc + 契约测试）"
if [[ -d plugin/vendor/@deepseek-ai/dsh-tools ]]; then
  if (cd plugin && npm run check >/tmp/dl-ts-check.log 2>&1); then
    ok "TS 层通过（$(grep -c '^ok' /tmp/dl-ts-check.log 2>/dev/null || echo '?') 个测试）"
  else
    bad "TS 层失败，日志：/tmp/dl-ts-check.log"
    tail -20 /tmp/dl-ts-check.log
  fi
else
  printf '   \033[33m!\033[0m 跳过：先跑 node scripts/extract-dsh-packages.mjs 抽出宿主包\n'
fi

step "3/6 客户端面板产物（语法 + 注册行为）"
if [[ -f plugin/client/client.js ]]; then
  if node --check plugin/client/client.js 2>/dev/null; then
    ok "client.js 语法通过（$(wc -c < plugin/client/client.js | tr -d ' ') 字节）"
  else
    bad "client.js 语法错误"
  fi
  if node scripts/verify-client.mjs 2>&1 | tail -3; then
    ok "客户端注册行为正确（shell.overlay / inject=slots）"
  else
    bad "客户端注册校验失败"
  fi
else
  bad "缺少 plugin/client/client.js"
fi

step "4/6 写入链路（点「入账」能不能真写进文件）"
if node scripts/smoke-write.mjs >/tmp/dl-write.log 2>&1; then
  ok "写入链路通过（文件已改 / 可删除 / 可清空 / 走 subprocess）"
else
  bad "写入链路失败，日志：/tmp/dl-write.log"
  tail -15 /tmp/dl-write.log
fi

step "5/6 阶段三铁律（待收绝不进历史累计）"
if python3.11 -m unittest tests.test_acceptance.UpcomingIsolationTest >/tmp/dl-upcoming.log 2>&1; then
  ok "待收独立成区块，历史累计未受污染"
else
  bad "待收污染了历史累计！日志：/tmp/dl-upcoming.log"
  tail -12 /tmp/dl-upcoming.log
fi

step "6/6 幂等验收：连跑两次逐字节一致"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cp -R data "$WORK/data"
rm -rf "$WORK/data/out" "$WORK/data/events.json"

if "$PYTHON" -m dividend_ledger --data "$WORK/data" sync --from fixture >/dev/null 2>&1 \
  && "$PYTHON" -m dividend_ledger --data "$WORK/data" report \
       --now 2026-10-10T12:00:00+08:00 >/dev/null 2>&1; then
  cp "$WORK/data/out/report.json" "$WORK/report.1.json"
  cp "$WORK/data/out/ledger.json" "$WORK/ledger.1.json"

  "$PYTHON" -m dividend_ledger --data "$WORK/data" report \
    --now 2026-10-10T12:00:00+08:00 >/dev/null 2>&1

  if diff -q "$WORK/report.1.json" "$WORK/data/out/report.json" >/dev/null \
    && diff -q "$WORK/ledger.1.json" "$WORK/data/out/ledger.json" >/dev/null; then
    ok "report.json / ledger.json 逐字节一致"
  else
    bad "两次输出不一致"
  fi

  TOTALS="$("$PYTHON" -c "
import json,sys
r=json.load(open('$WORK/data/out/report.json',encoding='utf-8'))
t=r['totals']
print(f\"税前分红合计 {t['cash_amount']} / 成本 {t['cost_basis']} / 事件 {t['event_count']} 笔 / {t['stock_count']} 只\")
")"
  ok "$TOTALS"
else
  bad "生成报告失败"
fi

printf '\n'
if [[ "$FAILED" -eq 0 ]]; then
  printf '\033[32m全部验收通过\033[0m\n'
else
  printf '\033[31m有环节失败\033[0m\n'
fi
exit "$FAILED"
