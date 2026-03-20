#!/bin/bash
# ── 一键下载项目 + 安装依赖 + 打包 .pkg ──────────────────────
# 用法：在终端粘贴以下一行命令即可
#
#   curl -fsSL https://your-server/install.sh | bash
#
# 或者把本文件下载到本地后：
#   bash install.sh
# ─────────────────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC}  $1"; }
info() { echo -e "${YELLOW}→${NC}  $1"; }
die()  { echo -e "${RED}✗${NC}  $1"; exit 1; }

echo ""
echo "═══════════════════════════════════════"
echo "   Shell Agent — 一键打包安装脚本"
echo "═══════════════════════════════════════"
echo ""

# ── 检查 Python ───────────────────────────────────────────────
info "检查 Python..."
command -v python3 &>/dev/null || die "未找到 python3，请先安装 Python：https://python.org"
PY_VER=$(python3 -c "import sys; print(sys.version_info[:2] >= (3,9))")
[ "$PY_VER" = "True" ] || die "需要 Python 3.9+，当前版本过低"
ok "Python $(python3 --version)"

# ── 安装 pip 依赖 ─────────────────────────────────────────────
info "安装打包依赖（首次约需 1~2 分钟）..."
pip3 install -q pyinstaller rumps pyobjc-framework-Cocoa fastapi uvicorn
ok "依赖安装完成"

# ── 确认项目文件在当前目录 ────────────────────────────────────
[ -f "agent.py" ]      || die "未找到 agent.py，请在项目根目录运行此脚本"
[ -f "build_pkg.py" ]  || die "未找到 build_pkg.py，请确认项目文件完整"

# ── 运行打包脚本 ──────────────────────────────────────────────
info "开始打包（约需 3~5 分钟）..."
python3 build_pkg.py

# ── 完成 ──────────────────────────────────────────────────────
PKG=$(ls ShellAgent-*.pkg 2>/dev/null | head -1)
if [ -n "$PKG" ]; then
  echo ""
  echo "═══════════════════════════════════════"
  echo -e "${GREEN}✅  打包完成：${NC}${PKG}"
  echo "═══════════════════════════════════════"
  echo ""
  echo "  下一步："
  echo "  1. 双击 ${PKG}"
  echo "  2. 按向导操作（约 1 分钟）"
  echo "  3. 状态栏出现 🟢 即表示服务已启动"
  echo ""
  # 自动用 Finder 显示 .pkg 文件
  open -R "$PKG" 2>/dev/null || true
else
  die "打包失败，未生成 .pkg 文件"
fi
