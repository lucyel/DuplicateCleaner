# Duplicate Cleaner

A local Windows desktop app written in Python with PySide6. Scan one or more folders, review exact duplicates, and **manually check the individual files you want to send to the Windows Recycle Bin**. No file is selected automatically.

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

1. Click **Add folder** for each folder you want to compare. Subfolders are included by default; uncheck **Include subfolders** to scan only the explicitly selected folders. In **Excluded subfolders**, click **+ Exclude…** to choose a subfolder inside an added folder. Both scan tools skip that folder and everything inside it, even if it is also added as a scan root. Select exclusions and click **Remove** to include them again. Removing a scan folder clears exclusions that no longer belong to any remaining root. Exclusion changes apply to the next scan; existing results stay available.
2. Click **Scan for duplicates**. The status shows the current stage, files processed, bytes read, and current path. Cancel stops after the current read or Windows operation returns.
3. Expand a duplicate group and click a file row to show its details: name, type, full path, exact size, dates, verified SHA-256, and recycling-selection status. The details text can be selected and copied. Resize the divider between the list and details as needed. Selecting a row does not check its recycling checkbox.
4. Check only the files you want to recycle. You may select **every copy in a group**; no copy is kept automatically. Leave a file unchecked only if you want to keep it. To check all verified duplicates in one exact folder, highlight one of its file rows and use **Select this folder's duplicates**, or right-click the row and choose **Select all duplicates in this folder**.
5. Click **Recycle selected files**. The confirmation warns when every copy in a group is selected. Its details list exactly which files are selected and which copies, if any, will remain. Cancel is the default choice.
6. Review the cleanup report, then choose your next batch from the remaining results without rescanning. Successfully recycled files disappear from the list; groups disappear when fewer than two copies remain. Unfinished groups with at least two copies stay listed, including after cancellation. Skipped files are included in the report even if their group is no longer listed. All checkboxes reset after each batch so nothing is selected automatically.

Use the **All, Selected, Videos, Images, Archives, Documents, Audio, and Other** tabs to filter the duplicate results. **Selected** shows every complete duplicate group containing at least one checked file, including its unchecked copies so you can see what will remain. Archives includes ZIP, RAR, and 7z; Documents includes PDFs, text, spreadsheets, and presentations. File-type categories use filename extensions, ignoring capitalization; unknown extensions and files without an extension appear under Other. These tabs do not change scanning: identical contents still match across different names and extensions. A file-type tab may show just one matching file while its other copies are in other tabs; group totals and potential savings always refer to the entire group.

Checked files stay selected when switching tabs. The selection count and recycling confirmation warn when selected files are hidden by the current tab. Recycling includes checked files from **all tabs**, and **Clear file selection** clears all of them. Your active tab stays in place when the remaining results refresh after cleanup.

### Filtering duplicate groups

Click the **magnifying-glass icon** beside the theme icon to show the filters; they are hidden on launch. Click it again to hide the panel while keeping the applied filters. Use **Filename**, **File path**, and **Folder path** above the results for case-insensitive text matches. Folder path searches the file's parent path, without its filename. Enter optional inclusive minimum and maximum **File size** values in B, KiB, MiB, or GiB; decimals are accepted and blank bounds mean no limit. For an exact size, use the same minimum and maximum.

Click **Apply** or press Enter in a field. A group is shown when at least one file meets every filled criterion. Its other copies remain available for comparison and selection; existing file-type tabs still restrict which file rows are shown. **Clear filters** removes these criteria. Invalid sizes leave the previous filter unchanged. Filters use scan-time metadata, do not change files, and are not saved in session files.

Filtering preserves checked files, including files in hidden groups. The selection count and recycling confirmation report hidden checks. Use **Clear file selection** to uncheck files across all groups and filters.

A group containing checked files has an amber highlight and a checkmark beside its title, even when collapsed. The marker disappears when the group's last checked file is unchecked. This follows selections made in either the list or the preview.

The top summary shows the number of currently visible groups and their potentially recoverable size. These totals update when applying or clearing filters or switching tabs. Recovery estimates include all copies in each matching group, even when a file-type tab hides some copies.

Folder selection is additive: it keeps files already checked elsewhere and includes matching results hidden in other file-type tabs. It selects only verified duplicates in the exact parent folder, not unique files or duplicates in subfolders. The status line reports how many new files were checked and how many selected files from that folder are hidden by the current tab. Run the command again safely if needed; no file is recycled until you use the normal recycling confirmation.

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

Selecting an individual file in the results switches to the two-file viewer: File A is the selected file, and File B is another copy from its complete group. Each pane also has a **Select for recycling** checkbox synchronized with the file list. Groups with more than two copies have a file chooser above each pane. Choosing the other pane's file swaps the pair, so the two panes always refer to distinct paths. Hover over a chooser entry or the shortened path below the image to see its full location. Checking a file only changes the selection; recycling still requires the normal confirmation.

Supported static images are decoded from each file, with embedded orientation applied. Scroll over an image to zoom, drag to pan, and use **Fit** or **100%** to reset its view. Images above 24 megapixels use a reduced preview labeled with their original dimensions; 100% is disabled for those previews. Decoding has a 128 MiB allocation limit and a ten-second timeout. Formats that cannot be decoded within those limits can still be opened externally. Videos and documents use available Windows thumbnails; other unsupported files show an icon and **Open** button. There is no embedded video playback or document page navigation.

Drag the divider to give the preview more room, use **Hide preview** to reclaim space, or expand **File details** for the existing metadata. In smaller windows, scroll the Duplicate files page to reach the full preview and cleanup controls. Previewing and changing the displayed pair never check files for recycling. Each pane reports its file's current checkbox state. Images load in background helper processes; changing selection discards stale loads. Files that changed or disappeared are rejected when their preview is loaded. Scanning, loading results, and recycling clear previews before refreshing the list.

## What counts as identical?

The scanner compares ordinary file contents, including metadata embedded in those contents. Names, folder locations, creation dates, modification dates, and filesystem permissions do not have to match. A renamed copy qualifies; a resized image, re-encoded video, or document with different embedded metadata does not.

Candidates pass through all of these stages:

1. Same byte length (only a filter).
2. Matching SHA-256 signatures of samples from the beginning, middle, and end (only a filter).
3. Matching SHA-256 hashes of the **entire** file.
4. Direct **byte-for-byte comparison**, including an end-of-file check.

Matching hashes alone never establish duplication. Even an artificial hash collision is split into separate groups by the final comparison. Empty files can form duplicate groups but recover zero bytes.

Reads use 1 MiB chunks, with 64 KiB samples. File contents are not loaded wholesale into memory. Reads are sequential rather than aggressively parallel to avoid thrashing hard drives. Memory still grows with the number of discovered files and displayed results. Scanning hundreds of GB is an intended use case, not a measured performance claim; timing depends on storage, file count, and duplication.

## Safety and limits

- Scanning is read-only. There is no automatic selection, permanent-delete command, or permanent-delete fallback.
- Overlapping folder selections are deduplicated by filesystem identity.
- Symbolic links, junctions, other reparse points, offline/cloud-only files, and **all files with multiple hard links** are skipped conservatively.
- Files with named NTFS alternate data streams are excluded during content verification. This includes files with a `Zone.Identifier` stream, often attached to downloads. Extra streams are not compared, so such files must not be recycled as verified duplicates. The reason appears in **Skipped files / errors**. The app does not remove these streams.
- Files without a reliable filesystem identity, inaccessible files, and files detected changing during verification are not accepted as duplicates.
- When a copy is left unchecked, each selected file is compared byte for byte against it. The unchecked copy stays open under a Windows read lock that blocks writes and deletion throughout that group's cleanup. The selected file also blocks writes while being checked and recycled.
- When **every copy is selected**, one selected file serves as the temporary comparison reference and is recycled last. Its handle blocks writes throughout the operation while permitting recycling. Every other file is compared byte for byte against it; the reference itself can be recycled only after at least one comparison succeeds. A group with no reverified match is skipped.
- Identity, size, timestamps, path safety, and alternate streams are checked again immediately before every Windows Shell operation, including recycling the last copy. If the comparison reference is inaccessible or changed, that group's cleanup is skipped; the app does not silently choose different files to delete.
- Windows `IFileOperation` requests recycling, disables connected-file operations, and uses a native callback to veto permanent deletion. A successful result must report a Recycle Bin destination. Unsupported locations, disabled/full bins, or other Windows errors are reported without a deletion fallback.
- These checks are not a transactional filesystem snapshot. Windows recycles by path: another process that deliberately renames/replaces paths in the small interval after the final callback can still race the operation. Avoid scanning folders actively being modified or synchronized. The app is not designed to defend against malicious concurrent filesystem changes or kernel-level tools.
- Do not clean application installations or system directories merely because their files have matching contents. Different applications can require identical files at different paths. Choose personal data folders and review paths before confirming.
- Group savings estimate the logical bytes recovered by keeping one copy. The selected-file total includes every checked file, including the last copy if selected. Compression, sparse files, and filesystem deduplication can make actual disk savings smaller.
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
- `duplicate_cleaner/windows_trash.py`: Windows Shell recycling and permanent-delete veto.
- `duplicate_cleaner/gui.py`: desktop interface and background workers.
- `duplicate_cleaner/thumbnails.py`: hover previews, Windows thumbnail extraction, and the isolated preview helper.
- `Build Portable EXE.bat` and `requirements-build.txt`: reproducible single-file Windows packaging.
- `tests/`: scanning, cleanup, GUI, and optional Windows integration regression tests.

The app uses only PySide6 and pywin32 beyond Python's standard library. It makes no network requests during scanning or cleanup.
