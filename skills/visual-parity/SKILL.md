---
name: visual-parity
description: Freeze existing Chromium states and require pixel-exact evidence when migrating a UI component.
disable-model-invocation: true
---

# Visual parity

Select a component and freeze its current appearance before implementation. Take a baseline for every relevant state, including state transitions through the actual controls. If the baseline behavior needs changing, settle that as a separate product decision before setting the parity target.

Run `python3 -m workbench.visual_parity baseline --help` from the iBoard plugin root. Point it to a Git revision and a local image that contains the source under `/app`, with Chromium and Playwright in the image. Install Playwright under `/app/node_modules/playwright` in that image. Supply a JSON manifest such as:

```json
{
  "schema": 1,
  "viewport": {"width": 640, "height": 360},
  "states": [
    {"id": "closed", "path": "index.html", "steps": []},
    {"id": "open", "path": "index.html", "steps": [
      {"action": "click", "selector": "#toggle"},
      {"action": "visible", "selector": "#panel"}
    ]}
  ]
}
```

Use local file states or supply `server` with a tracked Node `entry`, an `args` array, a loopback `port`, and a `ready` path. In server mode, each state path stays on that one loopback origin. Available actions are `click`, `hover`, `fill`, `press`, `visible`, and `text`. Use `value` with `fill` for text, `press` for a key, or `text` for the expected text.

Each baseline state needs matching screenshots from two clean offline Chromium containers. Keep the baseline record SHA-256. For an automated UI task, pass that record and digest to `workbench.start --visual-baseline ... --visual-baseline-sha256 ... --source-mode image --build-source-image`. The selected baseline image remains the source of the browser runtime. Use `workbench.visual_parity compare --help` for an independent comparison of an existing candidate image derived from that baseline image.

Compare decoded pixels with a zero threshold. Nonzero results retain each state, bounding box and local difference mask. Work through shared primitives before dependent components. Give independent components their own task and worktree, and repeat against the same frozen baseline. Preserve the harness, selectors and state conditions during implementation.

The PR gate reopens both pairs of captures, checks their digests and recomputes the comparisons for the exact candidate. The original independent UI and delivery checks still apply. Report the verified states, pixel counts, baseline record and pending component work.
