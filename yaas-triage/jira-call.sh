#!/bin/bash
# jira-call.sh — Jira Cloud REST call.
#
# Thin shim. The implementation is client.py, which gives Slack MCP, the Slack Web
# API and Jira ONE error taxonomy (0 ok, 1 auth, 2 error, 3 bad args, 4 transient).
# Before that they each had their own, and a Slack rate limit came back as a hard
# error, which is how ~1,380 rate limits were misfiled and woke a paid worker.
#
# This file exists only because CLAUDE.md and several skills instruct the worker to
# call it by this name. Prefer: python3 yaas-triage/client.py jira ...
exec python3 "$(dirname "$0")/client.py" jira "$@"
