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

The cog button in the top banner opens a page where you can change:

- the port the board is served on (**Server**): after saving, the board restarts on the new port and the page follows it; a port that's in use is refused and the board stays where it is
- whether empty columns collapse into a thin bar
- the refresh interval, or turn auto-refresh off so the board (and notifications) only update when you reload the page
- whether notifications show, and their sound (any macOS system sound, or none; **Play** previews it)
- rules: named sets of conditions, reusable across columns
- per column: title, color, whether it notifies, its position on the board (the arrows), its [GitHub search query](https://docs.github.com/en/search-github/searching-on-github/searching-issues-and-pull-requests), and its rule

A column shows the PRs its query finds that match its rule, or every PR the query finds when it has no rule. A rule matches a PR when **all** of its conditions hold, or, with **Match any**, when at least one does; a rule without conditions matches every PR. **Add rule** creates one, **Add condition** adds a condition to it, and several columns can use the same rule. Each condition shows what it checks, and each rule shows a one-line summary of its logic. The conditions:

| Condition | Values | Checks |
|---|---|---|
| State | `open` / `merged` / `closed` | whether the PR is open, merged, or closed without merging |
| Draft | `yes` / `no` | the PR is marked as a draft |
| Approved | `yes` / `no` | GitHub's review decision is Approved, following the repo's required reviews |
| Changes requested | `yes` / `no` | a reviewer's latest review requests changes, and they haven't been asked to review again |
| CI | `passing` / `failing` / `pending` / `none` | the combined result of the checks on the latest commit; `none` means it has no checks |
| Merge conflicts | `yes` / `no` | GitHub reports the PR can't merge cleanly (`no` while GitHub is still checking) |
| Reviewers requested | `yes` / `no` | at least one person or team is currently asked to review |
| Review requested from me | `yes` / `no` | the gh account itself is asked to review; team requests don't count |
| Author is me | `yes` / `no` | the gh account opened the PR |
| Matches rule | a rule | the PR matches another rule |
| Doesn't match rule | a rule | the PR doesn't match another rule |

The last two let rules build on each other. For example, the default **Has a problem** rule matches any of changes requested, CI failing or merge conflicts, and a rule with *all* of "Draft: no" and "Matches rule: Has a problem" shows non-draft PRs that need action. A condition that would make rules use each other in a loop is dropped when you save.

With **Show each PR only in one section** on (the default), a PR that matches several columns only shows in the one with the lowest **Priority** number. The defaults use that: Draft (1, query `draft:true`), Needs action (2, rule "Has a problem"), Ready for merge (3, query `review:approved`), Waiting for reviewer (4, rule "Reviewers requested"), and Ready for review (5, no rule) catching the rest. Use the query for whatever GitHub search can express, and rules for what it can't, such as merge conflicts, "asked to review again", or "any of" combinations. Turn it off to show a PR in every column it matches.

## Tracked repositories

Besides your own PRs, the board can show a second row with the PRs of repositories you pick, in any state. In settings, **Tracked repositories** lists every repository the `gh` account can see, grouped as your public ones, your private ones, one group per org, and repositories you collaborate on, each marked public or private. Type in the filter to narrow the list, and check the ones to track. The list is cached in `~/.config/github-pr-overview/repositories.json`; **Reload list** fetches it again, for example after you join an org. An org only shows the repositories the `gh` token is authorized for: if an SSO org shows none, run `gh auth refresh` and authorize the token for that org.

Each tracked repository contributes its most recently updated PRs (20 by default). They fall into the **Tracked repository sections**, which are shared by every tracked repository and work like the columns above without a query: Merged (1), Closed (2), Draft (3), Needs action (4), Ready for merge (5), Ready for review (6, reviewers requested), and Open (7) catching the rest. **Hide PRs that already show in the My PRs row** (on by default) keeps your own board's PRs out of this row. A repository that fails to load shows an error above the row; the rest still load.

**Reset to defaults** restores the board above. Settings are saved in `~/.config/github-pr-overview/settings.json`.

## Install

On a Mac with [Homebrew](https://brew.sh):

```bash
curl -fsSL https://raw.githubusercontent.com/felipe-promineo/github-pr-overview/main/install.sh | bash
```

Or from a clone: `./install.sh`. The installer:

1. installs what's missing with Homebrew: `gh`, `terminal-notifier`, `git` and Python 3.8 or newer
2. runs `gh auth login` if `gh` isn't logged in yet
3. clones the app into `~/Repositories/github-pr-overview` (`--dir PATH` for another place; run from a clone, it uses that clone), or updates an existing clone
4. picks the port: the one in the settings (8765 at first), or the next free one when that is in use, which it then saves (`--port PORT` to choose it yourself)
5. writes `~/Library/LaunchAgents/github-pr-overview.plist` with the right paths and loads it, so the board runs now and starts at login (and restarts if it stops)
6. adds the `gh pr-overview` (terminal list) and `gh pr-overview-web` (opens the board on whatever port it uses) aliases
7. opens the board (`--no-browser` to skip)

At the end it lists the orgs the `gh` token can see. If an org's repositories don't show up in **Tracked repositories**, it probably uses SSO: run `gh auth refresh` and authorize the token for it at https://github.com/settings/tokens.

**Update:** run the installer again. It pulls the clone and restarts the board.

**Uninstall:** `./install.sh --uninstall` stops the board and removes the login item and the aliases. It keeps the clone and your settings in `~/.config/github-pr-overview`.

The server logs to `/tmp/github-pr-overview.log`.

## Manual setup

Requirements: macOS (notifications use `terminal-notifier` and `afplay`), Python 3.8+ (standard library only), the [GitHub CLI](https://cli.github.com/) logged in with `gh auth login`, and `brew install terminal-notifier`.

```bash
python3 github-pr-overview.py
```

This serves the page on http://127.0.0.1:8765 and opens it in your browser. Pass `--no-browser` to only start the server. `--show-port` prints the port it uses, and `--set-port PORT` saves another one (it takes effect when the server next starts). `github-pr-overview.sh` prints the same overview (without the review column) in the terminal.

To start it at login by hand, copy `github-pr-overview.plist` to `~/Library/LaunchAgents/`, replace `/path/to/github-pr-overview` with the path to your clone, check the `python3` path (`which python3`), and load it with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/github-pr-overview.plist`. After pulling changes, restart it with `launchctl kickstart -k gui/$(id -u)/github-pr-overview`.
