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

# dashboard-start.sh — start the YAAS dashboard server and open it in the browser.
# Safe to run multiple times: skips launch if already listening on the port.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=8877

if lsof -i ":$PORT" -sTCP:LISTEN -t &>/dev/null; then
    echo "Dashboard already running on port $PORT"
    open "http://localhost:$PORT"
    exit 0
fi

echo "Starting YAAS dashboard on http://localhost:$PORT ..."
python3 "$SCRIPT_DIR/dashboard-server.py" "$PORT" &
SERVER_PID=$!

# Wait up to 3s for the server to accept connections
for i in $(seq 1 10); do
    if curl -sf "http://localhost:$PORT" > /dev/null 2>&1; then
        break
    fi
    sleep 0.3
done

open "http://localhost:$PORT"
echo "Dashboard open.  Server PID: $SERVER_PID"
echo "To stop: kill $SERVER_PID"
