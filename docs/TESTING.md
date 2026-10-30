# Test evidence and limits

## Live acceptance sample — 2026-10-27, version 0.2.0

A user-authorized run capped at 20 jobs returned 18 before the own feed ended. All jobs mode was not used. It exported 17 grids and one variation, with the variation's parent and index included; one moodboard with 23 PNG references; one personalization profile; and 72 JPEG job images. Total: 95 images / 60,590,137 bytes. No final failures or unresolved parents remained. All JSON parsed and image dimensions matched API metadata.

Pause/resume preserved the hashes and modification timestamps of 48 completed images across an extension reload and a new archive tab. Personal data, exact account identifiers, prompts, profile/board names, and image files are kept only in ignored local artifacts.

## Offline verification — 2026-10-30, version 0.3.0

Repository organization, saved-setting restoration, selected-scope scan counts, saved-state protection on scope rejection, board-pause status, terminal-network-error stopping, and board-to-job reference links were updated. Local build/syntax/manifest checks and 18 synthetic tests passed. Tests make no network requests. Version 0.3.0 was not run against Midjourney; no new live export was initiated.

There has been no full-history, multi-account, historical/deleted-board, video, or very-large-archive acceptance test. Live rate-limit enforcement and account-ban probability are unknown. A successful small export is neither completeness evidence for every account nor a promise of platform permission.

Run `npm run check` and `npm test`. The standalone `scripts/verify-export.py` checks an existing local export without contacting any service.
