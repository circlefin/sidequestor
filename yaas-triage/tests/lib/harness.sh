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

# harness.sh — the assertion vocabulary every suite shares.
#
# Source it AFTER SCRIPT_DIR is set, since that is what locates it:
#
#     SCRIPT_DIR="$(_find_triage "$0")" || exit 1
#     . "$SCRIPT_DIR/tests/lib/harness.sh"
#
# _find_triage deliberately stays in each suite. It is the walk that finds this
# file, so it cannot live in this file — the same reason _repo_root is duplicated
# across the Python and shell entry points rather than shared. Counting `..` from
# the suite's own path instead would reintroduce the depth bug that
# behaviour/repo-root.test.sh exists to prevent.
#
# A suite that needs extra counters (RECORDED, FAILED, …) declares them itself
# after sourcing; this file owns only PASS/FAIL and the three assertions.

PASS=0; FAIL=0

ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }

# `bad` returns 0 so that `cond || bad "..."` does not abort a suite running
# under `set -e`. A failure is recorded in FAIL and reported by the suite's exit.
bad() {
  FAIL=$((FAIL+1))
  printf '  \033[31mFAIL\033[0m %s\n' "$1"
  [ -n "${2:-}" ] && printf '       %s\n' "$2"
  return 0
}

eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (want '$3', got '$2')"; }
