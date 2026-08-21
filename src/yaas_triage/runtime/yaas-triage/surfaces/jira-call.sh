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

# jira-call.sh — Jira Cloud REST call.
#
# Thin shim. The implementation is client.py, which gives Slack MCP, the Slack Web
# API and Jira ONE error taxonomy (0 ok, 1 auth, 2 error, 3 bad args, 4 transient).
# Before that they each had their own, and a Slack rate limit came back as a hard
# error, which is how ~1,380 rate limits were misfiled and woke a paid worker.
#
# This file exists only because CLAUDE.md and several skills instruct the worker to
# call it by this name. Prefer: python3 yaas-triage/surfaces/client.py jira ...
exec python3 "$(dirname "$0")/client.py" jira "$@"
