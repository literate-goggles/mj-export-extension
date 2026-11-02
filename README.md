# MJ Export

Manifest V3 Chrome extension for exporting the logged-in user's own Midjourney work to a local folder. No backend, telemetry, remote code, CDN scripts, npm dependencies, or ZIP library.

## Account risk and coverage

**This is experimental, unofficial software, not a guarantee of a complete backup or a ban-safe workflow.** Midjourney's [Terms of Service](https://docs.midjourney.com/hc/en-us/articles/32083055291277-Terms-of-Service), checked 2026-10-30, prohibit automated access and reserve suspension/ban rights. Read-only behavior, low request rates, and owning the exported work do not exempt this extension. The terms also restrict accessing the service to develop or offer competing products/services, relevant to commercial downstream use. Obtain written permission covering your intended export/use before running it on a valuable account. Neither Scan, Choose what to export nor Export is risk-free: all make automated API requests.

| Content | What the extension actually covers |
|---|---|
| Personal image generations | Jobs returned by the observed own `/api/imagine` feed, with cursor pagination and the selected scope. Completeness across every account/history type has not been established. Videos are unsupported; deleted/trashed/hidden/legacy jobs are not independently enumerated or guaranteed. |
| Moodboards | Current own boards and their current reference images returned by `/api/moodboards`. Turn **Small test** off to include all returned boards. No historical versions or deleted boards are retrieved. |
| Generations made using a moodboard | Exported as ordinary jobs from the own-job feed. `moodboards.jsonl` includes `generated_job_ids` derived from explicit personalization references on exported jobs; `codes` preserves observed historical codes. No separate per-board generation-history endpoint is queried. An empty list does not prove a board was never used. |
| Profiles and likes | Returned own profiles, visible global aggregate counts, and likes on selected own jobs. This is not a download of trained personalization weights, individual preference ratings, or all liked work by other creators. |

Only a capped 18-job, one-board, one-profile live sample has been validated. See [test evidence](docs/TESTING.md) and [API observations](docs/ENDPOINTS.md).

## Install

1. Open `chrome://extensions` in Chrome.
2. Enable Developer mode, choose **Load unpacked**, and select this folder.
3. Open `https://www.midjourney.com/organize` in the Chrome profile already signed in to Midjourney. Reload that page after installing or reloading the extension.
4. Open MJ Export's popup, or expand the **MJ Export · Local export** panel at the lower right of the Midjourney page.

After source changes, reload the extension card and then reload the Midjourney page. Keep that page open during a run. Navigating away or closing it interrupts the run; **Export / resume** revalidates ownership and continues saved progress.

## Use

- **Scan** counts records in the selected scope. Its estimate assumes 1 MiB per image and is clearly labeled approximate; scanning downloads no images.
- Scope is **All jobs** (default), **Since date**, or **Max N jobs** (20 when selected). Since-date mode scans metadata across the feed rather than assuming its order.
- **Include images** defaults on. Turn it off for metadata only.
- **Small test** defaults off. When enabled, it restricts boards and profiles to one each. Disable it to include all own boards and visible profiles. This limit is independent of the job limit.
- **Export / resume** continues the saved export. **Pause** stops further work after in-flight requests/downloads finish. **Resume** never starts automatically.
- **New export** starts a separate folder and resets extension progress for the new run; it does not delete earlier downloaded files.
- **Choose what to export** lets you leave out whole moodboards or single generations. It opens a chooser over the Midjourney page for the scope set in the controls. It first checks the account with one request, and scans first (the same requests as Scan) when there is no scan of that account and scope yet; it downloads no images. See [Choosing what to export](#choosing-what-to-export).
- **Clear selection** goes back to exporting everything in the scope.
- **Open export folder** reveals the metadata file in its folder. Chrome's own download-directory settings control the base destination. If Chrome asks where to save every download, those prompts still apply.

Only one run may be active across tabs, and an open chooser counts as one: until you **Save selection** or **Cancel** it, every other command, from the popup or another tab, answers "Finish choosing first". Run settings, the saved choice of what to export, and errors appear in the popup and on-page panel. An unfinished run restores its saved settings; a fresh install or completed run shows the new defaults; its **Resume** button uses the saved run's settings. **Export** uses the current controls and rejects a changed scope rather than overwriting the saved run.

### Exact sequence after permission to run

1. Reload the extension card in `chrome://extensions`, then reload your signed-in Organize page. The repository root is still the Load unpacked folder.
2. The defaults are **All jobs**, **Include images** on, and **Small test** off. An unfinished run retains its previous settings for safe resumption.
3. **Scan is optional.** It counts the chosen scope and estimates bytes, but does not create a cached export or guarantee completeness. Export reads the feed again.
4. **Choosing is optional.** To leave moodboards or generations out, use **Choose what to export** for the scope you will export, tick what to keep, and **Save selection**.
5. Because the earlier small test has saved progress, use **New export** when changing to All jobs or another scope, or to export with a new choice. This starts immediately in a separate folder. For the same saved scope, use **Export / resume** on the page or **Resume** in the popup.
6. Keep the tab open. Use **Pause**, then **Resume** as needed. When complete, choose **Open export folder** and inspect `export_meta.json` for failures and parent gaps.

There is no need to click Export again after a completed run.

## Choosing what to export

**Choose what to export** opens a window over the Midjourney page.

- On the left are your moodboards, each with its number of reference images and generations, and **Other generations**: generations not made with any of your moodboards. A moodboard's box ticks or unticks all of it: its reference images and every generation made with it. A partly ticked moodboard shows a dash.
- On the right is the moodboard you clicked. **Include the moodboard and its N reference images** decides whether the moodboard itself (its row and its pictures) is exported. Below it, every generation made with the moodboard is a card you can tick or untick; **Select all** and **Select none** there apply to these generations. Cards come in pages of about 120; **Show more** adds the next page.
- A generation made with two moodboards appears under both, with one tick.
- **Select all** and **Select none** at the top apply to everything. The line at the bottom says how much will be exported.
- **Save selection** keeps the choice. **Cancel** or Esc closes the window, and asks first when you changed something.

**New export** then saves only what is ticked, and so does **Export** when there is no saved export to continue. Nothing left out is downloaded or written: no image, no row, no like and no link to it; moodboard and profile codes come only from exported generations. An exported generation still names its parent, and a parent you left out is listed as a parent gap. Profiles are not part of the choice.

The choice belongs to the account and scope it was made for (**Include images** does not matter). An export of another scope or account is refused until you choose again for it or use **Clear selection**. The choice covers the work that was in the scan it was made from: generations and moodboards made after that are not exported until you choose again. With **Small test** on, only the first chosen moodboard is exported.

An export keeps the choice it started with when you resume it, but it never exports something your latest choice leaves out. If you save a new choice that leaves out something a saved export would export, finished or not, resuming it (**Export / resume** on the page, **Export** or **Resume** in the popup) stops before it downloads or writes anything and says so. Use **New export** to export with the new choice, or **Clear selection** to finish the saved export as it started. A new choice that only adds things, or leaves out only things the saved export does not include, does not change it.

## Output

`Downloads/mj_export_<username>_<YYYYMMDD>/` (new explicit runs add a timestamp suffix):

- `jobs.jsonl`: one JSON object per exported job: ID, creation time, original command, prompt with flags removed, parsed flags, parent ID/index, kind, relative image paths, source URLs, dimensions. Extra source type and personalization-reference fields preserve provenance.
- `moodboards.jsonl`: board ID/codes, name, version when exposed, creation time, reference-image URLs/files/dimensions, and `generated_job_ids` linking exported own jobs. With a saved choice, `generated_left_out` counts the board's generations in the scope that were left out.
- `profiles.jsonl`: profile IDs/codes, names, major versions, and aggregate visible ranking counts. No individual ratings are collected.
- `likes.jsonl`: nonempty `image_likes` from the exported own jobs.
- `images/`: original fetched bytes, without resizing or re-encoding.
- `export_meta.json`: extension version, export time, username, selected scope, `selection` (see below), counts, status, failures, unresolved parents and limitations.

`selection` is `null` when you did not choose. Otherwise it records when you chose (`chosen_at`); for moodboards and for generations, how many were in the scope (`available`), `exported` and `left_out`; for generations also `not_reviewed`, the ones that appeared after you chose, which are not exported either; and `left_out_moodboard_ids`, the left-out moodboards that exported generations still reference. Small test can trim moodboards further, so the counts need not add up to `available`. A parent you left out is listed in `parent_gaps` with `left_out: true`, and `limitations` gains "Items the designer left out are not in this export."

Metadata-only runs have empty image-file lists. A stopped run still tries to write partial metadata and a failure report. A successful metadata-only run does not prove image availability.

Folder output avoids the 2 GB ZIP limit. Individual images are held in memory briefly; the whole image archive is never assembled in RAM. Metadata files are generated using an offscreen document. API requests happen in the Midjourney page context. That context authorizes image URLs from observed own records; `chrome.downloads` saves those URLs directly using the browser session, avoiding cross-origin JavaScript response restrictions. Metadata uses offscreen local blobs with a generic MIME type so Chrome preserves the requested JSONL filenames.

## Resume and ownership

`chrome.storage.local` keeps cursor/checkpoint, discovered and completed IDs, normalized metadata, and completed download IDs/paths. `unlimitedStorage` accommodates large metadata sets. On each explicit resume the selected own-feed scope is re-read to establish ownership in the current page session. Already completed files are checked against Chrome's download records and skipped; unfinished files are overwritten at their deterministic paths. A run keeps the choice of what to export that it started with, on every resume, so a folder never mixes two choices. A resume stops before it downloads or writes anything when your latest choice leaves out something it would export. No historical cursor is blindly trusted after an account change.

Resume depends on Chrome retaining its download history. If a finished file is moved/deleted or its download record disappears, it may be downloaded again. A crash between download completion and saving progress may also cause a safe overwrite. Clearing extension storage removes resume state but does not delete exported files. The export folder is local data and should be shared deliberately.

## Privacy and safeguards

Host permissions are limited to `https://www.midjourney.com/*` and `https://cdn.midjourney.com/*`. The extension never reads session credentials or browser cookies. No account data is uploaded anywhere. It only reads same-origin API responses and saves selected metadata and image bytes locally. The chooser shows thumbnails by loading `https://cdn.midjourney.com` images in the page, as the site itself does; it makes no API requests and saves nothing but the choice, in extension storage.

The current account is learned from the site's own archive feed on an own-work page. Moodboards/profiles are checked against that account ID. Export operations use a fixed GET allowlist. There is no generic URL-fetch or write-API facility. API calls are spaced at least one second apart; at most two images are fetched/saved concurrently. Metadata HTTP 403/429/5xx responses use exponential backoff and stop after three attempts. Image-download errors expose Chrome error categories rather than exact HTTP status: forbidden and server/network failures receive the same bounded backoff. Session expiry and recognized challenge responses stop immediately. No polling or alarms start exports.

If Midjourney shows a CAPTCHA, account warning, or ban notice, stop and resolve it yourself. Do not repeatedly restart a failed export.

## Known limits

- Private API shapes can change. See [docs/ENDPOINTS.md](docs/ENDPOINTS.md) for observations and verification status.
- A maximum-count or date subset can omit parents. `parent_gaps` explicitly reports them. The extension does not fetch arbitrary job IDs or crawl other accounts to fill gaps.
- Child kinds without a recognizable API type remain `other`; the feed's selected/shown flags are never treated as lineage.
- Profile/board codes are included only when the API or an exported job reference exposes them. Missing versions/codes stay null or empty.
- Board images hosted outside the permitted CDN are reported but not downloaded.
- Likes are limited to selected own work, not every liked image from other creators.
- Video media export is not implemented and is reported as a failure.
- Prompts retain multi-prompt text and weights. Unknown flags are preserved separately; this is a parser, not a Midjourney command validator.
- The partial schema viewer is for local debugging. It shows types and field names, not response bodies or authentication headers.

## Repository layout and local development

```text
manifest.json            Load this repository root as the unpacked extension
src/                     Service worker, isolated controller, in-page chooser, page-origin API bridge
src/generated/core.js    Checked-in classic-script bundle; do not edit by hand
lib/                     Pure parser, normalization, selection, scheduling and path validation
ui/                      Popup and offscreen document
scripts/                 Dependency-free build/check and offline export verifier
tests/                   Synthetic unit and controller regression tests; no network
docs/                    Endpoint observations, architecture, and sanitized test evidence
viewer/                  Local read-only viewer for finished exports (Python; not part of the extension)
.github/workflows/       Offline CI checks
```

To browse a finished export, use the local viewer in [`viewer/`](viewer/README.md): `cd viewer && python3 -m mjviewer /path/to/mj_export_folder_or.zip`, then open `http://localhost:8765/`. It needs only Python 3.9+, reads the export without modifying it, and makes no network requests. It is not part of the extension and is not included in the distribution ZIP.

With Node 20+ already installed, no package installation is required:

```sh
npm run build
npm run check
npm test
python3 scripts/verify-export.py /absolute/path/to/mj_export_folder
```

After edits to `lib/`, rebuild before reloading Chrome. The generated core is committed so a fresh clone can be loaded directly; CI rejects stale output. Version numbers in `manifest.json` and `package.json` must match.

Personal exports (`test-exports/`), the original account-specific `TEST_REPORT.md`, and `test-verification.json` are deliberately ignored by Git and retained locally. Do not force-add them or commit browser HAR files. The shareable acceptance summary is [docs/TESTING.md](docs/TESTING.md).

## Share with designers

Use the generated `dist/mj-export-extension-0.4.0.zip`, not a ZIP of the entire working folder: the working folder may contain private ignored exports and Git history.

The distribution contains the ready-to-load manifest and bundled scripts, plus source, tests and documentation. Designers do not need Node, Python, npm, or a build step. They should unzip it, keep the extracted `mj-export-extension` directory in a permanent location, open `chrome://extensions`, enable Developer mode, click **Load unpacked**, and select that extracted directory containing `manifest.json`. Chrome does not load this ZIP directly.

The development folder `~/Projects/mj-export-extension` is also directly loadable. Rebuild only after changing library source. To regenerate a clean distribution as a developer, run `npm run package` (Node and Python required for packaging, not installation). No export starts on installation. The account-risk and coverage limits above apply to every designer too.
