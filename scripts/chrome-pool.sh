#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'EOF'
chrome-pool.sh 已退出账号生命周期管理。
账号运行时必须由持久账号租约、节点监督器和独占 PROFILE_DIR 管理；
本脚本不会启动、停止、删除或复用任何浏览器槽。
EOF
exit 2
