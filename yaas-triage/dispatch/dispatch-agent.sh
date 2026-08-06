#!/bin/bash
# Copyright 2026 Circle Internet Group, Inc. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# dispatch-agent.sh — backend-agnostic YaaS worker dispatch.
#
# YaaS's only job here: launch the configured agent headless in the repo, pass
# the prompt, and STREAM its raw event JSONL to stdout (agent's stderr → this
# script's stderr). Exit code = the agent's exit code. triage.sh owns the
# tee → format-stream.py pipeline and the post-run token extraction; this
# script is a thin, MCP-agnostic launcher.
#
# HOW the agent reaches Slack/Coda/etc. is NOT this script's concern — that is
# the user's per-agent config. In practice YaaS routes Slack through the repo's
# mcp-call.sh (shell + Keychain token) for uniform sender identity, which is why
# the codex posture below is bounded-sandbox-with-network rather than the full
# --dangerously-bypass (native side-effectful MCP writes need the bypass; a
# shell curl to mcp-call.sh does not).
#
# Usage:   YAAS_AGENT=codex|cursor|claude dispatch-agent.sh "<prompt>"
#
# Env:
#   YAAS_AGENT                   backend (default: claude)
#   REPO_ROOT                    repo working dir (default: parent of script dir)
#   YAAS_CLAUDE_PERMISSION_MODE  claude --permission-mode (default: acceptEdits)
#   YAAS_CODEX_PERMISSION_MODE   workspace-write (default) or bypassPermissions
#   YAAS_WORKER_PERMISSION_MODE  deprecated Claude fallback
#   YAAS_CLAUDE_MODEL            default: opus
#   YAAS_CLAUDE_EFFORT           claude --effort (low|medium|high|...); unset → omit flag
#   YAAS_CODEX_MODEL             default: "" → codex uses ~/.codex/config.toml model
#   YAAS_CURSOR_MODEL            default: "" → cursor uses its default (auto)

set -uo pipefail

PROMPT="${1:?usage: dispatch-agent.sh <prompt>}"
BACKEND="${YAAS_AGENT:-claude}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The repo root is the nearest ancestor directory that contains yaas-triage/.
#
# NOT "$SCRIPT_DIR/..": that holds only while every script sits directly in yaas-triage/,
# and silently resolves to yaas-triage/ itself once a script moves into a subdirectory,
# writing state into a parallel tree nothing reads. `pwd -P` resolves symlinks so this
# agrees with Python's Path.resolve() in the sibling scripts. Fails non-zero rather than
# guessing, because a guessed root is the silent divergence being eliminated.
_repo_root() {
  local d
  d=$(cd "$1" 2>/dev/null && pwd -P) || { echo "no such dir: $1" >&2; return 1; }
  while :; do
    [ -d "$d/yaas-triage" ] && { printf '%s' "$d"; return 0; }
    [ "$d" = "/" ] && break
    d=$(dirname "$d")
  done
  echo "cannot locate repo root above $1 (no ancestor has yaas-triage/)" >&2
  return 1
}
REPO_ROOT="${REPO_ROOT:-$(_repo_root "$SCRIPT_DIR")}" || exit 1

# The launchd/loop context has a minimal PATH; the agent CLIs live in these
# dirs. Prepend them so bare `claude`/`codex`/`cursor-agent` resolve.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

case "$BACKEND" in
  claude)
    MODEL="${YAAS_CLAUDE_MODEL:-opus}"
    EFFORT="${YAAS_CLAUDE_EFFORT:-}"
    PERMISSION_MODE="${YAAS_CLAUDE_PERMISSION_MODE:-${YAAS_WORKER_PERMISSION_MODE:-acceptEdits}}"
    exec claude --model "$MODEL" \
      ${EFFORT:+--effort "$EFFORT"} \
      --permission-mode "$PERMISSION_MODE" \
      --mcp-config "$SCRIPT_DIR/worker.mcp.json" \
      --strict-mcp-config \
      --tools "Read,Edit,Write,Bash,Glob,Grep,WebFetch,WebSearch" \
      --output-format stream-json --verbose \
      -p "$PROMPT"
    ;;
  codex)
    # The default bounded sandbox limits writes to the workspace and enables
    # network access for mcp-call.sh. bypassPermissions opts into Codex's fully
    # unsandboxed, no-approval mode and should only be used deliberately.
    # Disable Codex's native Slack plugin so there is exactly ONE Slack path:
    # the repo's mcp-call.sh (shell). This (a) avoids the native write-gate that
    # cancels side-effectful MCP calls under this bounded sandbox, and (b) keeps
    # the sender identity as "yourself-as-a-service" rather than the plugin's
    # "ChatGPT". Harmless if the plugin isn't installed.
    PERMISSION_MODE="${YAAS_CODEX_PERMISSION_MODE:-workspace-write}"
    set -- codex exec --json --skip-git-repo-check
    case "$PERMISSION_MODE" in
      workspace-write)
        set -- "$@" \
          -s workspace-write \
          -c approval_policy=never \
          -c 'sandbox_workspace_write.network_access=true'
        ;;
      bypassPermissions)
        set -- "$@" --dangerously-bypass-approvals-and-sandbox
        ;;
      *)
        echo "dispatch-agent.sh: invalid YAAS_CODEX_PERMISSION_MODE='$PERMISSION_MODE' (expected workspace-write or bypassPermissions)" >&2
        exit 2
        ;;
    esac
    set -- "$@" \
      -c 'plugins."slack@openai-curated".enabled=false' \
      -C "$REPO_ROOT"
    [ -n "${YAAS_CODEX_MODEL:-}" ] && set -- "$@" -m "$YAAS_CODEX_MODEL"
    exec "$@" "$PROMPT"
    ;;
  cursor)
    # --print headless, stream-json; --approve-mcps auto-approves MCP calls (no
    # per-call prompt exists headless). Default agent mode already permits the
    # shell tool used to run mcp-call.sh.
    set -- cursor-agent -p --output-format stream-json --approve-mcps \
      --workspace "$REPO_ROOT"
    [ -n "${YAAS_CURSOR_MODEL:-}" ] && set -- "$@" --model "$YAAS_CURSOR_MODEL"
    exec "$@" "$PROMPT"
    ;;
  *)
    echo "dispatch-agent.sh: unknown YAAS_AGENT='$BACKEND'" >&2
    exit 2
    ;;
esac
