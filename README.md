# Duplicate Cleaner

A local Windows desktop app written in Python with PySide6. Scan one or more folders, review exact duplicates, and **manually check the individual files you want to send to the Windows Recycle Bin**. No file is selected automatically.

## License

The original source code and documentation in this repository are licensed under the [MIT License](LICENSE), copyright (c) 2026 lucyel. You may use, modify, and redistribute them, including commercially, provided you retain the copyright and license notice. The software is provided without warranty.

Third-party dependencies retain their own licenses. In particular, [PySide6/Qt for Python](https://doc.qt.io/qtforpython-6/) offers LGPL/GPL and commercial licensing options; this project's MIT license does not relicense Qt. Python, pywin32, and other components bundled in an executable also retain their applicable notices and terms. [PyInstaller's license exception](https://pyinstaller.org/en/stable/license.html) permits packaging applications under other licenses.

When distributing a portable executable, include this project's `LICENSE` and the applicable third-party license texts and notices. For LGPL components, also meet the applicable source-availability and modification/relinking requirements described in the [Qt LGPL license](https://doc.qt.io/qt-6/lgpl.html). Adding this source license does not certify the existing one-file executable's distribution compliance; a complete bundled-component review remains necessary before publishing a binary release.

See [the preliminary IP search](IP_REVIEW.md) for related duplicate-detection patent publications and the limits of that search. The MIT license does not establish patent clearance.

## Start the app

### Portable Windows executable

Double-click **`dist\DuplicateCleaner.exe`**. You can copy that single file to another folder, a USB drive, or another 64-bit Windows 10/11 computer. Python, setup scripts, and the source folder are not required on the destination computer. Run it as a normal user, not as administrator.

This is a PyInstaller one-file bundle. It unpacks its included runtime into a temporary folder when started and removes it after a normal exit, so startup can take a few seconds. It needs a writable Windows temporary folder. The theme preference is saved for the current Windows user. Scan results are not saved automatically; use **Save session** when you want to continue later. This is portable in the sense of requiring no installation, not leaving zero traces. See [PyInstaller's one-file behavior](https://pyinstaller.org/en/stable/operating-mode.html#how-the-one-file-program-works).

The executable is unsigned. Windows or security software may warn about an unfamiliar executable; use only a copy you trust and follow your organization's security policy. Thumbnail availability still depends on Windows' installed preview handlers. Recycle Bin restrictions are unchanged.

To rebuild after changing the code, run **`Build Portable EXE.bat`**. The build uses the existing `.venv`, installs the pinned tools from `requirements-build.txt`, and writes `dist\DuplicateCleaner.exe`. Close the executable before rebuilding it. Build output and cache stay under `build`; the script limits its library search path to avoid picking up unrelated development-tool DLLs. Internet access may be needed to install build dependencies; normal app use requires no network connection.

### Run from Python source

On this workspace, dependencies have already been installed in `.venv`:

**Double-click `Launch Duplicate Cleaner.bat`.**

For a fresh checkout on another computer:

1. Install 64-bit Python 3.10 or newer with Python on PATH. Windows 10 or 11 is the supported target.
2. Double-click `Setup.bat` to create a project-local virtual environment and install dependencies. Internet access is needed for setup only.
3. Double-click `Launch Duplicate Cleaner.bat`.

To launch from PowerShell and see diagnostic output:

```powershell
.\.venv\Scripts\python.exe -m duplicate_cleaner
```

The app needs no administrator privileges. Do not elevate it to scan protected system folders. If setup encounters a certificate error on a managed network, configure pip with your organization's trusted CA certificate rather than disabling TLS verification.

Use the **sun/moon icon** in the top-right corner to switch between light and dark colors without interrupting your work. Hover over it to see which theme it will switch to. The preference is saved for your Windows user and restored on the next launch. The results list, file details, app dialogs, and thumbnail popups follow the chosen theme. The native Windows folder picker and external file-opening apps use their own appearance settings.

## Use

1. Open **Scan location** and click **Add folder** for each folder you want to compare. Subfolders are included by default; uncheck **Include subfolders** to scan only the explicitly selected folders. In **Excluded subfolders**, click **+ Exclude…** to choose a subfolder inside an added folder. Both scan tools skip that folder and everything inside it, even if it is also added as a scan root. Select exclusions and click **Remove** to include them again. Removing a scan folder clears exclusions that no longer belong to any remaining root. Exclusion changes apply to the next scan; existing results stay available.
2. Choose the matching checkboxes in **Search criteria**, then click **Scan for duplicates**. The status shows the current stage, files processed, bytes read, and current path. Cancel stops after the current read or Windows operation returns.
3. Expand a duplicate group and click a file row to show its details: name, type, full path, exact size, dates, verified SHA-256, and recycling-selection status. The details text can be selected and copied. Resize the divider between the list and details as needed. Selecting a row does not check its recycling checkbox.
4. Check only the files you want to recycle. You may select **every copy in a group**; no copy is kept automatically. Leave a file unchecked only if you want to keep it. To check all listed matches in one exact folder, highlight one of its file rows and use **Select this folder's duplicates**, or right-click the row and choose **Select all duplicates in this folder**.
5. Click **Recycle selected files**. The confirmation warns when every copy in a group is selected. Its details list exactly which files are selected and which copies, if any, will remain. Cancel is the default choice.
6. Review the cleanup report, then choose your next batch from the remaining results without rescanning. Successfully recycled files disappear from the list; groups disappear when fewer than two copies remain. Unfinished groups with at least two copies stay listed, including after cancellation. Skipped files are included in the report even if their group is no longer listed. All checkboxes reset after each batch so nothing is selected automatically.

Use the **All, Selected, Videos, Images, Archives, Documents, Audio, and Other** tabs to filter the duplicate results. **Selected** shows every complete duplicate group containing at least one checked file, including its unchecked copies so you can see what will remain. Archives includes ZIP, RAR, and 7z; Documents includes PDFs, text, spreadsheets, and presentations. File-type categories use filename extensions, ignoring capitalization; unknown extensions and files without an extension appear under Other. These result tabs do not change scanning: the checked options in **Search criteria** determine which files can form a group. A file-type tab may show just one matching file while its other copies are in other tabs; group totals and potential savings always refer to the entire group.

Checked files stay selected when switching tabs. The selection count and recycling confirmation warn when selected files are hidden by the current tab. Recycling includes checked files from **all tabs**, and **Clear file selection** clears all of them. Your active tab stays in place when the remaining results refresh after cleanup.

### Search criteria and scan location

The top tabs are **Duplicate files**, **Search criteria**, **Scan location**, and **Empty folders**. Folder roots, excluded subfolders, and recursion settings live in **Scan location**. Scan, save-session, and load-session actions remain available below every tab.

**Search criteria** applies before a scan; all checked criteria must match. **Same file size**, **Same file hash (SHA-256)**, and **Byte-for-byte comparison** are independent controls. All three start checked to retain the original size → sample → SHA-256 → byte verification scan. Hash-only mode does not compare every byte; byte-only mode compares bytes without calculating main-file hashes. Hash or byte matching always requires equal main-file sizes, so size tolerance is available only when both are off.

Additional options:

- **Same file name / extension**, **Similar file names**, and **Ignore "Copy" part of filename**. Names include the extension. Copy normalization removes common prefixes such as `Copy of ` and suffixes such as ` - Copy (2)`, not words such as `copyright`.
- **Bytes tolerance** accepts an inclusive size difference in main-file bytes, excluding NTFS extra streams.
- **Same created / modified date/time** compares filesystem timestamps. Each has **Match date only**, using the local calendar date. Creation-time matching skips files if their filesystem does not expose creation time.
- **Same drive** compares filesystem volume identity.
- **Same folder name** compares the immediate parent folder by default. **Match full folder name** compares the full parent path. **Match depth from top** compares the first N folder components below the drive/share and takes precedence over full-path matching. **Match from search root** instead uses the relative folder path (or its first N components); overlapping roots use the most specific configured root, independent of root order.
- **Ignore duplicate groups within the same folder** suppresses groups whose files all share one parent folder. Cross-folder groups retain their same-folder copies.
- **Is case sensitive** applies to filenames, extensions, folder paths, and Copy markers. **Similar text — tolerance** counts character insertions, deletions, and substitutions (default 3; zero requires exact text). Similarity uses complete filenames after optional Copy normalization.
- **Same NTFS extra data** requires matching extra stream names, sizes, and SHA-256 hashes.

Size and text tolerances must hold between every pair in a group. Files are grouped deterministically without appearing in multiple groups; loose matches do not form chains beyond the selected tolerance. Similar-name scans can require more comparisons than exact matching.

Uncheck both **Same file hash** and **Byte-for-byte comparison** to search by names, sizes, or other metadata without reading main-file contents. At least one primary criterion is required; text/folder modifiers alone do not enable scanning. Metadata-only groups are labeled **Possible match — contents not verified**. Hash-only groups are labeled **Hash match — bytes not verified**. Their savings are estimates. Before recycling, the app still compares contents and extra streams against a comparison copy and skips files that fail those checks. Filename/size matching never authorizes deleting different contents.

Version 2 session files preserve the verification mode, including byte-only matches without a hash. This app still reads version 1 sessions; older apps cannot read version 2 files. Search checkbox choices apply to the next scan and reset to the defaults when the app restarts.

### Filtering duplicate groups

Click the **magnifying-glass icon** beside the theme icon to show the filters; they are hidden on launch. Click it again to hide the panel while keeping the applied filters. Use **Filename**, **File path**, and **Folder path** above the results for case-insensitive text matches. Folder path searches the file's parent path, without its filename. Enter optional inclusive minimum and maximum **File size** values in B, KiB, MiB, or GiB; decimals are accepted and blank bounds mean no limit. For an exact size, use the same minimum and maximum.

Click **Apply** or press Enter in a field. A group is shown when at least one file meets every filled criterion. Its other copies remain available for comparison and selection; existing file-type tabs still restrict which file rows are shown. **Clear filters** removes these criteria. Invalid sizes leave the previous filter unchanged. Filters use scan-time metadata, do not change files, and are not saved in session files.

Enter multiple comma-separated terms in any text field. Plain terms must be present; prefix a term with **-** to exclude it. For example, **photo, holiday, -backup** requires both “photo” and “holiday” and excludes “backup,” ignoring capitalization. All terms across all fields apply to the same file; blank terms are ignored. Matching groups retain all their copies for comparison, including copies that do not meet the filter.

Spaces stay inside a term, so **summer photos** searches that whole phrase. Quote a term containing a comma, such as **"photos, 2026"**. To search for a literal leading minus or plus, prefix it with **+**, such as **+-draft**. Click **Apply** or press Enter to apply edits; malformed terms leave the previous results unchanged.

Filtering preserves checked files, including files in hidden groups. The selection count and recycling confirmation report hidden checks. Use **Clear file selection** to uncheck files across all groups and filters.

A group with some files checked has an amber highlight and a checkmark beside its title, even when collapsed. When every file is checked, the group turns red and shows a warning icon to indicate that no listed copy will remain. Unchecking one file changes it back to amber; unchecking every file removes the highlight. These indicators follow selections made in either the list or the preview and remain visible on the current group.

The top summary shows the number of currently visible groups and their potentially recoverable size. These totals update when applying or clearing filters or switching tabs. Recovery estimates include all copies in each matching group, even when a file-type tab hides some copies.

Folder selection is additive: it keeps files already checked elsewhere and includes matching results hidden in other file-type tabs. It selects only listed matches in the exact parent folder, not unique files or duplicates in subfolders. The status line reports how many new files were checked and how many selected files from that folder are hidden by the current tab. Run the command again safely if needed; no file is recycled until you use the normal recycling confirmation.

The remaining list is kept for the current app session and can be written to a `.dupsession` file with **Save session**. Files are rechecked before every cleanup, so changes since the scan still block unsafe recycling. Scan again to discover newly added or restored files, or to refresh changed files.

### Saving and loading sessions

- **Save session…** stores the remaining duplicate groups, scanned folders, excluded subfolders, scan statistics, skipped-file report, active tab, and every checked file. Older session files load with no exclusions. The `.dupsession` file contains absolute paths and scan metadata, including hashes, but never file contents or thumbnails. Keep it private if your folder names are sensitive.
- **Load session…** validates every saved path, filesystem identity, size, and timestamp in the background without hashing all file contents again. Missing, changed, linked, unsafe, or incomplete groups are removed and reported under **Skipped files / errors**. The current results remain untouched if loading fails or is cancelled.
- After validation, choose **Load with nothing checked** (the safe default) or **Restore checked files**. The dialog reports how many saved selections remain valid. If checks are restored, their complete groups appear in the **Selected** tab.
- Saving is manual. Save again after cleanup or changing selections when you want the session file to contain the latest state. Session writes use a temporary file and replace the destination only after a complete successful write.

A loaded session is a snapshot of an earlier scan. File contents are still reverified byte for byte before recycling, exactly as with results kept open in memory. Do not load session files from sources you do not trust, and scan again when you need to discover new files or updated duplicates.

Files can be restored using the Windows Recycle Bin. **Space is not freed until you empty the Recycle Bin yourself.** This app never empties it.

### Empty folders

The **Empty folders** tab is a separate cleanup tool. It uses the folders in the left panel and the **Include subfolders** setting, but it does not scan file contents, alter duplicate results, or share their checkboxes. Click **Scan empty folders**, review the full paths, and manually check only the folders you want to recycle. Nothing is checked automatically.

A folder qualifies only when it contains no entries at all: no files and no child folders, including hidden entries. The explicitly selected scan roots are never offered for deletion. Links, junctions, reparse points, unsafe paths, inaccessible folders, and paths without reliable filesystem identity are skipped and reported in this tab's own error list. With **Include subfolders** off, only direct child folders of each selected root are checked.

Before each checked folder is sent to the Windows Recycle Bin, the app confirms that its path still refers to the same folder and that it is still empty. A changed or newly nonempty folder is skipped. Successfully recycled paths disappear; unchecked and skipped paths remain listed, and every checkbox clears after the batch. Scan again to refresh stale paths and discover parent folders that became empty after their last child was recycled. Empty-folder results are not stored in duplicate `.dupsession` files.

### Inspecting files

Use the outlined sidebar icon at the right edge of the sidebar to hide the folder selection, scan, and session controls and give the results more room. The icon stays visible with a **Show sidebar** tooltip so you can restore the panel to its previous width. Your folders, exclusions, results, and checked files are kept when toggling it.

- **Single-click a group row:** see every file in that group in a preview grid. **Single-click a file row:** compare it with another copy. Expand **File details** for metadata. Sizes, modification dates, and hashes are labelled as scan-time information; a changed or missing file is flagged. The creation date is read from the current file.
- **Double-click a file row:** open it with its default Windows application. The app refuses to open a file that no longer matches its recorded identity and timestamps. Double-clicking a group or a recycling checkbox does not open a file. As in Explorer, opening an executable or script can run it; open only files you trust.
- **Hover over a file:** after a short delay, see a thumbnail. Windows supplies previews for formats with installed thumbnail handlers, including supported images, videos, and documents. Common image formats also have a Qt fallback. Unsupported files show a file icon and an unavailable-preview message; the app never launches a file to generate its thumbnail.
- **Open file location:** right-click a file row and choose this option to open its folder in Explorer, or use the button above the results.

Thumbnail generation runs in a separate, hidden helper process with a six-second deadline. Leaving the row, scrolling, starting another operation, or closing the app dismisses the preview and stops its helper. Recent thumbnails are cached in memory (up to 64 files), and stale results are discarded. Windows may update its own thumbnail cache. Preview handlers are supplied by Windows or installed software; the helper isolates crashes and delays but is not a security sandbox.

### Previewing groups and comparing copies

Click a duplicate group to display all its files in the **Group preview** grid on the right, including copies outside the current file-type filter. Each card has a checkbox for selecting that file for recycling, plus its preview, filename, size, and selection state; hover to see its full path or preview error. You can check files here while the group remains collapsed. Preview checkboxes and the existing file-list checkboxes stay synchronized, and **Clear file selection** clears both. Scroll the grid for larger groups. Gallery images are reduced to at most 320 pixels on their longest edge, loaded two at a time as they enter view, and released when they leave view. Group rows omit verification and potential-savings labels; file rows keep their size and modification date.

Selecting an individual file in the results switches to the two-file viewer. Clicking either displayed file keeps its current left/right position; selecting the group and returning to either file also preserves the pair. Selecting a file outside the current pair compares it with the current left-hand copy, arranged in group order. Each pane also has a **Select for recycling** checkbox synchronized with the file list. Groups with more than two copies have a file chooser above each pane. Choosing the other pane's file explicitly swaps the pair; that choice is preserved when clicking either displayed file or changing checkboxes. Hover over a chooser entry or the shortened path below the image to see its full location. Checking a file only changes the selection; recycling still requires the normal confirmation.

Supported static images are decoded from each file, with embedded orientation applied. Scroll over an image to zoom, drag to pan, and use **Fit** or **100%** to reset its view. Images above 24 megapixels use a reduced preview labeled with their original dimensions; 100% is disabled for those previews. Decoding has a 128 MiB allocation limit and a ten-second timeout. Formats that cannot be decoded within those limits can still be opened externally. Videos and documents use available Windows thumbnails; other unsupported files show an icon and **Open** button. There is no embedded video playback or document page navigation.

Drag the divider to give the preview more room, use **Hide preview** to reclaim space, or expand **File details** for the existing metadata. In smaller windows, scroll the Duplicate files page to reach the full preview and cleanup controls. Previewing and changing the displayed pair never check files for recycling. Each pane reports its file's current checkbox state. Images load in background helper processes; changing selection discards stale loads. Files that changed or disappeared are rejected when their preview is loaded. Scanning, loading results, and recycling clear previews before refreshing the list.

## Similar files

The **Similar files** tab has its own folders, exclusions, subfolder option, scan/cancel controls, results, and previews. Add folders inside that tab, choose **All**, **Images**, or **Videos**, choose **Strict**, **Balanced** (default), or **Broad**, and click **Scan similar files**. The media selector applies to the next scan. The existing scan location, search criteria, filters, checked files, cleanup, and saved sessions do not control or receive these results. Similarity results are read-only and are not included in saved duplicate sessions.

Image matching supports JPEG, PNG, BMP, WebP, and TIFF. It looks for resized, recompressed, or lightly edited copies, including copies with different filenames and byte sizes. Exact copies can also appear. Click a group to see every image in a scrollable gallery; select a file or double-click a gallery image to compare it with the reference using zoom, pan, Fit, and 100% controls. Larger images are preferred as group references; this does not establish which copy is the original or best quality.

Matching uses a 63-bit perceptual fingerprint of normalized grayscale image content. **Visual distance** counts differing bits relative to the group's reference: lower is closer, and zero does not mean identical files. Strict, Balanced, and Broad allow at most 4, 8, and 12 differing bits respectively, with similar aspect ratios required. Every candidate must match its reference directly; chains of progressively different images are not merged. Broader settings admit more false matches. These presets are heuristics, not calibrated confidence percentages.

Heavy crops, arbitrary rotation, substantial edits, and images with similar layouts but different details may produce missed or false matches. Nearly uniform images are skipped because they have too little visual detail. Only the first frame/page of animated or multi-page image formats is compared. Audio, document, and arbitrary binary similarity are not supported. Review the content before making any decisions about it.

Scanning uses the same file-identity and safe-path checks as duplicate discovery, with its own isolated image decoder, a ten-second per-image deadline, and a 128 MiB decoder allocation limit. Unsupported, unreadable, changing, or timed-out files are counted or reported under **Skipped files / errors**. Cancelling discards partial groups. Thumbnail previews load on demand; changing tabs releases displayed similarity previews while retaining results. No files are selected or recycled by this tab.

### Similar videos

Video matching supports MP4, M4V, MOV, MKV, AVI, and WebM containers when the bundled decoder supports their video codec. It targets near-complete visual copies with changed resolution, compression, frame rate, or container. Images and videos always form separate groups. Audio is not compared: videos with different soundtracks can appear together. Trimmed excerpts, reordered scenes, major crops, and speed changes are outside this version's target scope.

The scanner samples two nearby frames at each of 12 positions across the video, normalizes orientation and scale, and compares the sequence's visual fingerprints. Low-detail samples do not count as evidence. Strict, Balanced, and Broad require at least 11, 10, or 9 matching sections respectively, using frame-distance limits of 5, 9, or 12 bits. Matches must include at least two sections in every third of the timeline. Similar aspect ratios and durations are required; duration tolerances are 0.5%, 1.5%, or 3%, with a 0.25-second floor and 5-second ceiling. These are heuristic settings, not confidence percentages or exhaustive frame verification. Differences between sampled positions can be missed, and similar static imagery can produce false matches.

Click a video group to see every video's preview, duration, and resolution. Select a file to compare its sampled frames with the reference. The time selector shows paired approximate timestamps and visual distances for all 12 sections. The previews are reduced 192 × 108 samples, with zoom and pan; use **Open video** for full-resolution playback in the default app. Embedded video/audio playback is not included. Previewing or opening a changed file is rejected until it is scanned again.

Video metadata and decoding run in separate hidden FFmpeg processes, with two decoder threads, bounded output, a 15-second per-process deadline, and a 90-second sampling deadline per video. Decoding requests a 128 MiB single-allocation limit; this is not a total process-memory cap. Only local-file/pipe protocols and the supported container demuxers are enabled. Videos over seven days or 100 megapixels are rejected. Cancelling terminates the active decoder. Compressed sampled-frame sheets are retained with results at no more than 96 KiB per video; memory still grows with the number of scanned files.

Portable builds embed pinned FFmpeg/FFprobe 9.0.1 and upstream notices. For source runs, execute `.venv\Scripts\python.exe tools\fetch_ffmpeg.py` once; **Build Portable EXE.bat** also runs this checksum-verified setup. Installation needs network access if the archive is not cached. Scanning and previewing make no network requests. See [decoder provenance and license](tools/ffmpeg/README.md).

## What counts as identical?

The scanner compares ordinary file contents, including metadata embedded in those contents. Names, folder locations, creation dates, modification dates, and filesystem permissions do not have to match. A renamed copy qualifies; a resized image, re-encoded video, or document with different embedded metadata does not.

Candidates pass through all of these stages:

1. Same byte length (only a filter).
2. Matching SHA-256 signatures of samples from the beginning, middle, and end (only a filter).
3. Matching SHA-256 hashes of the entire main file contents. Named NTFS streams are hashed separately to identify metadata differences without hiding matching pictures or other files.
4. Direct **byte-for-byte comparison**, including an end-of-file check.

Matching main-content hashes alone never establish duplication. Even an artificial hash collision is split into separate groups by the final comparison. Files with empty main contents can form duplicate groups; any named-stream bytes still count toward their size.

Reads use 1 MiB chunks, with 64 KiB samples. File contents are not loaded wholesale into memory. Reads are sequential rather than aggressively parallel to avoid thrashing hard drives. Memory still grows with the number of discovered files and displayed results. Scanning hundreds of GB is an intended use case, not a measured performance claim; timing depends on storage, file count, and duplication.

## Safety and limits

- Scanning is read-only. There is no automatic selection, permanent-delete command, or permanent-delete fallback.
- Overlapping folder selections are deduplicated by filesystem identity.
- Symbolic links, junctions, other reparse points, offline/cloud-only files, and **all files with multiple hard links** are skipped conservatively.
- Duplicate groups match the main file contents byte for byte. **Exact match** means the extra stream names, sizes, and content hashes match too; **Metadata differs** means a stream is missing or different in another copy. The preview's **Show stream details** lists each file's stream names, sizes, and differences. Older saved sessions without stream hashes show **Metadata not checked — rescan**. Unreadable or changing streams are reported in **Skipped files / errors**.
- Recycling rechecks the main contents and extra streams byte for byte. When an unchecked comparison copy is kept, it may contain additional streams (such as Dropbox metadata): these are preserved and do not block recycling. Every stream on the selected file must also exist with matching bytes on that kept copy, except for explicitly accepted metadata differences described below. If every copy is selected, streams must match in both directions unless their differences were explicitly accepted.
- Differences in `Zone.Identifier` download metadata and `com.dropbox.attrs` Dropbox metadata can be accepted in the recycling confirmation. Its unchecked-by-default **Allow differences in … metadata for this batch** checkbox lists the exact stream names being accepted. This also supports selected files whose Dropbox metadata is missing or different in the kept copy. Metadata unique to a recycled file may remain only in the Recycle Bin; retained files stay unchanged. Consent applies only to that batch, is not saved, and never allows differences in the main contents or in other streams (including other `com.dropbox.*` names). Without consent, the affected selected files are skipped. No metadata is stripped or merged; selected files are recycled with their own streams.
- Files without a reliable filesystem identity, inaccessible files, and files detected changing during verification are not accepted as duplicates.
- When a copy is left unchecked, each selected file is compared byte for byte against it, including every named stream. The unchecked copy and its streams stay open under Windows read locks that block writes and deletion throughout that group's cleanup. The selected file and its streams also block writes while being checked and recycled.
- When **every copy is selected**, one selected file serves as the temporary comparison reference and is recycled last. Its handle blocks writes throughout the operation while permitting recycling. Every other file is compared byte for byte against it; the reference itself can be recycled only after at least one comparison succeeds. A group with no reverified match is skipped.
- Identity, size, timestamps, path safety, and alternate streams are checked again immediately before every Windows Shell operation, including recycling the last copy. If the comparison reference is inaccessible or changed, that group's cleanup is skipped; the app does not silently choose different files to delete.
- Windows `IFileOperation` requests recycling, disables connected-file operations, and uses a native callback to veto permanent deletion. A successful result must report a Recycle Bin destination. Unsupported locations, disabled/full bins, or other Windows errors are reported without a deletion fallback.
- These checks are not a transactional filesystem snapshot. Windows recycles by path: another process that deliberately renames/replaces paths in the small interval after the final callback can still race the operation. Avoid scanning folders actively being modified or synchronized. The app is not designed to defend against malicious concurrent filesystem changes or kernel-level tools.
- Do not clean application installations or system directories merely because their files have matching contents. Different applications can require identical files at different paths. Choose personal data folders and review paths before confirming.
- File sizes, size filters, scan totals, and group savings include the main content plus named-stream bytes. When copies have different stream sizes, potential group savings assume the largest copy is kept. This is an estimate before recycling safety checks; blocked files do not free space. The selected-file total includes every checked file, including the last copy if selected. Compression, sparse files, and filesystem deduplication can make actual disk savings smaller.
- Cancelling a scan discards partial results. Cancelling cleanup stops subsequent work but does not undo files already recycled. If an unexpected error stops cleanup before a report is available, the list is kept and marked as potentially out of date; inspect the Recycle Bin and rescan.

## Verification

Run scanner, cleanup, and GUI regression tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests create disposable fixtures only beneath the workspace's `.verification` directory. The default suite does **not** recycle real files. GUI tests use Qt's offscreen mode and save `.verification/gui-preview.png`. The symlink test skips when Windows does not permit creating symlinks.

To measure session-record serialization, cleanup error reporting, and GUI population/filtering with 6,000 synthetic records:

```powershell
.\.venv\Scripts\python.exe -m tests.benchmark_performance
```

This benchmark does not open or recycle scanned files. It reports median elapsed seconds, not disk-scan throughput or performance guarantees. Temporary preferences stay under `.verification`.

After building, test the packaged thumbnail helper (including changed-file rejection) without Python on its search path:

```powershell
$env:DUPLICATE_CLEANER_EXE = (Resolve-Path .\dist\DuplicateCleaner.exe).Path
.\.venv\Scripts\python.exe -m unittest tests.test_thumbnails.ThumbnailTests.test_portable_executable_renders_and_rejects_changed_files -v
Remove-Item Env:DUPLICATE_CLEANER_EXE
```

The optional Windows integration tests exercise the actual Shell recycling API, including native callback vetoes and recycling every copy in a group. They recycle only small, generated test files and leave them recoverable in the Recycle Bin:

```powershell
$env:DUPLICATE_CLEANER_RECYCLE_TEST = '1'
.\.venv\Scripts\python.exe -m unittest tests.test_windows_trash -v
Remove-Item Env:DUPLICATE_CLEANER_RECYCLE_TEST
```

Run those integration tests from a normal user terminal with Recycle Bin access. A restricted execution sandbox can deny bin access, in which case the app deliberately refuses the operation.

## Source layout

- `duplicate_cleaner/scanner.py`: discovery, staged comparison, progress, cancellation.
- `duplicate_cleaner/files.py`: file identity, path checks, Windows read locks, extra-stream checks.
- `duplicate_cleaner/cleanup.py`: selection validation and pre-recycle verification.
- `duplicate_cleaner/empty_folders.py`: independent empty-folder discovery, validation, and recycling.
- `duplicate_cleaner/similarity.py` and `similar_tab.py`: independent image-similarity scanning and read-only review.
- `duplicate_cleaner/video_similarity.py` and `video_review.py`: video fingerprints, matching evidence, and sampled-frame review inside Similar files.
- `duplicate_cleaner/windows_trash.py`: Windows Shell recycling and permanent-delete veto.
- `duplicate_cleaner/gui.py`: desktop interface and background workers.
- `duplicate_cleaner/thumbnails.py`: hover previews, Windows thumbnail extraction, and the isolated preview helper.
- `Build Portable EXE.bat` and `requirements-build.txt`: reproducible single-file Windows packaging.
- `tests/`: scanning, cleanup, GUI, and optional Windows integration regression tests.

The Python dependencies are PySide6 and pywin32. Similar video scanning additionally uses bundled FFmpeg/FFprobe executables. The app makes no network requests during scanning or cleanup.
