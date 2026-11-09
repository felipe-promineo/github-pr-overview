# GitHub PR overview

A small local web page that shows your open GitHub pull requests as a board, and sends a macOS notification when one needs you.

Default columns:

- **Draft**
- **Ready for review (no reviewer)**
- **Waiting for reviewer**: lists the requested reviewers
- **Needs action**: changes requested, CI failing or merge conflicts
- **Ready for merge**: approved (tagged "CI running" while checks are pending)
- **Needs my review**: other people's PRs where you or one of your teams is a requested reviewer

By default the page and a background check refresh every 2 minutes, and a sound plays and a notification shows for every PR that newly lands in **Needs action**, **Ready for merge** or **Needs my review**. Clicking the notification opens the PR. The header shows which `gh` account the board is for.

Everything runs on your machine with your own `gh` login, and the page is served on `127.0.0.1` only.

## Settings

The **Settings** link on the board opens a page where you can change:

- whether empty columns collapse into a thin bar
- the refresh interval, or turn auto-refresh off so the board (and notifications) only update when you reload the page
- whether notifications show, and their sound (any macOS system sound, or none; **Play** previews it)
- per column: title, color, the [GitHub search query](https://docs.github.com/en/search-github/searching-on-github/searching-issues-and-pull-requests) it shows, the rule that filters the query's results (draft, no reviewer, waiting for reviewer, needs action, approved, or every PR), and whether it notifies

**Reset to defaults** restores the board above. Settings are saved in `~/.config/github-pr-overview/settings.json`.

## Requirements

- macOS (notifications use `terminal-notifier` and `afplay`)
- Python 3 (standard library only)
- [GitHub CLI](https://cli.github.com/), logged in: `gh auth login`
- `terminal-notifier`: `brew install terminal-notifier`

## Run it

```bash
python3 github-pr-overview.py
```

This serves the page on http://127.0.0.1:8765 and opens it in your browser. Pass `--no-browser` to only start the server.

`github-pr-overview.sh` prints the same overview (without the review column) in the terminal.

## Optional: gh aliases

Add these under `aliases:` in `~/.config/gh/config.yml`, with the path to your clone:

```yaml
aliases:
    pr-overview: '!/path/to/github-pr-overview/github-pr-overview.sh'
    pr-overview-web: '!open http://127.0.0.1:8765'
```

Then `gh pr-overview` prints the list and `gh pr-overview-web` opens the page.

## Optional: start at login

1. Copy `github-pr-overview.plist` to `~/Library/LaunchAgents/`, renaming the file and its label if you like.
2. Replace `/path/to/github-pr-overview` with the path to your clone, and check the `python3` path (`which python3`).
3. Load it:

   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/github-pr-overview.plist
   ```

The server then starts at login and restarts if it stops. Its output goes to `/tmp/github-pr-overview.log`.
