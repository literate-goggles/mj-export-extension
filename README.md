# MJ Export

Manifest V3 Chrome extension for exporting the logged-in user's own Midjourney work to a local folder. No backend, telemetry, remote code, CDN scripts, npm dependencies, or ZIP library.

## Account risk and coverage

**This is experimental, unofficial software, not a guarantee of a complete backup or a ban-safe workflow.** Midjourney's [Terms of Service](https://docs.midjourney.com/hc/en-us/articles/32083055291277-Terms-of-Service), checked 2026-10-30, prohibit automated access and reserve suspension/ban rights. Read-only behavior, low request rates, and owning the exported work do not exempt this extension. The terms also restrict accessing the service to develop or offer competing products/services, relevant to commercial downstream use. Obtain written permission covering your intended export/use before running it on a valuable account. Neither Scan nor Export is risk-free: both make automated API requests.

| Content | What the extension actually covers |
|---|---|
| Personal image generations | Jobs returned by the observed own `/api/imagine` feed, with cursor pagination and the selected scope. Completeness across every account/history type has not been established. Videos are unsupported; deleted/trashed/hidden/legacy jobs are not independently enumerated or guaranteed. |
| Moodboards | Current own boards and their current reference images returned by `/api/moodboards`. Turn **Small test** off to include all returned boards. No historical versions or deleted boards are retrieved. |
| Generations made using a moodboard | Exported as ordinary jobs from the own-job feed. `moodboards.jsonl` includes `generated_job_ids` derived from explicit personalization references on selected jobs; `codes` preserves observed historical codes. No separate per-board generation-history endpoint is queried. An empty list does not prove a board was never used. |
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
- **Open export folder** reveals the metadata file in its folder. Chrome's own download-directory settings control the base destination. If Chrome asks where to save every download, those prompts still apply.

Only one run may be active across tabs. Run settings and errors appear in the popup and on-page panel. An unfinished run restores its saved settings; a fresh install or completed run shows the new defaults; its **Resume** button uses the saved run's settings. **Export** uses the current controls and rejects a changed scope rather than overwriting the saved run.

### Exact sequence after permission to run

1. Reload the extension card in `chrome://extensions`, then reload your signed-in Organize page. The repository root is still the Load unpacked folder.
2. The defaults are **All jobs**, **Include images** on, and **Small test** off. An unfinished run retains its previous settings for safe resumption.
3. **Scan is optional.** It counts the chosen scope and estimates bytes, but does not create a cached export or guarantee completeness. Export reads the feed again.
4. Because the earlier small test has saved progress, use **New export** when changing to All jobs or another scope. This starts immediately in a separate folder. For the same saved scope, use **Export / resume** on the page or **Resume** in the popup.
5. Keep the tab open. Use **Pause**, then **Resume** as needed. When complete, choose **Open export folder** and inspect `export_meta.json` for failures and parent gaps.

There is no need to click Export again after a completed run.

## Output

`Downloads/mj_export_<username>_<YYYYMMDD>/` (new explicit runs add a timestamp suffix):

- `jobs.jsonl`: one JSON object per selected job: ID, creation time, original command, prompt with flags removed, parsed flags, parent ID/index, kind, relative image paths, source URLs, dimensions. Extra source type and personalization-reference fields preserve provenance.
- `moodboards.jsonl`: board ID/codes, name, version when exposed, creation time, reference-image URLs/files/dimensions, and `generated_job_ids` linking selected own jobs.
- `profiles.jsonl`: profile IDs/codes, names, major versions, and aggregate visible ranking counts. No individual ratings are collected.
- `likes.jsonl`: nonempty `image_likes` from the selected own jobs.
- `images/`: original fetched bytes, without resizing or re-encoding.
- `export_meta.json`: extension version, export time, username, selected scope, counts, status, failures, unresolved parents and limitations.

Metadata-only runs have empty image-file lists. A stopped run still tries to write partial metadata and a failure report. A successful metadata-only run does not prove image availability.

Folder output avoids the 2 GB ZIP limit. Individual images are held in memory briefly; the whole image archive is never assembled in RAM. Metadata files are generated using an offscreen document. API requests happen in the Midjourney page context. That context authorizes image URLs from observed own records; `chrome.downloads` saves those URLs directly using the browser session, avoiding cross-origin JavaScript response restrictions. Metadata uses offscreen local blobs with a generic MIME type so Chrome preserves the requested JSONL filenames.

## Resume and ownership

`chrome.storage.local` keeps cursor/checkpoint, discovered and completed IDs, normalized metadata, and completed download IDs/paths. `unlimitedStorage` accommodates large metadata sets. On each explicit resume the selected own-feed scope is re-read to establish ownership in the current page session. Already completed files are checked against Chrome's download records and skipped; unfinished files are overwritten at their deterministic paths. No historical cursor is blindly trusted after an account change.

Resume depends on Chrome retaining its download history. If a finished file is moved/deleted or its download record disappears, it may be downloaded again. A crash between download completion and saving progress may also cause a safe overwrite. Clearing extension storage removes resume state but does not delete exported files. The export folder is local data and should be shared deliberately.

## Privacy and safeguards

Host permissions are limited to `https://www.midjourney.com/*` and `https://cdn.midjourney.com/*`. The extension never reads session credentials or browser cookies. No account data is uploaded anywhere. It only reads same-origin API responses and saves selected metadata and image bytes locally.

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
src/                     Service worker, isolated controller, page-origin API bridge
src/generated/core.js    Checked-in classic-script bundle; do not edit by hand
lib/                     Pure parser, normalization, scheduling and path validation
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

Use the generated `dist/mj-export-extension-0.3.1.zip`, not a ZIP of the entire working folder: the working folder may contain private ignored exports and Git history.

The distribution contains the ready-to-load manifest and bundled scripts, plus source, tests and documentation. Designers do not need Node, Python, npm, or a build step. They should unzip it, keep the extracted `mj-export-extension` directory in a permanent location, open `chrome://extensions`, enable Developer mode, click **Load unpacked**, and select that extracted directory containing `manifest.json`. Chrome does not load this ZIP directly.

The development folder `~/Projects/mj-export-extension` is also directly loadable. Rebuild only after changing library source. To regenerate a clean distribution as a developer, run `npm run package` (Node and Python required for packaging, not installation). No export starts on installation. The account-risk and coverage limits above apply to every designer too.
