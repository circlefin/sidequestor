---
name: yaas-dashboard-theme
description: Skin or customize the Sidequestor dashboard using workspace-owned CSS, local images/fonts, custom logo and activity train artwork, or restore its default appearance. Use for dashboard styling requests, not engine behavior or task execution.
---

# Customize the dashboard

Read [the theme API and workflow](references/theming.md) before editing. Find the intended workspace (its `.yaas/instance.json` identifies it); do not assume the package checkout is the live workspace.

Keep changes in that workspace's `theme/`. Inspect and preserve existing customizations; when replacing a theme, keep a backup outside `theme/`. Never customize by editing packaged dashboard HTML, `.yaas/engine/`, engine state, approvals, settings or launch jobs. This skill authorizes styling only.

Use the user's aesthetic direction to produce a cohesive design, including navigation, cards, dialogs and narrow layouts. Prefer documented variables; component selectors are best effort. Preserve readable status distinctions, focus indicators, visible action labels, disabled states and existing functionality. Do not hide approval destinations or replace operational text with CSS-generated labels. Use `branding.json` for supported logo/activity image configuration. Prefix custom keyframe names with a theme-specific name to avoid replacing built-in animations; respect reduced motion for CSS movement. GIF playback stays unchanged.

Refresh the real dashboard and preview desktop, narrow screens and representative dialogs when browser tools are available. Do not click operational actions merely to test styling. Report what you actually verified, any preview limitations, and the filesystem reset: empty or delete `theme/`, then refresh. No restart is required.
