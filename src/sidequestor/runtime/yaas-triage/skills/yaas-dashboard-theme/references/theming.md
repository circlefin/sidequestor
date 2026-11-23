# Sidequestor theme API v1

The packaged dashboard is the default. `<workspace>/theme/custom.css` loads as the last stylesheet in the head. Entry CSS is read on request with `Cache-Control: no-store`; image/font/imported-CSS assets use private browser caching with mandatory ETag revalidation (`private, no-cache`). Refresh after editing. Missing or empty CSS is a no-op. Upgrades and resource sync never write under `theme/`.

## Workflow and reset

1. Identify the intended workspace and read its `AGENTS.md` if present. Inspect `theme/` before editing. Back up existing work outside that folder when replacing it.
2. Create `theme/custom.css` and optionally `theme/assets/`. No manifest, installer, registration or engine change is required for CSS. Optional `branding.json` configures logo/activity artwork.
3. Use variables for shared styling, then explicit CSS selectors for the rest. Current bundled styles contain hardcoded colors, so variables alone do not reskin every component. Inspect the installed dashboard HTML read-only if needed.
4. Refresh and inspect the actual dashboard without performing operational actions. Check overflow, contrast, focus, hover, disabled controls and dialogs; preserve status distinctions and `[hidden]` behavior. If no browser is available, disclose that visual QA remains outstanding.
5. To reset, empty or delete `theme/`, then refresh. Sidequestor will not recreate it. Store backups outside `theme/`.

## Variables

Existing supported variables (current Field Guide defaults):

| Variables | Purpose |
| --- | --- |
| `--ink`, `--muted`, `--faint` | Primary and secondary text |
| `--paper`, `--surface`, `--line` | Surfaces and separators |
| `--blue`, `--blue2` | Accent and pale accent (legacy names; current accent is green) |
| `--teal`, `--teal2` | Positive status and pale background |
| `--amber`, `--amber2` | Attention status and pale background |
| `--red`, `--red2` | Error/danger status and pale background |
| `--shadow` | Shared shadow |
| `--mono`, `--sans` | Monospace and body font stacks |

Additional v1 variables:

| Variable | Default |
| --- | --- |
| `--page-background` | `#e8f0e8` |
| `--page-background-image` | Built-in Field Guide grid/gradients; set `none` for a flat fill |
| `--control-background` | Unset (`initial`): preserve each input/textarea/select's built-in background; setting a color overrides those controls, including composer states |
| `--focus-ring` | `#76b7ff` |
| `--base-font-size` | `14px` on body only; components with explicit sizes do not scale automatically |
| `--card-radius` | `13px` (major panels) |

For example:

```css
:root {
  --ink: #30251b;
  --surface: #f6eddb;
  --page-background: #d3c1a0;
  --page-background-image: none;
  --control-background: #fff7e8;
  --focus-ring: #8b4513;
  --sans: Georgia, "Times New Roman", serif;
  --card-radius: 3px;
}
.panel-head { background: #e4d3af; }
```

Current selectors such as `.panel`, `.panel-head`, `.hero`, `.button`, `.nav`, `.focus-head`, `.focus-footer`, `.review-card`, `.audit-panel`, `.briefings` and `dialog` can be overridden. These selectors and DOM structure have best-effort compatibility; only documented variables form the v1 contract. Normal cascade rules still apply: inline styles, `!important` and higher specificity can take precedence. Avoid broad rules overriding display/visibility and avoid replacing meaningful text using pseudo-elements.

## Local assets and limits

Place assets under `theme/assets/`. In custom.css, `url("assets/parchment.png")` resolves to `/theme/assets/parchment.png`. A local font can use `@font-face { font-family: Chronicle; src: url("assets/chronicle.woff2") format("woff2"); }`. Assets referenced by nested CSS resolve relative to that CSS file.

Allowed formats: CSS, PNG, JPEG, GIF, WebP, AVIF, WOFF and WOFF2 (case-insensitive extensions). No served HTML, JavaScript, SVG, dotfiles, directory listings or symlinks anywhere under theme. Existing inline `data:` images remain allowed by the dashboard policy; `data:` fonts do not. Use local WOFF/WOFF2 files. Theme routes retain dashboard cookie authentication and Host validation. Remote CSS imports, images and fonts are blocked by the dashboard content security policy; bundle assets locally. Use trusted CSS: it can hide or misrepresent dashboard controls.

CSS is limited to 1 MiB per file; other assets to 10 MiB. Missing, unreadable, non-regular or oversized entry CSS is discarded entirely and served as empty CSS; rejected entry CSS other than a missing file produces a server warning. Other missing/rejected assets return 404; oversized assets return 413. Inspect the browser network panel and dashboard error log for failures. Refresh after deleting an asset to see the new response. Nothing loads scripts or changes dashboard behavior.

## Logo and activity train

Optional `theme/branding.json` is read once per page load (maximum 32 KiB). Emptying
or deleting `theme/`, or omitting a valid configuration, restores built-in branding
after refresh. It accepts presentation-only fields; unknown fields are ignored.

```json
{
  "logo": "assets/crest.png",
  "activity": {
    "images": ["assets/flower.gif", "assets/knight.png", "assets/star.webp"],
    "height": 28,
    "gap": 16,
    "speed": 32,
    "direction": "right"
  }
}
```

Paths must be under `assets/`, with a supported raster image extension. No URLs,
traversal or symlinks. Custom logos use natural-quality scaling within their frame without stretching non-square artwork. Missing/broken logo falls back to the original logo. Missing
activity images are skipped; if none load, the built-in activity belt returns.
Up to 32 unique configured images are accepted. Cars are randomly chosen initially
and whenever one exits, with a common height and preserved image aspect ratio.
Height is 12–96 pixels; gap 0–96 pixels; speed 4–160 pixels per second. Direction
is `right` (left to right, default) or `left`. Static and animated images can mix.
The bounded car count adapts to the available strip width when the layout resizes. Intrinsic GIF animation speed is never changed. Reduced-motion preference stops
conveyor translation only. Artwork is decorative: the adjacent status text remains
the source of operational status.

The activity module is separate from engine state and can be removed independently.
Use `.motion-stream.sq-activity-train` and `--sq-activity-height` only to style its
container; avoid changing image transforms, which position individual cars.

## Custom CSS animation

Prefix keyframe names (for example `mytheme-candle-glow`) so they do not replace
built-in `spin`, `conveyor`, `cargo-flash` or `help-pop`. Gate custom CSS motion with
`@media (prefers-reduced-motion: no-preference)` or provide a reduced-motion rule
that follows the animation rules. The built-in reduced-motion styles precede user
CSS and cannot override arbitrary later animation declarations.

Theme file serving currently targets POSIX/macOS environments using descriptor-
relative opens; Windows is not supported by this loader. Assets are revalidated on
use: unchanged files return 304 without retransmitting GIF/font/image bytes, while
edits and deletions take effect on refresh. Keep assets small for the first load.
