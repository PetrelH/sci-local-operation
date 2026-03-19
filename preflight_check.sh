#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Shell Agent — 本地预检脚本
# 在打包之前，验证关键流程在你的开发机上是否正常
# 不会真正安装任何东西，所有操作都可以撤销
#
# 用法：chmod +x preflight_check.sh && ./preflight_check.sh
# ═══════════════════════════════════════════════════════════════

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'; BOLD='\033[1m'

PASS=0; FAIL=0; WARN=0

pass() { echo -e "  ${GREEN}✓${NC} $1"; ((PASS++)); }
fail() { echo -e "  ${RED}✗${NC} $1"; ((FAIL++)); }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; ((WARN++)); }
section() { echo -e "\n${BOLD}${BLUE}── $1 ──${NC}"; }

echo -e "${BOLD}Shell Agent 打包预检${NC}"
echo "=================================================="

# ─── 1. 项目文件 ──────────────────────────────────────────────
section "项目文件"

[ -f "agent.py" ]       && pass "agent.py 存在"       || fail "agent.py 不存在"
[ -f "menubar_app.py" ] && pass "menubar_app.py 存在"  || fail "menubar_app.py 不存在（菜单栏 App）"
[ -f "console.html" ]   && pass "console.html 存在"   || warn "console.html 不存在（Web 控制台将跳过）"
[ -f "build_pkg.sh" ]   && pass "build_pkg.sh 存在"   || fail "build_pkg.sh 不存在"

# ─── 2. Python 依赖 ──────────────────────────────────────────
section "Python 依赖"

python3 -c "import fastapi"    2>/dev/null && pass "fastapi"    || fail "fastapi 未安装 → pip install fastapi"
python3 -c "import uvicorn"    2>/dev/null && pass "uvicorn"    || fail "uvicorn 未安装 → pip install uvicorn"
python3 -c "import pydantic"   2>/dev/null && pass "pydantic"   || fail "pydantic 未安装 → pip install pydantic"
python3 -c "import rumps"      2>/dev/null && pass "rumps"      || fail "rumps 未安装 → pip install rumps pyobjc-framework-Cocoa"
python3 -c "import PyInstaller" 2>/dev/null && pass "pyinstaller" || fail "pyinstaller 未安装 → pip install pyinstaller"

# ─── 3. 系统工具 ─────────────────────────────────────────────
section "系统工具"

command -v pkgbuild     &>/dev/null && pass "pkgbuild（系统自带）"     || fail "pkgbuild 不存在，需安装 Xcode Command Line Tools"
command -v productbuild &>/dev/null && pass "productbuild（系统自带）" || fail "productbuild 不存在"
command -v launchctl    &>/dev/null && pass "launchctl"                || fail "launchctl 不存在（非 macOS？）"

# ─── 4. agent.py 语法检查 ────────────────────────────────────
section "agent.py 语法检查"

if python3 -m py_compile agent.py 2>/dev/null; then
  pass "agent.py 语法正确"
else
  fail "agent.py 存在语法错误："
  python3 -m py_compile agent.py
fi

# ─── 5. agent.py 能否正常启动（10 秒冒烟测试）──────────────
section "agent.py 启动测试（端口 18765）"

AGENT_PORT=18765
AGENT_TOKEN=preflight-test

# 清理可能残留的进程
pkill -f "shellagent\|agent\.py.*18765" 2>/dev/null || true
sleep 1

echo "  启动 agent.py..."
AGENT_PORT=$AGENT_PORT AGENT_TOKEN=$AGENT_TOKEN \
  python3 agent.py &>/tmp/shellagent_preflight.log &
AGENT_PID=$!
sleep 3

if kill -0 $AGENT_PID 2>/dev/null; then
  pass "进程启动成功（PID=$AGENT_PID）"

  # 测试 /
  HEALTH=$(curl -s --max-time 3 "http://localhost:${AGENT_PORT}/" || echo "")
  if echo "$HEALTH" | grep -q '"status"'; then
    pass "健康检查 GET / 返回正常"
  else
    fail "GET / 无响应或格式异常"
    echo "    响应：$HEALTH"
  fi

  # 测试 /exec（带 token）
  EXEC=$(curl -s --max-time 5 -X POST \
    -H "Content-Type: application/json" \
    -H "x-token: $AGENT_TOKEN" \
    -d '{"command":"echo hello_preflight"}' \
    "http://localhost:${AGENT_PORT}/exec" || echo "")
  if echo "$EXEC" | grep -q "hello_preflight"; then
    pass "/exec 命令执行正常（echo 测试）"
  else
    fail "/exec 执行失败"
    echo "    响应：$EXEC"
  fi

  # 测试 /exec 错误 token 应返回 401
  UNAUTH=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 \
    -X POST -H "Content-Type: application/json" \
    -H "x-token: wrong-token" \
    -d '{"command":"echo hi"}' \
    "http://localhost:${AGENT_PORT}/exec" || echo "0")
  if [ "$UNAUTH" = "401" ]; then
    pass "错误 Token 正确返回 401"
  else
    warn "错误 Token 返回 $UNAUTH（期望 401）"
  fi

  # 测试 /cwd（持久目录功能）
  CWD=$(curl -s --max-time 3 \
    -H "x-token: $AGENT_TOKEN" \
    "http://localhost:${AGENT_PORT}/cwd" || echo "")
  if echo "$CWD" | grep -q '"cwd"'; then
    pass "/cwd 返回正常：$CWD"
  else
    warn "/cwd 接口异常（可能是旧版 agent.py）"
  fi

  kill $AGENT_PID 2>/dev/null
  wait $AGENT_PID 2>/dev/null || true
  pass "进程已停止"
else
  fail "agent.py 启动失败，查看日志："
  echo ""
  tail -20 /tmp/shellagent_preflight.log | sed 's/^/    /'
  echo ""
fi

# ─── 6. launchd plist 模板验证 ───────────────────────────────
section "launchd plist 模板验证"

TEMP_PLIST=$(mktemp /tmp/shellagent_test_XXXX.plist)
cat > "$TEMP_PLIST" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.shellagent.test</string>
  <key>ProgramArguments</key>
  <array><string>/usr/local/bin/shellagent</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AGENT_TOKEN</key><string>__TOKEN__</string>
    <key>AGENT_PORT</key><string>__PORT__</string>
  </dict>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
PLIST

# 模拟 postinstall 的 sed 替换
sed -i '' 's/__TOKEN__/test-token-abc/g' "$TEMP_PLIST"
sed -i '' 's/__PORT__/9999/g'            "$TEMP_PLIST"

if plutil -lint "$TEMP_PLIST" &>/dev/null; then
  pass "plist 替换后格式合法"
  TOKEN_OK=$(grep -c "test-token-abc" "$TEMP_PLIST")
  PORT_OK=$(grep  -c "9999"           "$TEMP_PLIST")
  [ "$TOKEN_OK" -gt 0 ] && pass "TOKEN 占位符替换成功" || fail "TOKEN 替换失败"
  [ "$PORT_OK"  -gt 0 ] && pass "PORT 占位符替换成功"  || fail "PORT 替换失败"
else
  fail "plist 替换后格式异常"
  plutil -lint "$TEMP_PLIST"
fi
rm -f "$TEMP_PLIST"

# ─── 7. launchctl 权限检查 ───────────────────────────────────
section "launchctl 权限"

if [ "$(id -u)" -eq 0 ]; then
  pass "当前以 root 运行（postinstall 脚本运行环境）"
else
  warn "当前非 root（正常，预检不需要 root）"
  warn "postinstall 脚本由 macOS Installer 以 root 运行，实际安装时无需担心"
fi

# sudo launchctl 可用性
if sudo -n launchctl print system &>/dev/null 2>&1; then
  pass "sudo launchctl 可用"
else
  warn "sudo launchctl 需要密码（正常，安装时 Installer 会提示用户输入）"
fi

# ─── 8. menubar_app.py 语法检查 ──────────────────────────────
section "menubar_app.py 语法检查"

if [ -f "menubar_app.py" ]; then
  if python3 -m py_compile menubar_app.py 2>/dev/null; then
    pass "menubar_app.py 语法正确"
  else
    fail "menubar_app.py 存在语法错误"
    python3 -m py_compile menubar_app.py
  fi
fi

# ─── 汇总 ────────────────────────────────────────────────────
echo ""
echo "=================================================="
echo -e "${BOLD}预检结果${NC}"
echo "--------------------------------------------------"
echo -e "  ${GREEN}通过${NC}  $PASS 项"
[ "$WARN" -gt 0 ] && echo -e "  ${YELLOW}警告${NC}  $WARN 项（不影响打包，但建议处理）"
[ "$FAIL" -gt 0 ] && echo -e "  ${RED}失败${NC}  $FAIL 项（需要修复后再打包）"
echo "=================================================="

if [ "$FAIL" -eq 0 ]; then
  echo -e "\n${GREEN}✅  一切就绪，可以运行 ./build_pkg.sh 打包！${NC}\n"
  exit 0
else
  echo -e "\n${RED}❌  有 $FAIL 项需要修复，请处理后重新运行此脚本。${NC}\n"
  exit 1
fi
