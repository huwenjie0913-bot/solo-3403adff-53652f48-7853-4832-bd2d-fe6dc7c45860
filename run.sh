#!/usr/bin/env bash
# 离线启动陶瓷碎片修复工作台（仅监听本机 127.0.0.1）
set -e
cd "$(dirname "$0")"
python3 -m pip install --user --break-system-packages -q -r requirements.txt 2>/dev/null || \
  echo "[提示] 依赖已存在或需手动安装：pip install -r requirements.txt"
python3 backend/database.py
exec python3 backend/app.py
