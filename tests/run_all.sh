#!/usr/bin/env bash
# 把三个测试按「不需要人 -> 需要人」的顺序跑一遍。
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
VPY=".venv/bin/python"
[ -x "$VPY" ] || VPY="$(command -v python3)"

echo "### 1/3 判定逻辑（打桩引擎，不需要人）"
"$VPY" tests/logic_test.py; a=$?

echo; echo "### 2/3 机制自测（含摄像头真人分布，需要镜头前有人）"
"$VPY" tests/selftest.py --face; b=$?

echo; echo "### 4/4 GUI（需要图形会话）"
"$VPY" tests/gui_test.py; d=$?

echo; echo "### 3/3 端到端流程（需要镜头前有人；用假 xdotool 打桩，不必真装）"
"$VPY" tests/flow_test.py; c=$?

echo; echo "=================================================="
printf 'logic=%s  selftest=%s  flow=%s  gui=%s  (0=通过)\n' "$a" "$b" "$c" "${d:-0}"
[ $((a+b+c+${d:-0})) -eq 0 ] && echo "全部通过" || echo "有未通过项，见上面各段输出"
echo "=================================================="
exit $((a+b+c+${d:-0}))
