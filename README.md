# SpaceGoat Fisch Tournament

Windows x64 tournament server with OCR, an admin dashboard, and an OBS leaderboard.

## Live OCR reader

The live app now defaults to the standalone tool's **Tesseract + rolling history** reader: full-region screenshots, color-independent contrast, scale 2, PSM 6, and a 2-second interval. Set the interval, preprocessing, scale, PSM, executable path, or legacy RapidOCR reader under **Capture / OCR > Reader settings** while capture is stopped. The desktop window also exposes the capture interval for the next start. Use the same settings that worked in the standalone tool.

The first readable view after starting capture or a tournament transition establishes a baseline and excludes messages already visible. Bottom-up ordered overlap and 128 viewport hashes identify subsequent new rows, which feed the existing wrapped-message parser and scoring rules. Separate identical announcements remain separate catches. In best-effort mode, complete high-confidence catches also score without exact line overlap. A bounded recent-catch occurrence cache excludes already-visible announcements across changed wrapping; strict mode keeps no-overlap catches in review. Screenshots retain the fallback evidence. In-flight reads from an earlier tournament state are discarded.

The default minimum OCR confidence is 85% for Tesseract. Older configurations using the shipped 92% RapidOCR threshold migrate to 85%, with a `config.pre-tesseract.json` backup; custom thresholds and explicit backend choices are preserved. Confidence is calculated per physical row, so a strong neighboring message cannot hide a weak read. The minimum remains editable under Parser calibration.

Tesseract is detected automatically on this machine. Source installations on another computer need Tesseract with English data installed, or an explicit executable path. New Windows builds include the executable, runtime DLLs, English data, TSV configuration, and distribution notices. Existing executables must be rebuilt to include these changes.

For a laptop using Git pull: download the 64-bit Windows installer from the [UB Mannheim Tesseract page](https://github.com/UB-Mannheim/tesseract/wiki), run it with English language data selected, and keep the default `C:\Program Files\Tesseract-OCR` folder. Restart the app afterward; PATH changes are not required for that default folder. If installed elsewhere, enter the full path to `tesseract.exe` under **Capture / OCR > Reader settings**. The desktop app displays these directions when Tesseract is missing, and the dashboard includes an **Install Tesseract on Windows** help section. Pulling the repository does not install Tesseract automatically.

## Standalone Tesseract chat experiment

Run **Run-Tesseract-Chat.cmd**, or `.venv\Scripts\python.exe tesseract_chat_reader.py`. This separate utility opens a frozen desktop overlay: drag around chat and release; Esc cancels. Set **Capture every (seconds)** and click **Start**. Tesseract is auto-detected from PATH or standard Windows installation folders; the executable can also be selected in the UI. It must include English language data. Python dependencies are `mss`, `Pillow`, `numpy`, and `opencv-python`, already included in this project's environment; the UI uses Python's built-in Tkinter.

The first capture outputs all visible text. The **Latest full OCR capture** tab shows everything Tesseract read; **New chat** and the console show only new lines. A bounded history of 128 viewport hashes and a bottom-up search for the longest old-bottom/new-top line overlap suppress repeated views. Ordered occurrences are preserved, so a new identical announcement can still be emitted. Hash matches are verified against actual normalized text. Stop/Start retains history; **Reset history / output** starts a new transcript. A new capture region also resets history. This utility does not parse fish, calculate scores, or connect to the tournament server.

Every interval captures a screenshot; identical pixels skip redundant OCR. OCR calls never overlap, so a slow OCR call lengthens the effective interval. Use **Contrast** for colored text on dark chat, **Color** to compare the unflattened image, or **Threshold** to try black-and-white preprocessing. Scale and Tesseract PSM are configurable while stopped. Screenshots stay in memory unless saved; **Save latest images** writes both the original and OCR input, and **Save transcript** exports UTF-8 text. Keep the reader window outside the selected region.

Exact hash matching cannot repair OCR changes. If no overlap or historical view matches, the utility emits the entire current view and reports that gap rather than silently dropping text. Entirely identical viewports cannot reveal whether unseen, identical messages arrived. This is a diagnostic chat reader, not a guarantee of complete chat capture.

Read a saved image without the UI: `.venv\Scripts\python.exe tesseract_chat_reader.py --image screenshot.png`. Run its focused tests with `.venv\Scripts\python.exe -m unittest -v test_tesseract_chat_reader`.

The dashboard opens without a password. Anyone who can reach the server on your private network can view screenshots and control capture and tournaments.

## Clone and run on another laptop

1. Install Git for Windows and Python 3.10 x64 from python.org, including the Python launcher.
2. Clone this repository into a writable folder. Sign in to GitHub when prompted for private repository access.
3. Double-click **Update-And-Run.bat** each time you want to run the application.

The launcher pulls updates with `git pull --ff-only`, creates `.venv`, installs locked dependencies on first run or when the lock file changes, and starts the application. First launch needs Internet access and may take several minutes. Close the app before updating. Local source edits or an unsuccessful pull stop the launcher without discarding changes.

Run offline after setup: `Update-And-Run.bat -SkipUpdate`.

Settings, evidence, and tournament history stay on each laptop in `%LOCALAPPDATA%\SpaceGoatFisch`. Git does not synchronize this data. The existing development launcher uses the separate private `spacegoat-dev-data` folder.

## Tournament controls

In the admin dashboard, choosing **Total weight** or **Fish caught** applies the scoring mode immediately. Enter the tournament title and footer and click **Save settings** to save that text. Rankings recalculate from recorded qualifying catches. The OBS overlay displays kilograms or fish counts to match. Enter a custom footer, or leave it blank for the automatic winning rule.

**Qualifying catches** selects the regex pattern used in both scoring modes. In count mode each confirmed matching announcement adds one catch; an unreadable numeric weight does not prevent counting if the announcement still matches the selected regex. A regex that requires a weight token still requires that token to be present.

Under **Player name corrections**, choose the encountered name to merge and its destination, then click **Merge names**. Both dropdowns use names observed in the current tournament, including review events. Existing scores merge and future catches use the destination name. Creating or resetting a tournament clears its active name list and corrections; archived catch history remains available.

Manual catch editing, overlay settings, and parser calibration are collapsed until needed. Pattern selection stays visible.

The default Tesseract reader preserves gray and colored text using the standalone tool's contrast preprocessing. The optional legacy RapidOCR reader retains its contrast retry for weak or suspicious reads. Best-effort parsing requires the configured minimum OCR confidence. Tesseract uses recent complete-catch occurrences to suppress replayed messages when exact line overlap is unavailable; strict mode also requires reliable insertion evidence. Separate insertions remain separate catches even when their names and weights match.

Use a stable dark chat background and keep complete messages inside the selected region. Larger text helps recognition but leaves fewer messages visible. Unseen or clipped announcements cannot be reliably recovered; use **Test OCR / Preview** after changing the font or capture region and review uncertain events in the catch feed.

## Validation and packaging

- `Update-And-Run.bat -SelfTest`: scoring and lifecycle tests.
- `Update-And-Run.bat -SmokeTest`: OCR, HTTP, password-free controls, SSE, and Qt startup/shutdown checks.
- `Update-And-Run.bat -SetupOnly`: prepare dependencies without launching.

Validation writes to `.verification`, separate from tournament data. Live game capture, OBS, and display settings require checks on the target laptop.

See `SpaceGoat-Setup.md` for application controls, OBS, and packaging. Build a standalone executable with `powershell -NoProfile -ExecutionPolicy Bypass -File .\spacegoat-build.ps1`. Generated environments, builds, validation reports, and private tournament data are excluded from Git.

For a complete readable catch held as **Uncertain visual insertion**, enable **Best-effort live scoring** under Parser calibration and save. New qualifying Tesseract catches can score even with a 35% visual diagnostic. Existing review records are not automatically reprocessed: use **Review / edit**, choose **Confirmed**, and **Save**. The recent fallback cache is limited to 512 catch keys and expires keys after at least 30 seconds off screen; identical arrivals without observable overlap can remain ambiguous.
