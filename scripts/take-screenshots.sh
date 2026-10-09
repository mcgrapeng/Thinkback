#!/bin/bash
# 批量截图脚本 — 用 Chrome headless 拍所有页面
set -e

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
OUT="/Users/zhangpeng/workspace/liaohe/Thinkback/assets/screenshots"
BASE="http://localhost:7001"

# 高质量截图选项
COMMON_OPTS=(
  --headless=new
  --disable-gpu
  --no-sandbox
  --hide-scrollbars
  --window-size=1440,900
  --virtual-time-budget=4000
)

# 截图函数: $1=URL, $2=输出文件名, $3=可选等待毫秒
shoot() {
  local url="$1"
  local name="$2"
  local wait="${3:-2500}"
  echo "→ $name"
  $CHROME "${COMMON_OPTS[@]}" \
    --screenshot="$OUT/$name" \
    --virtual-time-budget=$wait \
    "$url" 2>/dev/null
  echo "  ✓ $OUT/$name"
}

# 1. Overview (hero)
shoot "$BASE/" "overview-hero.png" 3500

# 2. Overview 滚动到 System Pulse
shoot "$BASE/" "overview-pulse.png" 3500
# Chrome 默认截 viewport,full page 用 --screenshot-with-format? 改用更大 viewport
$CHROME "${COMMON_OPTS[@]}" \
  --window-size=1440,2200 \
  --screenshot="$OUT/overview-full.png" \
  --virtual-time-budget=4000 \
  "$BASE/" 2>/dev/null
echo "  ✓ $OUT/overview-full.png"

# 3. 记忆浏览器
shoot "$BASE/memories" "memories-list.png" 3500
$CHROME "${COMMON_OPTS[@]}" \
  --window-size=1440,1600 \
  --screenshot="$OUT/memories-full.png" \
  --virtual-time-budget=4000 \
  "$BASE/memories" 2>/dev/null
echo "  ✓ $OUT/memories-full.png"

# 4. 任务监控
shoot "$BASE/tasks" "tasks-list.png" 3000
$CHROME "${COMMON_OPTS[@]}" \
  --window-size=1440,1600 \
  --screenshot="$OUT/tasks-full.png" \
  --virtual-time-budget=3500 \
  "$BASE/tasks" 2>/dev/null
echo "  ✓ $OUT/tasks-full.png"

# 5. 治理
shoot "$BASE/govern" "govern.png" 2500
$CHROME "${COMMON_OPTS[@]}" \
  --window-size=1440,1600 \
  --screenshot="$OUT/govern-full.png" \
  --virtual-time-budget=3000 \
  "$BASE/govern" 2>/dev/null
echo "  ✓ $OUT/govern-full.png"

# 6. 审计
shoot "$BASE/audit" "audit.png" 2500
$CHROME "${COMMON_OPTS[@]}" \
  --window-size=1440,1400 \
  --screenshot="$OUT/audit-full.png" \
  --virtual-time-budget=3000 \
  "$BASE/audit" 2>/dev/null
echo "  ✓ $OUT/audit-full.png"

# 7. 配置
shoot "$BASE/config" "config.png" 2500
$CHROME "${COMMON_OPTS[@]}" \
  --window-size=1440,1600 \
  --screenshot="$OUT/config-full.png" \
  --virtual-time-budget=3000 \
  "$BASE/config" 2>/dev/null
echo "  ✓ $OUT/config-full.png"

# 8. 集成 - API Keys
shoot "$BASE/integration" "integration-keys.png" 3000

# 9. 集成 - 滚到 mem0 提示词管理
$CHROME "${COMMON_OPTS[@]}" \
  --window-size=1440,2800 \
  --screenshot="$OUT/integration-prompts.png" \
  --virtual-time-budget=3500 \
  "$BASE/integration" 2>/dev/null
echo "  ✓ $OUT/integration-prompts.png"

# 10. 移动端总览
$CHROME "${COMMON_OPTS[@]}" \
  --window-size=414,900 \
  --screenshot="$OUT/overview-mobile.png" \
  --virtual-time-budget=3500 \
  "$BASE/" 2>/dev/null
echo "  ✓ $OUT/overview-mobile.png"

echo ""
echo "=== Done. Screenshots in $OUT ==="
ls -la "$OUT"
