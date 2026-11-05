# Test evidence and limits

## Live acceptance sample — 2026-10-27, version 0.2.0

A user-authorized run capped at 20 jobs returned 18 before the own feed ended. All jobs mode was not used. It exported 17 grids and one variation, with the variation's parent and index included; one moodboard with 23 PNG references; one personalization profile; and 72 JPEG job images. Total: 95 images / 60,590,137 bytes. No final failures or unresolved parents remained. All JSON parsed and image dimensions matched API metadata.

Pause/resume preserved the hashes and modification timestamps of 48 completed images across an extension reload and a new archive tab. Personal data, exact account identifiers, prompts, profile/board names, and image files are kept only in ignored local artifacts.

## Offline verification — 2026-10-30, version 0.3.0

Repository organization, saved-setting restoration, selected-scope scan counts, saved-state protection on scope rejection, board-pause status, terminal-network-error stopping, and board-to-job reference links were updated. Local build/syntax/manifest checks and 18 synthetic tests passed. Tests make no network requests. Version 0.3.0 was not run against Midjourney; no new live export was initiated.

## Offline verification — 2026-11-02, version 0.4.0

Choosing what to export was added. Local build/syntax/manifest checks and 49 synthetic tests passed; they make no network requests. They cover the selection logic and the controller with a saved choice, including every privacy rule: a left-out or not-reviewed generation has no downloaded image, no `jobs.jsonl` or `likes.jsonl` row and no `generated_job_ids` link; a left-out moodboard has no row and no downloaded reference image; a code that only left-out generations used appears in no moodboard or profile row; and a choice made for another account or scope is refused before anything is written. A resume keeps the choice it started with after a newer choice that leaves out none of what it exports. When the newer choice does leave something out, the resume stops before any download or file write and leaves the saved export unchanged; this is tested for an export paused without a choice, one paused with an earlier choice, a completed one, and work made after the export started. While the chooser is open, every other command is refused, nothing is exported, and another tab cannot take the run lease. Choose reuses a scan only while it is under ten minutes old. Small test is not part of a choice's scope: it neither refuses a choice nor forces a new scan, and the chooser starts from the latest choice for the account. Clear selection, on the panel and in the popup, forgets the choice only on a second click within a few seconds, without a browser dialog. Without a choice, this version's controller and the 0.3.1 controller wrote byte-identical JSONL files and the same downloads, and `export_meta.json` differed only by `"selection": null`, on synthetic input and on input rebuilt from a local sample export that is not in the repository.

The chooser was checked in headless Chromium on a local page with synthetic data (3 moodboards, 150 generations, one of them in two moodboards, one video) at 1280×800 and 1920×1080, with every request that was not a local file aborted: whole-moodboard boxes including the partly ticked state, per-moodboard and global select all and none, the summary line, Show more, Cancel's question and Esc, keyboard focus, the thumbnail fallbacks and placeholders, saving and reopening a choice, and that the page's own CSS does not reach the window. The panel was also run end to end in Chromium on a local page standing in for the Organize page, with a stand-in page bridge answering with synthetic data: Choose what to export scanned once, a second Choose reused the scan, Save updated the panel, and New export wrote and downloaded only what was chosen, with the matching `selection` block and a left-out parent reported as a parent gap. The popup's choice line and buttons were checked the same way. After the review fixes, the real unpacked extension was loaded in Chromium against a local stand-in page and API, with every other request aborted: Export from the popup while the chooser was open answered "Finish choosing first" and started nothing; after a stricter choice, Export / resume of a completed export stopped with its message, downloaded nothing, wrote no file and left the saved export unchanged; and Clear selection, on the panel and in the popup, cleared only on a second click. The chooser's header ("Your work as of …") and footer were checked at 1280×800, 1920×1080 and 720×800. These pages and their screenshots are not in the repository.

Version 0.4.0 was not run against Midjourney: no live scan, choice or export was made. The thumbnail name the chooser tries first is unverified ([ENDPOINTS.md](ENDPOINTS.md#chooser-thumbnails)).

## Offline verification — 2026-11-05, version 0.4.1

Moodboard pictures stored as Midjourney short links (`https://s.mj.run/<code>`) are now downloaded. Local build, syntax and manifest checks and 76 synthetic tests passed; they make no network requests. They cover:

- which addresses the page bridge, the content script and the service worker accept (the same set in all three);
- a short link saved under the suffix Chrome gives its type, and recorded under that name, with `final_url` and no query;
- a short link that leads to a page, an SVG, a non-HTTPS address or a download Chrome flags, or that does not finish within 2 minutes (before or after its response begins): removed, and reported for that picture alone;
- a short link's type checked as soon as Chrome reports one, against every type Chrome reported for it, so that a server's own type (such as `application/octet-stream`) counts even after Chrome reports `image/jpeg` for the `.jpeg` it named; a type that is not a picture, an error page's included, ends the download before its file is complete and is retried like a server error;
- a short link's download checked every 2 seconds and on any change Chrome reports, a danger verdict included, and waiting while Chrome scans it;
- Chrome's errors failing a short link alone after the usual backoff, with its partial download erased, while local errors such as a full disk still stop the export; an error code Chrome gives when it cannot start a download is treated the same way;
- a picture saved outside the export folder or under another name stopping the export, whatever words its folder or name contain;
- unchanged error handling for CDN pictures, with no time limit;
- a resume that skips finished pictures, a generation picture saved through the `.png` fallback included, retries failed ones, and records the name Chrome saved a picture under where 0.4.0 recorded another;
- a resume that downloads again the pictures that moved on a board, and drops the record of one whose place now holds a picture on another host;
- a failed retry that drops its stale record from the counts.

The 349 viewer unit tests passed, apart from 33 browser tests that were skipped. They include a 0.4.1 export with short-link pictures through `--check`, and, in hosted mode, a short-link picture of a hidden generation withheld from everyone else, also when its final address names the generation after the first path segment.

How `chrome.downloads` treats a link that redirects elsewhere was checked in Chrome for Testing 148.0.7778.96 on Linux, headless. The test extension had only the `downloads` permission, and a local server's links answered 302 to a second local host.

- The download followed the redirect without host permissions.
- Chrome replaced a requested `.jpeg` with `.png`, `.webp`, `.gif`, `.html` or `.txt` to match the type sent.
- It kept `.jpeg` for JPEG, for `application/octet-stream`, and when the server named the file.
- `mime` and `finalUrl` were known when `download()` resolved; `mime` was then the type the server sent, and, once Chrome had named the file, the type of the suffix it was saved under (`image/jpeg` for every file kept as `.jpeg`).
- Cancelling right after that left no file; `removeFile`, which deletes a completed download's file, refused one still in progress.
- For an HTTP error (403, 404, 410, 429 or 500) the item was still in progress, with the error page's type, when `download()` resolved, and Chrome reported the error code only afterwards, in the change that named the file.

No Midjourney host was contacted.

Version 0.4.1 was not run against Midjourney: where short links lead, and what their servers send, is unknown until a real export.

There has been no full-history, multi-account, historical/deleted-board, video, or very-large-archive acceptance test. Live rate-limit enforcement and account-ban probability are unknown. A successful small export is neither completeness evidence for every account nor a promise of platform permission.

Run `npm run check` and `npm test`. The standalone `scripts/verify-export.py` checks an existing local export without contacting any service.
