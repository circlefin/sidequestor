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

# dashboard-render.test.sh — the dashboard's markdown renderer is safe by BEHAVIOUR.
#
# Briefings, quest context and timeline bodies all reach the DOM through
# `reader.innerHTML = ... markdown(body)`, and their content is not authored by the
# person reading it: a briefing quotes Slack messages other people wrote. So the
# renderer is the boundary, and asserting that the file merely CONTAINS certain
# strings does not test it. This suite extracts the real functions out of
# dashboard.html and runs them, so a change that stops escaping fails here.
#
# node is an OPTIONAL dependency of this project (doctor.sh lists it as such), so the
# behavioural half skips cleanly when node is missing. The source-level half — that the
# briefing reader routes its body through markdown() at all — always runs, because that
# is the one regression no amount of renderer hardening would catch.

set -u
_find_triage() {
  local d; d=$(cd "$(dirname "$1")" && pwd -P)
  while [ "$d" != "/" ]; do
    [ -d "$d/yaas-triage" ] && { printf '%s' "$d"; return 0; }
    d=$(dirname "$d")
  done
  echo "cannot locate the repo root above $1" >&2; return 1
}
REPO="$(_find_triage "$0")" || exit 1
HTML="$REPO/dashboard.html"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$1"; }
has()  { grep -qF -- "$2" "$HTML" && ok "$1" || bad "$1"; }
eq()   { [ "$2" = "$3" ] && ok "$1" || bad "$1 (want $3, got $2)"; }
# A missing helper or a typo must not read as a pass: an unhandled error aborts the run
# with a non-zero status instead of printing to stderr and continuing to the summary.
trap 'echo "  ERROR: the suite hit an unexpected failure at line $LINENO" >&2; exit 1' ERR
set -E

echo "── the briefing body is routed through the renderer, never inserted raw ───"
# If this line ever becomes `${body}`, every assertion below is irrelevant: the escaping
# would still be correct and completely bypassed.
has "the briefing reader renders through markdown()" 'class="brief-copy">${markdown(body)}'
has "the renderer escapes before formatting" 'let out=esc(raw)'
has "URLs are filtered through safeUrl()" 'const x=safeUrl(u)'

echo
echo "── every poll-path DOM write goes through a guard ─────────────────────────"
has "the html write guard exists" "Object.defineProperty(Element.prototype,'htmlOnce'"
has "the text write guard exists" "Object.defineProperty(Element.prototype,'textOnce'"
for target in "\$('#quest-list')" "\$('#quest-focus')" "\$('#trail')" "\$('#approval-history')" "\$('#run-history')"; do
  if grep -qF -- "${target}.innerHTML=" "$HTML"; then
    bad "${target} still writes innerHTML unconditionally on every poll"
  else
    ok "${target} writes through htmlOnce"
  fi
done
for target in "\$('#live-tail')" "\$('#live-title')"; do
  if grep -qF -- "${target}.textContent=" "$HTML"; then
    bad "${target} still writes textContent unconditionally on every poll"
  else
    ok "${target} writes through textOnce"
  fi
done
grep -qF -- "instruction.value=ui.instruction" "$HTML" \
  && bad "the prompt box value is still assigned directly, resetting its scroll" \
  || ok "the prompt box is restored through setValue"

echo
echo "── the renderer, executed ────────────────────────────────────────────────"
if ! command -v node >/dev/null 2>&1; then
  # NOT a failure: node is an optional dependency (ops/doctor.sh), and this project runs on
  # the operator's own machine rather than in CI. But a silent skip would read as coverage,
  # so name exactly what went untested.
  skip "node not installed — NOT COVERED: escaping, protocol allowlist, link placeholders,"
  skip "  and the client's briefing-date field. Install node to run them (doctor.sh checks)."
else
  # Lift esc/safeUrl/mdInline/markdown out of the single inline <script> by matching
  # braces, so the test runs the SHIPPED implementation rather than a copy of it.
  python3 - "$HTML" "$TMP/render.js" <<'PY'
import pathlib, sys
src = pathlib.Path(sys.argv[1]).read_text()

def grab(name):
    i = src.index(f"function {name}(")
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise SystemExit(f"unbalanced braces reading {name}")

def grab_stmt(prefix):
    i = src.index(prefix)
    return src[i:src.index("\n", i)]

def grab_arrow(name):
    i = src.index(f"{name}=")
    return src[i:src.index("\n", i)].rstrip(",;")

# safeUrl resolves relative URLs against location.href, which only exists in a browser.
# Shim it with the dashboard's own origin so the extracted function behaves as it does
# when shipped, instead of throwing and refusing every link.
out = ["globalThis.location = { href: 'http://localhost:8877/' };",
       # the guards are installed on Element.prototype, which node has no notion of
       "globalThis.Element = class Element {};",
       "const " + grab_arrow("esc") + ";",
       "const " + grab_arrow("safeUrl") + ";",
       grab("mdInline"), grab("markdown"),
       grab("briefDate"), grab("briefBucket"), grab("briefDateText"),
       grab_stmt("Object.defineProperty(Element.prototype,'htmlOnce'"),
       grab_stmt("Object.defineProperty(Element.prototype,'textOnce'"),
       grab("setValue")]
pathlib.Path(sys.argv[2]).write_text("\n".join(out) + """
const [mode, arg] = [process.argv[2], process.argv[3]];
if (mode === 'render') {
  console.log(JSON.stringify(JSON.parse(arg).map(c => markdown(c))));
} else if (mode === 'guards') {
  // A stand-in for a DOM element that counts how often the expensive write happens.
  class FakeEl {
    set innerHTML(v) { this.writes = (this.writes || 0) + 1; this._html = v }
    get innerHTML() { return this._html }
    set textContent(v) { this.writes = (this.writes || 0) + 1; this._text = v }
    get textContent() { return this._text }
  }
  Object.defineProperty(FakeEl.prototype, 'htmlOnce', Object.getOwnPropertyDescriptor(Element.prototype, 'htmlOnce'));
  Object.defineProperty(FakeEl.prototype, 'textOnce', Object.getOwnPropertyDescriptor(Element.prototype, 'textOnce'));
  const h = new FakeEl(); h.htmlOnce = '<p>a</p>'; h.htmlOnce = '<p>a</p>'; h.htmlOnce = '<p>a</p>'; h.htmlOnce = '<p>b</p>';
  const x = new FakeEl(); x.textOnce = 'same'; x.textOnce = 'same'; x.textOnce = 'other';
  // A textarea whose user has scrolled down inside it.
  const unchanged = { value: 'draft', scrollTop: 140 };
  setValue(unchanged, 'draft');
  const changed = { value: 'draft', scrollTop: 140 };
  setValue(changed, 'draft revised');
  console.log(JSON.stringify({
    htmlWrites: h.writes, textWrites: x.writes,
    unchangedScroll: unchanged.scrollTop, unchangedValue: unchanged.value,
    changedScroll: changed.scrollTop, changedValue: changed.value,
  }));
} else if (mode === 'briefdate') {
  // What the UI would show for this payload: the ISO instant it resolved, and the bucket.
  const brief = JSON.parse(arg);
  const d = briefDate(brief);
  console.log(JSON.stringify([
    Number.isFinite(d.getTime()) ? d.toISOString() : 'INVALID',
    briefBucket(brief),
    briefDateText(brief),
  ]));
}
""")
PY

  # render <markdown> → the HTML the dashboard would insert. Dies rather than returning
  # empty on any failure: an empty string would satisfy every "does not contain" assertion
  # below and turn a broken harness into a green suite.
  render() {
    local args out
    args="$(python3 -c 'import json,sys; print(json.dumps([sys.argv[1]]))' "$1")"
    out="$(node "$TMP/render.js" render "$args" 2>&1)" || { bad "renderer failed to run: $out"; return 1; }
    out="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0])' 2>/dev/null)" \
      || { bad "renderer produced no parseable output"; return 1; }
    [ -n "$out" ] || { bad "renderer returned empty output for: $1"; return 1; }
    printf '%s' "$out"
  }

  OUT="$(render '<img src=x onerror=alert(1)>')"
  case "$OUT" in
    *"<img"*) bad "raw HTML in a briefing reaches the DOM unescaped: $OUT" ;;
    *"&lt;img"*) ok "raw HTML is escaped, not rendered" ;;
    *) bad "unexpected render of raw HTML: $OUT" ;;
  esac

  OUT="$(render '<javascript:alert(1)|click me>')"
  case "$OUT" in
    *"<a href"*) bad "a javascript: URL was turned into a link: $OUT" ;;
    *) ok "an unsafe protocol is never linkified" ;;
  esac

  OUT="$(render '[t](javascript:alert(1))')"
  case "$OUT" in
    *"<a href"*) bad "a markdown link to javascript: was linkified: $OUT" ;;
    *) ok "...including in markdown link syntax" ;;
  esac

  OUT="$(render '<https://example.test/ok|the label>')"
  case "$OUT" in
    *'<a href="https://example.test/ok"'*'rel="noopener"'*"the label"*)
      ok "a safe link survives, with rel=noopener" ;;
    *) bad "safe link did not render: $OUT" ;;
  esac

  # The allowlist is https/mailto/slack — plain http is deliberately not linkified.
  OUT="$(render '<http://example.test/plain|insecure>')"
  case "$OUT" in
    *"<a href"*) bad "a plain http URL was linkified: $OUT" ;;
    *) ok "http is not in the protocol allowlist" ;;
  esac

  # A `$&` inside a URL or label used to be expanded by String.replace's substitution
  # patterns when the link was swapped back in, corrupting the output.
  OUT="$(render '<https://example.test/a$&b|a $& label>')"
  case "$OUT" in
    *'https://example.test/a$&amp;b'*) ok "a \$-pattern in a URL is inserted literally" ;;
    *) bad "\$-pattern corrupted the URL: $OUT" ;;
  esac

  # The link placeholder must not be forgeable from prose.
  OUT="$(render 'see <https://example.test/x|here> and YAASLINKTOKEN0Z too')"
  case "$OUT" in
    *"YAASLINKTOKEN0Z too"*) ok "prose that looks like a placeholder is left alone" ;;
    *) bad "prose was replaced by a link: $OUT" ;;
  esac

  OUT="$(render '## Heading
- one
- two')"
  case "$OUT" in
    *"<h2>Heading</h2>"*"<ul>"*"<li>one</li>"*"<li>two</li>"*)
      ok "ordinary markdown still renders" ;;
    *) bad "markdown structure broke: $OUT" ;;
  esac
  # The sentinel is private-use, so prose cannot forge it: those codepoints are stripped
  # from the input before any link is swapped out.
  # The forged sentinel is placed BEFORE the real link on purpose: replace() takes the first
  # occurrence, so prose that arrives first is what would steal the link. Two properties are
  # asserted — exactly one link, and NO private-use codepoint survives into the HTML (an
  # unconsumed placeholder is the tell that the swap went to the wrong occurrence).
  OUT="$(render "$(printf 'forged \xee\x80\x800\xee\x80\x81 then link <https://example.test/x|here>')")"
  LINKS="$(printf '%s' "$OUT" | grep -o '<a href' | wc -l | tr -d ' ')"
  LEFTOVER="$(printf '%s' "$OUT" | LC_ALL=C grep -c $'\xee\x80\x80\|\xee\x80\x81' || true)"
  if [ "$LINKS" = "1" ] && [ "$LEFTOVER" = "0" ]; then
    ok "a forged private-use sentinel cannot capture a link, and none leak into the DOM"
  else
    bad "forged sentinel disturbed the render ($LINKS link(s), $LEFTOVER leftover): $OUT"
  fi

  echo
  echo "── the poll path never rewrites DOM it has not changed ───────────────────"
  # The dashboard repolls every POLL_MS. Rewriting a subtree that did not change destroys
  # and recreates its nodes, which is what made a focused prompt box flicker and a scrolled
  # draft jump to the top. These guards are the mechanism that stops it.
  OUT="$(node "$TMP/render.js" guards 2>&1)"
  gfield() { printf '%s' "$OUT" | python3 -c "import json,sys; print(json.load(sys.stdin)[sys.argv[1]])" "$1" 2>/dev/null; }
  eq "four htmlOnce writes of 3 identical + 1 new → 2 DOM writes" "$(gfield htmlWrites)" "2"
  eq "three textOnce writes of 2 identical + 1 new → 2 DOM writes" "$(gfield textWrites)" "2"
  eq "setValue leaves an unchanged textarea's scroll alone" "$(gfield unchangedScroll)" "140"
  eq "...and its value alone" "$(gfield unchangedValue)" "draft"
  eq "setValue preserves scroll when the value DOES change" "$(gfield changedScroll)" "140"
  eq "...while writing the new value" "$(gfield changedValue)" "draft revised"

  echo
  echo "── the client renders a briefing date from the canonical field only ───────"
  # briefDate() must read `at`. A payload whose mtime (`ts`) is years away from `at` proves
  # which field won: if the client ever goes back to mtime or to parsing the filename, the
  # resolved instant changes and this fails. The server-side assertions in
  # dashboard-routes.test.sh cannot catch that, because they never run the client.
  bd() { node "$TMP/render.js" briefdate "$1" 2>&1; }
  OUT="$(bd '{"file":"2026-08-17_0830_morning.md","at":"2026-08-17T08:30:00+08:00","ts":"2020-01-01T00:00:00+00:00"}')"
  case "$OUT" in
    *'2026-08-17T00:30:00.000Z'*) ok "the date comes from at, not from mtime" ;;
    *'2020-'*) bad "the client is still dating briefings from mtime: $OUT" ;;
    *) bad "unexpected briefDate result: $OUT" ;;
  esac
  # An absent `at` must read as invalid rather than silently falling back to mtime.
  OUT="$(bd '{"file":"2026-08-17_0830_morning.md","ts":"2020-01-01T00:00:00+00:00"}')"
  case "$OUT" in
    *INVALID*) ok "a briefing with no canonical time is not dated from mtime" ;;
    *) bad "a missing at fell back to another representation: $OUT" ;;
  esac
  # Bucketing runs off the same field, so an old briefing is never grouped as Today.
  OUT="$(bd '{"file":"2020-01-01_0830_morning.md","at":"2020-01-01T08:30:00+08:00","ts":"2020-01-01T00:00:00+00:00"}')"
  case "$OUT" in
    *'"Older"'*) ok "an old briefing buckets as Older" ;;
    *) bad "bucketing did not use the canonical field: $OUT" ;;
  esac
fi

echo
printf 'dashboard-render: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
