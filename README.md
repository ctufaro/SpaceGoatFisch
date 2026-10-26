# SpaceGoat Fisch Tournament

Windows x64 tournament server with OCR, an admin dashboard, and an OBS leaderboard.

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

## Validation and packaging

- `Update-And-Run.bat -SelfTest`: scoring and lifecycle tests.
- `Update-And-Run.bat -SmokeTest`: OCR, HTTP, password-free controls, SSE, and Qt startup/shutdown checks.
- `Update-And-Run.bat -SetupOnly`: prepare dependencies without launching.

Validation writes to `.verification`, separate from tournament data. Live game capture, OBS, and display settings require checks on the target laptop.

See `SpaceGoat-Setup.md` for application controls, OBS, and packaging. Build a standalone executable with `powershell -NoProfile -ExecutionPolicy Bypass -File .\spacegoat-build.ps1`. Generated environments, builds, validation reports, and private tournament data are excluded from Git.
