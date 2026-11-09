# GitHub PR overview

A small local web page that shows your open GitHub pull requests as a board, and sends a macOS notification when one needs you.

Columns:

- **Draft**
- **Ready for review (no reviewer)**
- **Waiting for reviewer**: lists the requested reviewers
- **Needs action**: changes requested, CI failing or merge conflicts
- **Ready for merge**: approved (tagged "CI running" while checks are pending)
- **Needs my review**: other people's PRs where you or one of your teams is a requested reviewer

The page refreshes every 2 minutes. In the background it checks every 2 minutes as well, and plays a sound and shows a notification for every PR that newly lands in **Needs action**, **Ready for merge** or **Needs my review**. Clicking the notification opens the PR.

Everything runs on your machine with your own `gh` login: the page is served on `127.0.0.1` only, and nothing is stored.

## Requirements

- macOS (notifications use `terminal-notifier` and `afplay`)
- Python 3 (standard library only)
- [GitHub CLI](https://cli.github.com/), logged in: `gh auth login`
- `terminal-notifier`: `brew install terminal-notifier`

## Run it

```bash
python3 myprs_app.py
```

This serves the page on http://127.0.0.1:8765 and opens it in your browser. Pass `--no-browser` to only start the server.

`myprs.sh` prints the same overview (without the review column) in the terminal.

## Optional: gh aliases

Add these under `aliases:` in `~/.config/gh/config.yml`, with the path to your clone:

```yaml
aliases:
    myprs: '!/path/to/github-pr-overview/myprs.sh'
    myprs-web: '!open http://127.0.0.1:8765'
```

Then `gh myprs` prints the list and `gh myprs-web` opens the page.

## Optional: start at login

1. Copy `com.example.myprs.plist` to `~/Library/LaunchAgents/`, renaming the label and file if you like.
2. Replace `/path/to/github-pr-overview` with the path to your clone, and check the `python3` path (`which python3`).
3. Load it:

   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.myprs.plist
   ```

The server then starts at login and restarts if it stops. Its output goes to `/tmp/myprs.log`.
