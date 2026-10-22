# SpaceGoat Fisch Tournament Server

## Install on the laptop

1. Extract the complete `SpaceGoatFisch-Windows-x64.zip` into a folder.
2. Double-click `Install-SpaceGoat.cmd`. It installs for the current user and creates Desktop and Start Menu shortcuts. No administrator rights or separate Python, OCR engine, or model installation is needed.
3. Launch the Desktop shortcut. Keep the complete installed folder together, especially `_internal`.
4. Open the Admin Dashboard URL displayed by the application. Keep the application running; capture and tournament controls are available on the web dashboard.

The bundle targets Windows x64. Packaged tests on the build PC verify bundled OCR models, native dependencies, HTTP/SSE, Qt, and startup with Python removed from PATH. A clean-machine test on the laptop is still required.

## Web dashboard workflow

- In Capture / OCR, expand Chat region setup, choose the game monitor, load a screenshot, drag around complete catch messages, and Save chat region. Stop capture before changing the region. Test OCR checks the saved region without scoring old messages.
- Create a named tournament. This archives the previous tournament and clears the new scoreboard while retaining history.
- Start now begins immediately. Start with countdown accepts seconds and automatically starts at zero. Both start capture automatically, as does Resume. Cancel countdown cancels a scheduled start.
- The overlay shows the start countdown, then elapsed tournament time. Pause freezes scoring and elapsed time. Resume continues them. Finish preserves final scores and elapsed time.
- Start capture, Stop capture, and Restart capture provide recovery without using the Python window. Stop discards queued work. Restart waits for the current OCR call to finish before starting a fresh capture baseline.
- The tournament countdown is owned by the server and does not require an open browser tab. Keep the server application running.

## Game visibility and scoring

Keep Roblox's Catches tab visible on the selected monitor. Capture reads screen pixels: browser windows, mobile previews, tooltips, or other windows covering chat corrupt OCR. Prefer controlling the dashboard from another monitor or computer. Do not minimize Roblox or lock the capture computer. Reselect the region after resolution, display scaling, monitor, or chat layout changes.

Best-effort live scoring is enabled by default. Readable username, rarity, weight, and fish information scores immediately without OCR/scroll confidence approval. Best-effort mode also removes detached icon letters and vertical strokes around nameplates, joins spaces in display names for scoring (Meow Skulls becomes MeowSkulls), and accepts an extra OCR letter between caught and a/an. This can merge distinct display names. Short detached numeric tokens before a decimal weight are dropped only when normal weight parsing fails; valid thousands grouping is preserved. These repairs are heuristic. Unreadable or malformed messages remain in review. Rapid re-reads matching player, rarity, and weight within three seconds are suppressed; legitimate identical catches within that window may be missed. Repeated redraws can still produce inaccurate totals. The overlay labels scores experimental and not 100% accurate. Disable Best-effort live scoring in Parser calibration for strict confirmation checks.

## Overlay and LAN access

Use the displayed OBS Overlay URL for an OBS Browser Source. `127.0.0.1` works only on the capture computer; another computer needs the laptop's private LAN IP. No password is required. Anyone who can reach the server on your private network can access screenshots, capture settings, and tournament controls. Allow the application through Windows Firewall on your private network if controlling it from another PC.

## Data and cleanup

Installed program: `%LOCALAPPDATA%\Programs\SpaceGoatFisch`.
Runtime data: `%LOCALAPPDATA%\SpaceGoatFisch`. Developer launches use a separate `spacegoat-dev-data` folder.

- `evidence\*.png` accumulates OCR diagnostic, uncertain-capture, review, and overflow screenshots without automatic expiry. With the application closed, delete older screenshots when no longer needed. This removes evidence, not scores, although history may still reference deleted screenshots. Turn off diagnostic evidence in the application to reduce capture volume; some error/review evidence is still saved.
- `server.log` rotates at approximately 2 MB with three backups (roughly 8 MB total). Routine cleanup is unnecessary.
- `tournaments.sqlite3` contains scores, tournaments, and audit history and grows with use. Preserve it unless intentionally discarding history. Its `-wal` and `-shm` files belong to SQLite; do not delete them while running.
- `config.json` contains the selected region and settings. Preserve it and keep backups private. Initial parser migration backups and startup-error.txt are small.
- On the development PC, `build\SpaceGoatFisch`, `spacegoat-validation`, and `overlay-update-validation` are generated build/test output. They can be removed when not building/testing and no reports or replay captures are needed. `dist` holds the deployable package; `.spacegoat-build` is the reusable build environment. They are not required on the laptop.

Back up the runtime-data folder while the application is closed. Uninstalling or deleting the program folder does not remove tournament history.

## Build from source

On a Windows x64 development machine with Python 3.10:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\spacegoat-build.ps1
```

This builds and tests the complete executable distribution and creates the deployment ZIP. The build manifest records source/executable hashes and target validation limits. Do not copy only the EXE; copy or install the complete extracted bundle. Actual Fisch observer visibility, OBS, and mixed-DPI selection still need checking on the target laptop.

## Player name corrections, scoreboard removal, and rules

Everyone joins automatically when a readable catch appears. Registration is not required. In Player name corrections, enter `FifthSparkGMZZ->FifthSparkGMZ`, one correction per line, then Save corrections. Use the scoreboard's Correct name button to fill the source name, choose the target username, and Add and save correction. Saving merges matching existing names in the current tournament without changing weights or confirming review catches. Corrections persist for future catches and future tournaments. Removing a correction stops future rewriting but does not undo past merges. Use direct mappings to the final username; chains and cycles are rejected. Similar names are not automatically merged by the explicit mapping system. Prior display-name aliases are shown as corrections when loading the dashboard.

The leaderboard's Kick button removes that player's existing score and blocks subsequent catches for the current tournament. Disqualified catches and audits remain in history. A new tournament starts without the prior kick list. Mapped corrections preserve kicked status. Kick affects only this application scoreboard.

Overlay rules is a free-text field, up to 2000 characters, shown below the timer where LIVE previously appeared. Rules update immediately when saved; they are display text, not an automatic rarity filter. Keep the desired Common-only regex in Parser calibration to enforce that restriction.

Under Parser calibration > Pattern, choose a friendly name from the dropdown to immediately apply that regex as the only active game pattern. Edit and Delete sit beside the dropdown; Add pattern opens a compact editor for a new name and regex. Save pattern persists the entry and applies it immediately; Cancel discards editor changes. Common Fish, All Rarities, and Limited Fish presets are included alongside existing regexes. Select Limited Fish to accept only Limited catches. Deleting the selected entry applies the next available entry; the last entry cannot be deleted. Up to 50 named entries can be stored. Older configurations with multiple active regexes select the first active regex when the admin page loads. Save parser still applies the other calibration settings.

The Fisch-inspired theme is built into the executable. Remove the previously supplied theme from OBS Custom CSS; keep the Browser Source background transparent.
