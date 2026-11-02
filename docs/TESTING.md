# Test evidence and limits

## Live acceptance sample — 2026-10-27, version 0.2.0

A user-authorized run capped at 20 jobs returned 18 before the own feed ended. All jobs mode was not used. It exported 17 grids and one variation, with the variation's parent and index included; one moodboard with 23 PNG references; one personalization profile; and 72 JPEG job images. Total: 95 images / 60,590,137 bytes. No final failures or unresolved parents remained. All JSON parsed and image dimensions matched API metadata.

Pause/resume preserved the hashes and modification timestamps of 48 completed images across an extension reload and a new archive tab. Personal data, exact account identifiers, prompts, profile/board names, and image files are kept only in ignored local artifacts.

## Offline verification — 2026-10-30, version 0.3.0

Repository organization, saved-setting restoration, selected-scope scan counts, saved-state protection on scope rejection, board-pause status, terminal-network-error stopping, and board-to-job reference links were updated. Local build/syntax/manifest checks and 18 synthetic tests passed. Tests make no network requests. Version 0.3.0 was not run against Midjourney; no new live export was initiated.

## Offline verification — 2026-11-02, version 0.4.0

Choosing what to export was added. Local build/syntax/manifest checks and 44 synthetic tests passed; they make no network requests. They cover the selection logic and the controller with a saved choice, including every privacy rule: a left-out or not-reviewed generation has no downloaded image, no `jobs.jsonl` or `likes.jsonl` row and no `generated_job_ids` link; a left-out moodboard has no row and no downloaded reference image; moodboard and profile codes come only from exported generations; and a choice made for another account or scope is refused before anything is written. A resume keeps the choice it started with after a newer choice that leaves out none of what it exports. When the newer choice does leave something out, the resume stops before any download or file write and leaves the saved export unchanged; this is tested for an export paused without a choice, one paused with an earlier choice, a completed one, and work made after the export started. While the chooser is open, every other command is refused, nothing is exported, and another tab cannot take the run lease. Choose reuses a scan only while it is under ten minutes old. Without a choice, this version's controller and the 0.3.1 controller wrote byte-identical JSONL files and the same downloads, and `export_meta.json` differed only by `"selection": null`, on synthetic input and on input rebuilt from a local sample export that is not in the repository.

The chooser was checked in headless Chromium on a local page with synthetic data (3 moodboards, 150 generations, one of them in two moodboards, one video) at 1280×800 and 1920×1080, with every request that was not a local file aborted: whole-moodboard boxes including the partly ticked state, per-moodboard and global select all and none, the summary line, Show more, Cancel's question and Esc, keyboard focus, the thumbnail fallbacks and placeholders, saving and reopening a choice, and that the page's own CSS does not reach the window. The panel was also run end to end in Chromium on a local page standing in for the Organize page, with a stand-in page bridge answering with synthetic data: Choose what to export scanned once, a second Choose reused the scan, Save updated the panel, and New export wrote and downloaded only what was chosen, with the matching `selection` block and a left-out parent reported as a parent gap. The popup's choice line and buttons were checked the same way. These pages and their screenshots are not in the repository.

Version 0.4.0 was not run against Midjourney: no live scan, choice or export was made. The thumbnail name the chooser tries first is unverified ([ENDPOINTS.md](ENDPOINTS.md#chooser-thumbnails)).

There has been no full-history, multi-account, historical/deleted-board, video, or very-large-archive acceptance test. Live rate-limit enforcement and account-ban probability are unknown. A successful small export is neither completeness evidence for every account nor a promise of platform permission.

Run `npm run check` and `npm test`. The standalone `scripts/verify-export.py` checks an existing local export without contacting any service.
