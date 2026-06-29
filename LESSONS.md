# Canopy — Engineering Lessons

> Captures engineering patterns that were missed during implementation and only
> surfaced during production testing. Each entry records: what went wrong, why
> the normal process didn't catch it, and the rule to apply next time.
>
> Distinct from DECISIONS.md (architectural choices) and LIMITATIONS.md (design boundaries).
> This file is specifically for oversights — things that should have been right the first time.
>
> Last updated: 2026-06-29.

---

## L1 — Docker VOLUME directories are root-owned by default

**What happened:** The non-root `canopy` user got `EACCES` on every cache and history
write. `/data/cache.tmp` and `/data/history.jsonl` were permission-denied. The app ran
but silently lost all persistence.

**Why it wasn't caught:** `pytest` doesn't start Docker. The unit tests monkeypatch
`CANOPY_DATA_DIR` to a temp directory owned by the current user — the permission model
never applies. The issue only surfaces when the image runs as a non-root user.

**Root cause:** `VOLUME ["/data"]` in a Dockerfile creates the mount point as `root:root`.
Declaring `USER canopy` before a `chown` leaves the app process with no write access to
its own data directory.

**Rule:** In any Dockerfile with a non-root USER and a VOLUME, `mkdir` + `chown` that
directory **before** the `USER` instruction. The `chown` must happen while still running
as root, and the `VOLUME` declaration must come after so Docker initialises the named
volume with the correct ownership from the image layer.

```dockerfile
# Correct pattern:
RUN useradd -m canopy && mkdir -p /data && chown canopy:canopy /data
USER canopy
VOLUME ["/data"]   # inherits canopy ownership from the image layer
```

---

## L2 — `>=` dependency pins hide breaking changes between local and Docker

**What happened:** `pyproject.toml` specified `gradio>=6.0`. The local dev environment
had an older 6.x version installed. Docker built a fresh image and installed the latest
Gradio 6.x, which had moved the `css` parameter from `gr.Blocks()` to `launch()`. The
UserWarning only appeared in the running container, not in local tests.

**Why it wasn't caught:** `pytest` doesn't import Gradio in a version-sensitive way. The
warning fires at runtime when the Gradio application is constructed, not at import time.
No test constructs the full Gradio app.

**Root cause:** `>=` pins resolve differently in two environments. The local environment
pins the installed version implicitly; Docker always resolves to the latest compatible
release. Any breaking change between those two resolved versions only surfaces in Docker.

**Rule:** After any `>=` constraint is set on a UI or framework library, read the
changelog for the major version boundary. When Docker builds produce a different version
than local, treat that as a schema change: run the application in Docker before marking
the feature complete.

---

## L3 — Gradio `.change` fires on programmatic updates, not just user interaction

**What happened:** The history sidebar radio button used `.change` to trigger a query
when a history item was clicked. The query handler's final yield set `history_radio`
value to `None` to clear the selection. That programmatic update fired `.change` again,
which re-ran the query with the stale `question_box` value still in memory. The same
query ran 6–8 times per history click.

**Why it wasn't caught:** The test suite doesn't test Gradio event wiring. The Playwright
tests (manual) ran against the local dev server with cached queries; the repeated runs
were fast enough to be invisible at first glance.

**Root cause:** In Gradio, `.change` fires whenever the component's value changes —
whether by user interaction or programmatic update from a generator output. A component
that is both an event source and a generator output creates a trigger loop.

**Rule:** For any component that appears in both a `.change` event handler's input list
AND a generator's output list, use `.input` instead of `.change`. `.input` fires only
on direct user interaction.

```python
# Correct:
history_radio.input(fn=..., inputs=[history_radio], outputs=[question_box]).then(...)

# Creates a loop:
history_radio.change(fn=..., inputs=[history_radio], outputs=[question_box]).then(...)
```

---

## L4 — Python warning filters require the correct exception class in the hierarchy

**What happened:** The Starlette deprecation warning (`StarletteDeprecationWarning`)
continued to appear in logs after a `warnings.filterwarnings` call specified
`category=DeprecationWarning`. The filter had no effect.

**Why it wasn't caught:** The filter appeared to be correct — "Starlette deprecation
warning, filter DeprecationWarning" is a plausible inference. The filter was not tested
against a running server.

**Root cause:** `StarletteDeprecationWarning` inherits from `UserWarning`, not
`DeprecationWarning`. Python's warning filter matches on the exact class or its
subclasses. A filter targeting the wrong branch of the hierarchy silently does nothing.

**Rule:** Before writing a `warnings.filterwarnings` call for a third-party warning,
verify the actual class hierarchy (`class.__mro__`) or check the library source. When
unsure, omit the `category` argument and match on `message` pattern alone.

---

## L5 — Hardcoded hex colors break in dark mode

**What happened:** Status bar and timing footer used hardcoded `#6b7280` and `#9ca3af`.
These are mid-grey tones that look correct on a light background. On a dark background
they either disappear or clash.

**Why it wasn't caught:** The Playwright MCP browser (Chromium headless) defaults to
light mode. No dark mode testing was performed at any point — the NFR checklist had a
dark mode entry but it was documentation, not an enforced check.

**Root cause:** CSS was written for the visible (light) case without considering that
Gradio applies a `.dark` class when the browser reports `prefers-color-scheme: dark`.
Any hardcoded color that works in light mode is a potential dark mode defect.

**Rule:** In any Gradio app, use CSS custom properties (`var(--body-text-color-subdued)`,
`var(--background-fill-primary)`, etc.) instead of hardcoded hex values for any color
that should adapt to the active theme. Verify in both modes before marking UI work complete.

---

## L6 — Unit tests and linting do not validate server runtime behaviour

**What happened:** Multiple production defects — Docker permissions, Gradio API version
warnings, Gradio event loop bugs, CSS dark mode — all passed `pytest` and `ruff`
cleanly. Every one of them required running the actual container to surface.

**Why it wasn't caught:** The dev-loop verification step is `ruff check` + `pytest`. Neither
starts a server, builds a Docker image, or interacts with the Gradio event system.

**Root cause:** The verification loop was defined for code correctness, not runtime
correctness. Integration points (Docker, Gradio, third-party libraries) have failure modes
that only exist in the running process.

**Rule:** Any feature that touches Docker, a UI framework, or a third-party runtime must
include a step of running the actual container before the feature is marked complete.
The smoke test (`scripts/smoke_test_docker.sh`) automates the minimum viable runtime check.
For Gradio-specific changes, open the UI in a browser with dark mode enabled and interact
with the changed component before committing.
