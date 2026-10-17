#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${VIDEO_TRANSLATE_ENV_FILE:-$ROOT/.env}"
OPEN_EDITOR=0

if [[ "${1:-}" == "--open" ]]; then
  OPEN_EDITOR=1
elif [[ -n "${1:-}" ]]; then
  echo "Usage: bash scripts/open_env_setup.sh [--open]" >&2
  exit 2
fi

if [[ ! -f "$ENV_FILE" ]]; then
  umask 077
  cat > "$ENV_FILE" <<'EOF'
# Local-only video translation credentials. Never commit this file.
DASHSCOPE_API_KEY=
ALIYUN_WORKSPACE_ID=
ALIYUN_REGION=cn-beijing
# Fixed ASR stack: one asynchronous filetrans job per video, no public file host.
ALIYUN_ASR_MODEL=qwen-audio-3.1-asr-flash-filetrans
# Optional precompiled hotword list. It only applies when its target_model equals ALIYUN_ASR_MODEL.
ALIYUN_ASR_VOCABULARY_ID=
# Optional local hotword JSON sent as inline hotwords (Qwen-Audio-3.x models only).
ALIYUN_ASR_HOTWORDS_FILE=
EOF
  chmod 600 "$ENV_FILE"
fi

echo "Local env file: $ENV_FILE"
echo "Fill DASHSCOPE_API_KEY and ALIYUN_WORKSPACE_ID. Do not commit this file."

if [[ "$OPEN_EDITOR" == "1" && "$(uname)" == "Darwin" ]]; then
  open -e "$ENV_FILE"
fi
