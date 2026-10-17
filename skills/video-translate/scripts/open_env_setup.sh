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
# Fixed ASR stack. The default sync model takes the audio inline, so no upload is needed.
# Select a -filetrans model only for the public-URL fallback; see references/execution-contract.md.
ALIYUN_ASR_MODEL=qwen-audio-3.1-asr-flash
# Optional precompiled hotword list. It only applies when its target_model equals ALIYUN_ASR_MODEL.
ALIYUN_ASR_VOCABULARY_ID=
# Optional local hotword JSON sent as inline hotwords (Qwen-Audio-3.x models only).
ALIYUN_ASR_HOTWORDS_FILE=
# Optional chunking tunables for the default synchronous path.
ALIYUN_ASR_SYNC_MAX_SECONDS=270
ALIYUN_ASR_SYNC_CONCURRENCY=2
ALIYUN_ASR_SYNC_RETRIES=3
ALIYUN_ASR_MP3_KBPS=16
# Only needed by the filetrans fallback that hands the model a public audio URL.
OKFILE_TOKEN=
EOF
  chmod 600 "$ENV_FILE"
fi

echo "Local env file: $ENV_FILE"
echo "Fill DASHSCOPE_API_KEY, ALIYUN_WORKSPACE_ID, and OKFILE_TOKEN. Do not commit this file."

if [[ "$OPEN_EDITOR" == "1" && "$(uname)" == "Darwin" ]]; then
  open -e "$ENV_FILE"
fi
