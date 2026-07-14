# Matte selection — investigation & decision (2026-07-13)

Session notes from a live debugging/decision session using `scripts/spike_matte_gallery.py` against the three real Frame TVs (43L, 43R, 50), following up on `KNOWN_ISSUES.md` #3 ("elección de matte sin decidir"). Kept separate from `docs/dev_plan.md` because Etapa 3 there is effectively closed (only the multi-day Pi stability check in 3.7 remains) — this is a new, narrow thread of work: hardware-API troubleshooting plus a product decision, not a dev_plan iteration.

**Bottom line: the matte is decided (`shadowbox_warm`, confirmed twice on all three TVs) and shipped to production**, along with the connection-robustness fixes found along the way. See "Production status: shipped" at the end.

---

## Summary

What started as "run the spike script and pick a matte" turned into four real, compounding bugs in the spike script plus one substantive discovery about how the Frame TV Art API actually expects matte ids to be shaped — undocumented by the `samsungtvws` library and only found by reading its GitHub issue tracker and cross-checking against live TV behavior. Each bug masked the next one:

1. Matte list was always empty (`_matte_ids()` read the wrong dict key) → every TV showed only `'none'`.
2. TV connections held open across a whole round idled long enough to throw `BrokenPipeError`.
3. Even after fixing (2), the underlying `samsungtvws` library can hang indefinitely inside `_wait_for_d2d` regardless of `timeout=` — a known upstream bug, not something fixable by us.
4. Even after fixing (1)-(3) and successfully uploading, **every applied matte silently rendered as if no matte were applied at all** — because a bare style name like `"shadowbox"` (what `get_matte_list()` returns) is not a valid matte id on real hardware. Valid ids are compound `style_color` strings, e.g. `"shadowbox_warm"`.

Once (4) was understood, manually setting a matte via the physical remote and reading it back with `get_current()` confirmed the correct id shape, and the same value was applied programmatically to all three TVs — twice, with fresh generated images both times — with no further failures.

---

## Bugs found in `scripts/spike_matte_gallery.py` (all fixed in the script itself)

### 1. `_matte_ids()` read a nonexistent `"id"` key

`get_matte_list()` (from `samsungtvws`) returns entries shaped `{"matte_type": "shadowbox"}` — there is no separate `"id"` field. The original code did `entry.get("id")`, which was always `None`, so the flattening collapsed to just the script's own `'none'` fallback. Every TV printed "1 mattes -> ['none']" and the whole gallery loop ran exactly one round before finishing — it wasn't crashing, it genuinely had nothing left to iterate. Confirmed against the library's own official example script (`example/art_remove_mats.py` in `xchwarze/samsung-tv-ws-api`), which does the equivalent of reading `.values()` off each dict — same fix, independently confirmed.

**Fix:** read `entry.get("matte_type")` instead.

### 2. Idle connections triggered `BrokenPipeError`

The script originally opened one `SamsungTVArt` connection per TV and held it for the entire run — including the ~13s each *other* TV's turn took, plus however long the human paused at the `input()` prompt between rounds. Reproduced directly: sleeping 20s+ after `open()` before the first `upload()` reliably kills the socket; under 15s is fine. Since 43L/43R's per-round work (upload + select + cleanup, ~13s) ran before the 50"'s turn in the same round, the 50" was consistently the one hitting a stale connection.

**Fix:** open a fresh connection immediately before each TV's per-round work and close it right after, so no connection is ever idle long enough to go stale.

### 3. `samsungtvws`'s own hang, not bounded by `timeout=`

Even with fix #2, a round against the 50" hung completely silently for 50+ seconds with `timeout=15` set on the connection — no exception, no output, nothing until manually killed. This is a confirmed **upstream bug**: [xchwarze/samsung-tv-ws-api issue #106, "stuck at upload()"](https://github.com/xchwarze/samsung-tv-ws-api/issues/106). `_wait_for_d2d` (in `samsungtvws/art/art.py`) loops on `_recv_frame()` until a frame's id matches the outstanding request; the per-call `timeout=` only bounds a single `recv()`, not the loop. If the TV keeps emitting other frames (keepalives, unrelated events) that never match, each individual `recv()` succeeds and resets the clock — the loop can spin forever. The issue thread shows this affecting multiple users across firmware versions since 2022, "fixed in master" per the maintainer but evidently not eliminated on all firmware/timing combinations (reproduced live against 3.0.5, the version pinned in this repo).

**Fix:** wrap each round's connect+upload+select+cleanup in a worker thread with a wall-clock deadline (30s). If the thread is still alive past the deadline, force-close the raw socket (`tv.connection.sock.close()`) from outside — the only way to unstick a thread blocked in `recv()` — log it, and move on. Verified live: a round against the 50" hit this exact condition once during testing; the watchdog caught it, logged `"sin respuesta tras 30s..."`, and the script continued cleanly to the next round instead of hanging.

**Side effect observed once, not fully root-caused:** during an earlier (less careful) manual retry attempt — closing and immediately reopening a connection right after a `BrokenPipeError`, before the watchdog fix existed — the 50" TV's screen dropped out of full-screen Art Mode into its on-screen Art Store/navigation menu (the "browse and manage art" UI a human would see when pressing the remote's menu button), rather than just failing the upload. It recovered on its own within a couple minutes with no further intervention. This has not been reproduced deliberately since the reconnect-per-round + watchdog fixes landed, and no repro attempt was made to pin down the exact trigger — treat as a known-possible but not well-understood side effect of aggressively reconnecting to a TV that's already in a confused connection state, not a new bug to chase separately unless it recurs.

---

## The real discovery: matte ids must be compound `style_color` strings

After bugs 1–3 were fixed, `_apply_round` successfully uploaded and selected images with matte values like `"none"`, `"modernthin"`, `"modern"`, `"modernwide"`, `"shadowbox"` — no errors from the API. But **the physical TVs never visibly changed matte across six full rounds.** All three screens looked identical the entire run.

Root cause, found by reading [xchwarze/samsung-tv-ws-api issue #133, "Anyone know which matte style/color options work for uploaded images?"](https://github.com/xchwarze/samsung-tv-ws-api/issues/133): a bare style name (what `get_matte_list()`'s `matte_types` list actually contains — `"modern"`, `"shadowbox"`, etc.) **is not a valid matte id** on real hardware. The TV silently accepts it as if it were a no-op rather than erroring. Valid ids are compound `{style}_{color}` strings, e.g. `shadowbox_polar`, `modern_warm`, `modernwide_warm`. `get_matte_list()` returns the style names and the 16 valid color names (`black`, `neutral`, `antique`, `warm`, `polar`, `sand`, `seafoam`, `sage`, `burgandy` [sic, confirmed by another user in that thread — not a typo in our code], `navy`, `apricot`, `byzantine`, `lavender`, `redorange`, `skyblue`, `turquoise`) as two **separate** lists (`matte_types` / `matte_colors`) — combining them into the compound id is left to the caller, undocumented in the library itself.

This means: across the whole gallery run, uploads never failed and `select_image` always reported success, but the matte visually applied was effectively always "whatever the TV falls back to for an unrecognized id" (observed as looking identical to `none`) — a silent no-op, not a visible error, which is why six rounds of the review session showed no change on any screen.

### Confirming the correct id shape live

Rather than brute-force the ~10 styles × 16 colors ≈ 160 combinations, we set a matte manually via the physical remote on 43L and read it back programmatically:

```python
tv.get_current()
# -> {"content_id": "MY_F0156", "matte_id": "none",
#     "portrait_matte_id": "shadowbox_warm", ...}
```

This confirmed both the id shape (`shadowbox_warm`) and a second important detail below.

### `matte_id` vs `portrait_matte_id` — governed by the TV's physical orientation, not by us

`get_current()`/`change_matte()` always carry **two** separate matte fields. Which one actually controls what's rendered depends on the *physical* orientation of that TV, not anything the caller chooses:

- **43L / 43R (portrait-mounted, 9:16):** `portrait_matte_id` is what's visibly applied. `matte_id` can be anything and has no visible effect.
- **50" (landscape, 16:9):** `matte_id` is what's visibly applied. `portrait_matte_id` has no visible effect there.

Confirmed by trying to set the 50" via `change_matte(content_id, matte_id="shadowbox_warm", portrait_matte="shadowbox_warm")`: the API's own ack only echoed back `portrait_matte_id` as updated even though both were sent identically, and a subsequent `get_current()` showed **both fields reverted to the old value (`flexible`)** — the change didn't persist at all on the 50" via this path. This is consistent with dev_plan.md's characterization of the 50" as running an older/legacy protocol variant (§3.4) — `change_matte` may simply not be reliable against it, whereas the upload-time `matte=`/`portrait_matte=` parameters (exercised successfully in `deploy_image_to_tv` since Etapa 3) did work.

**Practical implication for any future code:** always pass the *same* compound id to both `matte` and `portrait_matte` (as `deploy_image_to_tv` already does) rather than trying to special-case by TV — the TV itself picks the field that matters for its orientation, so sending both identically is correct and orientation-agnostic. Do not rely on `change_matte()` against the 50" for anything that must actually stick; prefer re-`upload()`.

### `change_matte()` alone doesn't force a redraw — needs a follow-up `select_image()`

On 43R, `change_matte(content_id, matte_id="shadowbox_warm", portrait_matte="shadowbox_warm")` returned success and `get_current()` confirmed `portrait_matte_id: "shadowbox_warm"` was stored — but the physical screen did not visibly update. Calling `select_image(content_id, show=True)` immediately after forced the TV to actually re-render with the new matte. **Lesson: `change_matte()` updates stored metadata; only `select_image(..., show=True)` triggers a visible redraw.** Any future code that calls `change_matte()` on an already-displayed piece of art must follow it with `select_image()` or the change won't be visible until the next unrelated redraw.

---

## Decision: `shadowbox_warm`

Confirmed live, twice, with two different freshly-generated 4K images each time (one 9:16 shared by 43L/43R, one 16:9 for the 50), via the upload-time `matte`/`portrait_matte` parameters (not `change_matte`) — the reliable path, especially for the 50":

| Run | 43L | 43R | 50 |
|---|---|---|---|
| 1 | `MY_F0157` | `MY_F0149` | `MY_F0085` |
| 2 | `MY_F0158` | `MY_F0150` | `MY_F0086` |

Both runs succeeded cleanly on all three TVs with no errors, no hangs, and the user confirmed visually both times that the matte looked correct ("worked perfectly").

---

## Production status: shipped

Items 1–3 below (config flip, KNOWN_ISSUES closeout, connection-robustness
fix) landed together — see `config/tv_deploy.toml` (`[matte]` table, all
three TVs set to `shadowbox_warm`, keyed by full TV name per the repo's
existing `config/tvs.toml`/`config/room.toml` convention rather than a
single house-wide value) and `src/engine/tv_deploy.py` (`deploy_image_to_tv`
now opens `SamsungTVArt` with `timeout=_TV_TIMEOUT_SECONDS` and runs the
whole open→upload→select→cleanup→history sequence inside a worker thread
under a `_DEPLOY_DEADLINE_SECONDS` wall-clock watchdog, ported from
`scripts/spike_matte_gallery.py`'s `_apply_round` — a timed-out worker gets
its raw socket force-closed from outside, same mechanism as the spike,
while preserving the function's four original distinguishable error
messages and its `{'error': ...}`-never-raises contract). `KNOWN_ISSUES.md`
#3 is marked resolved referencing this document.

Item 4 (the one-time "50 dropped into Art Store nav menu" side effect) is
still unresolved/unreproduced — no action taken, watch for recurrence.
