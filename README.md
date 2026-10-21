# SpaceGoat Fisch Tournament

Windows x64 tournament server with OCR, an admin dashboard, and an OBS leaderboard.

## Clone and run on another laptop

1. Install Git for Windows and Python 3.10 x64 from python.org, including the Python launcher.
2. Clone this repository into a writable folder. Sign in to GitHub when prompted for private repository access.
3. Double-click **Update-And-Run.bat** each time you want to run the application.

The launcher pulls updates with `git pull --ff-only`, creates `.venv`, installs locked dependencies on first run or when the lock file changes, and starts the application. First launch needs Internet access and may take several minutes. Close the app before updating. Local source edits or an unsuccessful pull stop the launcher without discarding changes.

Run offline after setup: `Update-And-Run.bat -SkipUpdate`.

Settings, admin credentials, evidence, and tournament history stay on each laptop in `%LOCALAPPDATA%\SpaceGoatFisch`. Git does not synchronize this data. The existing development launcher uses the separate private `spacegoat-dev-data` folder.

## Validation and packaging

- `Update-And-Run.bat -SelfTest`: scoring and lifecycle tests.
- `Update-And-Run.bat -SmokeTest`: OCR, HTTP, authentication, SSE, and Qt startup/shutdown checks.
- `Update-And-Run.bat -SetupOnly`: prepare dependencies without launching.

Validation writes to `.verification`, separate from tournament data. Live game capture, OBS, and display settings require checks on the target laptop.

See `SpaceGoat-Setup.md` for application controls, OBS, and packaging. Build a standalone executable with `powershell -NoProfile -ExecutionPolicy Bypass -File .\spacegoat-build.ps1`. Generated environments, builds, validation reports, and private tournament data are excluded from Git.
