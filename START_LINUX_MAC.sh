#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONUTF8=1

if [ ! -f config.toml ] && [ -f config.example.toml ]; then
  cp config.example.toml config.toml
  echo "[INFO] Создан config.toml из шаблона — впиши API-ключ Kintaro в веб-панели (Настройки)."
fi

if command -v uv >/dev/null 2>&1; then
  echo "🚀 Запуск KINTARO FARM OS v5.0 (uv) на http://127.0.0.1:8080 ..."
  exec uv run kintaro-auto web --host 127.0.0.1 --port 8080
fi

if [ ! -x ".venv/bin/kintaro-auto" ]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -e .
  .venv/bin/python -m playwright install chromium || true
fi

echo "🚀 Запуск KINTARO FARM OS v5.0 на http://127.0.0.1:8080 ..."
exec .venv/bin/kintaro-auto web --host 127.0.0.1 --port 8080
