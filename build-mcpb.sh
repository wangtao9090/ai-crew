#!/usr/bin/env bash
# 把 ai-crew 打包成 Claude 桌面端（Cowork）可安装的 .mcpb 扩展。
# 产物：dist/ai-crew-<version>.mcpb（一个 zip，根目录含 manifest.json + server.py）。
# 运行时用 `uv run --with mcp python server.py` 启动，自包含；只额外要求本机装好 GitHub Copilot CLI。
set -euo pipefail
cd "$(dirname "$0")"

VERSION=$(python3 -c "import json;print(json.load(open('mcpb/manifest.json'))['version'])")
STAGE=build/mcpb
OUT="dist/ai-crew-${VERSION}.mcpb"

rm -rf "$STAGE" && mkdir -p "$STAGE" dist
cp mcpb/manifest.json "$STAGE/manifest.json"          # 必须在 zip 根目录
cp server.py          "$STAGE/server.py"              # 入口（${__dirname}/server.py）
cp requirements.txt   "$STAGE/requirements.txt" 2>/dev/null || true
cp credentials.example.py "$STAGE/credentials.example.py" 2>/dev/null || true
cp README.md          "$STAGE/README.md" 2>/dev/null || true

# 校验 manifest 合法
python3 -c "import json;json.load(open('$STAGE/manifest.json'));print('manifest OK')"

rm -f "$OUT"
( cd "$STAGE" && zip -qr "../../$OUT" . -x '.*' '__pycache__/*' )
echo "打好包：$OUT"
unzip -l "$OUT"
