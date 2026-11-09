import html
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PORT = 8765
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{host}" for host in ALLOWED_HOSTS}
SETTINGS_PATH = Path.home() / ".config" / "github-pr-overview" / "settings.json"
SOUNDS_DIRECTORY = Path("/System/Library/Sounds")
SEARCH_SYNTAX_URL = "https://docs.github.com/en/search-github/searching-on-github/searching-issues-and-pull-requests"

MY_PULL_REQUESTS = "is:pr is:open author:@me archived:false"
REVIEW_REQUESTS = "is:pr is:open archived:false review-requested:@me -author:@me"

RULES = {
    "draft": "Draft",
    "no_reviewer": "Ready for review, no reviewer requested",
    "waiting": "Waiting for reviewer",
    "needs_action": "Needs action: changes requested, CI failing or conflicts",
    "approved": "Approved",
    "all": "Every PR the query finds",
}

DEFAULT_SETTINGS = {
    "collapse_empty_sections": True,
    "auto_refresh": True,
    "refresh_minutes": 2,
    "notifications": True,
    "sound": "Glass",
    "sections": [
        {"title": "Draft", "color": "#8b949e", "query": MY_PULL_REQUESTS, "rule": "draft", "notify": False},
        {"title": "Ready for review (no reviewer)", "color": "#58a6ff", "query": MY_PULL_REQUESTS, "rule": "no_reviewer", "notify": False},
        {"title": "Waiting for reviewer", "color": "#d29922", "query": MY_PULL_REQUESTS, "rule": "waiting", "notify": False},
        {"title": "Needs action", "color": "#f85149", "query": MY_PULL_REQUESTS, "rule": "needs_action", "notify": True},
        {"title": "Ready for merge", "color": "#3fb950", "query": MY_PULL_REQUESTS, "rule": "approved", "notify": True},
        {"title": "Needs my review", "color": "#bc8cff", "query": REVIEW_REQUESTS, "rule": "all", "notify": True},
    ],
}

PULL_REQUEST_FIELDS = """
fragment PullRequestFields on PullRequest {
        title
        url
        number
        isDraft
        mergeable
        reviewDecision
        updatedAt
        repository { nameWithOwner }
        reviewRequests(first: 10) {
          nodes {
            requestedReviewer {
              ... on User { login }
              ... on Team { slug }
            }
          }
        }
        author { login }
        latestOpinionatedReviews(first: 20) { nodes { state author { login } } }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
}
"""


def available_sounds():
    return sorted(path.stem for path in SOUNDS_DIRECTORY.glob("*.aiff"))


def is_bool(value):
    return isinstance(value, bool)


def is_minutes(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def is_text(value):
    return isinstance(value, str) and value.strip() != ""


def is_color(value):
    return isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) is not None


def is_rule(value):
    return isinstance(value, str) and value in RULES


def is_sound(value):
    return isinstance(value, str) and (value == "" or value in available_sounds())


VALIDATORS = {
    "collapse_empty_sections": is_bool,
    "auto_refresh": is_bool,
    "refresh_minutes": is_minutes,
    "notifications": is_bool,
    "sound": is_sound,
    "title": is_text,
    "color": is_color,
    "query": is_text,
    "rule": is_rule,
    "notify": is_bool,
}


def pick(source, defaults, key):
    value = source.get(key)
    return value if VALIDATORS[key](value) else defaults[key]


def normalized_settings(raw):
    raw = raw if isinstance(raw, dict) else {}
    raw_sections = raw.get("sections") if isinstance(raw.get("sections"), list) else []
    sections = []
    for index, default in enumerate(DEFAULT_SETTINGS["sections"]):
        source = raw_sections[index] if index < len(raw_sections) and isinstance(raw_sections[index], dict) else {}
        sections.append({key: pick(source, default, key) for key in default})
    settings = {key: pick(raw, DEFAULT_SETTINGS, key) for key in DEFAULT_SETTINGS if key != "sections"}
    settings["sections"] = sections
    return settings


def load_settings():
    try:
        return normalized_settings(json.loads(SETTINGS_PATH.read_text()))
    except (FileNotFoundError, ValueError):
        return normalized_settings({})


def save_settings(settings):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = SETTINGS_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(settings, indent=2))
    os.replace(temporary_path, SETTINGS_PATH)


def settings_from_form(form):
    def text(name):
        return form.get(name, [""])[0].strip()

    def checked(name):
        return name in form

    minutes = text("refresh_minutes")
    return normalized_settings({
        "collapse_empty_sections": checked("collapse_empty_sections"),
        "auto_refresh": checked("auto_refresh"),
        "refresh_minutes": int(minutes) if minutes.isdigit() else None,
        "notifications": checked("notifications"),
        "sound": text("sound"),
        "sections": [
            {
                "title": text(f"section{index}_title"),
                "color": text(f"section{index}_color"),
                "query": text(f"section{index}_query"),
                "rule": text(f"section{index}_rule"),
                "notify": checked(f"section{index}_notify"),
            }
            for index in range(len(DEFAULT_SETTINGS["sections"]))
        ],
    })


def section_queries(sections):
    return list(dict.fromkeys(section["query"] for section in sections))


def build_query(query_count):
    variables = ", ".join(f"$q{index}: String!" for index in range(query_count))
    searches = "".join(
        f"  q{index}: search(query: $q{index}, type: ISSUE, first: 100) {{ nodes {{ ...PullRequestFields }} }}\n"
        for index in range(query_count)
    )
    return f"query({variables}) {{\n  viewer {{ login url }}\n{searches}}}\n{PULL_REQUEST_FIELDS}"


def fetch_pull_requests(sections):
    queries = section_queries(sections)
    arguments = ["gh", "api", "graphql", "-f", f"query={build_query(len(queries))}"]
    for index, query in enumerate(queries):
        arguments += ["-f", f"q{index}={query}"]
    output = subprocess.run(arguments, capture_output=True, text=True, check=True).stdout
    return json.loads(output)["data"]


def fetch_viewer():
    arguments = ["gh", "api", "graphql", "-f", "query={ viewer { login url } }"]
    output = subprocess.run(arguments, capture_output=True, text=True, check=True).stdout
    return json.loads(output)["data"]["viewer"]


def classify(pr):
    commits = pr["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    ci = rollup["state"] if rollup else "NONE"
    reviewers = [
        node["requestedReviewer"].get("login") or node["requestedReviewer"].get("slug")
        for node in pr["reviewRequests"]["nodes"]
        if node["requestedReviewer"]
    ]
    change_requesters = {
        review["author"]["login"]
        for review in pr["latestOpinionatedReviews"]["nodes"]
        if review["state"] == "CHANGES_REQUESTED" and review["author"]
    }
    problems = []
    if change_requesters - set(reviewers):
        problems.append("changes requested")
    if ci in ("FAILURE", "ERROR"):
        problems.append("CI failing")
    if pr["mergeable"] == "CONFLICTING":
        problems.append("conflicts")

    if pr["isDraft"]:
        return "draft", []
    if problems:
        return "needs_action", problems
    if pr["reviewDecision"] == "APPROVED":
        return "approved", ["CI running"] if ci == "PENDING" else []
    if reviewers:
        return "waiting", reviewers
    return "no_reviewer", []


def review_tags(pr, viewer_login):
    requested = [node["requestedReviewer"] for node in pr["reviewRequests"]["nodes"] if node["requestedReviewer"]]
    tags = [f"by {pr['author']['login']}"] if pr["author"] else []
    if any(reviewer.get("login") == viewer_login for reviewer in requested):
        tags.append("you")
    tags += [f"team: {reviewer['slug']}" for reviewer in requested if "slug" in reviewer]
    return tags


def by_last_update(pull_requests):
    return sorted(pull_requests, key=lambda p: p["updatedAt"], reverse=True)


def group_pull_requests(data, sections):
    queries = section_queries(sections)
    viewer_login = data["viewer"]["login"]
    groups = []
    for section in sections:
        nodes = [node for node in data[f"q{queries.index(section['query'])}"]["nodes"] if node and "url" in node]
        items = []
        for pr in by_last_update(nodes):
            if section["rule"] == "all":
                items.append((pr, review_tags(pr, viewer_login)))
                continue
            rule, tags = classify(pr)
            if rule == section["rule"]:
                items.append((pr, tags))
        groups.append(items)
    return groups


def play_sound(name):
    if name in available_sounds():
        subprocess.Popen(["afplay", str(SOUNDS_DIRECTORY / f"{name}.aiff")])


def show_notification(title, pr, tags):
    subtitle = " · ".join([f"{pr['repository']['nameWithOwner']}#{pr['number']}", *tags])
    subprocess.Popen([
        "terminal-notifier",
        "-title", title,
        "-subtitle", subtitle,
        "-message", pr["title"],
        "-open", pr["url"],
        "-group", pr["url"],
    ])


class Notifier:
    def __init__(self):
        self._lock = threading.Lock()
        self._seen = None
        self._settings = None

    def update(self, groups, settings):
        current = {
            (index, pr["url"]): (section["title"], pr, tags)
            for index, section in enumerate(settings["sections"])
            if section["notify"]
            for pr, tags in groups[index]
        }
        with self._lock:
            known = self._seen if self._settings is settings else None
            self._seen, self._settings = current, settings
        new_keys = current.keys() - known.keys() if known is not None else set()
        if not settings["notifications"] or not new_keys:
            return
        play_sound(settings["sound"])
        for key in new_keys:
            show_notification(*current[key])


class Overview:
    def __init__(self, settings, notifier):
        self.settings = settings
        self.notifier = notifier
        self.settings_changed = threading.Event()
        self.viewer = None

    def change_settings(self, settings):
        self.settings = settings
        self.settings_changed.set()

    def load_board(self):
        settings = self.settings
        data = fetch_pull_requests(settings["sections"])
        groups = group_pull_requests(data, settings["sections"])
        self.notifier.update(groups, settings)
        self.viewer = data["viewer"]
        return data["viewer"], groups

    def current_viewer(self):
        if self.viewer is None:
            try:
                self.viewer = fetch_viewer()
            except (subprocess.CalledProcessError, KeyError, ValueError):
                return None
        return self.viewer

    def watch(self):
        while True:
            settings = self.settings
            timeout = None
            if settings["auto_refresh"]:
                try:
                    self.load_board()
                except (subprocess.CalledProcessError, KeyError, ValueError):
                    pass
                timeout = settings["refresh_minutes"] * 60
            self.settings_changed.wait(timeout)
            self.settings_changed.clear()


STYLE = """
  body { margin: 0; background: #0d1117; color: #e6edf3;
         font: 14px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  a { color: #58a6ff; }
  .banner { position: sticky; top: 0; z-index: 1; display: flex; justify-content: space-between; align-items: center;
            gap: 16px; padding: 12px 24px; background: #161b22; border-bottom: 1px solid #30363d; }
  .banner h1 { font-size: 18px; margin: 0; }
  .banner h1 a { color: inherit; text-decoration: none; }
  .content { padding: 24px; }
  h1 { font-size: 20px; margin: 0 0 16px; }
  .hint { color: #8b949e; font-size: 12px; }
  .meta { display: flex; gap: 16px; align-items: center; }
  .account { color: #e6edf3; text-decoration: none; }
  .account:hover { text-decoration: underline; }
  .button { display: inline-block; color: #e6edf3; text-decoration: none; background: #21262d;
            border: 1px solid #30363d; border-radius: 6px; padding: 4px 12px; }
  .button:hover { border-color: #8b949e; }
  .button.icon { display: inline-flex; padding: 6px; }
  .button.icon svg { width: 16px; height: 16px; fill: currentColor; }
  main { display: flex; gap: 12px; align-items: flex-start; }
  section { flex: 1 1 0; min-width: 200px; background: #161b22; border: 1px solid #30363d;
            border-top: 3px solid var(--accent); border-radius: 8px; padding: 12px; }
  section.collapsed { flex: 0 0 auto; min-width: 0; padding: 12px 8px; }
  section.collapsed h2 { writing-mode: vertical-rl; margin: 0; gap: 8px; color: #8b949e; }
  h2 { font-size: 13px; margin: 0 0 10px; display: flex; justify-content: space-between; }
  .count { color: var(--accent); }
  .card { display: block; padding: 10px; margin-bottom: 8px; border-radius: 6px; background: #0d1117;
          border: 1px solid #30363d; color: inherit; text-decoration: none; }
  .card:hover { border-color: var(--accent); }
  .repo { color: #8b949e; font-size: 12px; }
  .title { margin: 4px 0; }
  .tag { display: inline-block; font-size: 11px; padding: 1px 6px; margin: 2px 4px 0 0;
         border-radius: 10px; color: var(--accent); border: 1px solid var(--accent); }
  .error { background: #161b22; border: 1px solid #f85149; border-radius: 8px; padding: 12px; white-space: pre-wrap; }
  .notice { color: #3fb950; }
  fieldset { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px 16px; margin: 0 0 16px; }
  legend { padding: 0 6px; font-weight: 600; }
  fieldset > label, fieldset > p { display: block; margin: 8px 0; }
  input, select, button { font: inherit; color: inherit; background: #0d1117; border: 1px solid #30363d;
                          border-radius: 6px; padding: 4px 8px; }
  input[type=checkbox] { padding: 0; }
  input[type=number] { width: 4em; }
  input[type=color] { width: 36px; height: 30px; padding: 2px; }
  button { cursor: pointer; }
  button:hover { border-color: #8b949e; }
  button.primary { background: #238636; border-color: #238636; }
  .rows { display: grid; grid-template-columns: auto minmax(140px, 1fr) minmax(260px, 2fr) auto auto;
          gap: 8px; align-items: center; margin-top: 8px; }
  .rows .heading { color: #8b949e; font-size: 12px; }
  .actions { display: flex; gap: 12px; align-items: center; }
"""

ICON = (
    "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='%233fb950'>"
    "<path d='M1.5 3.25a2.25 2.25 0 1 1 3 2.122v5.256a2.251 2.251 0 1 1-1.5 0V5.372A2.25 2.25 0 0 1 1.5 3.25Zm5.677-.177"
    "L9.573.677A.25.25 0 0 1 10 .854V2.5h1A2.5 2.5 0 0 1 13.5 5v5.628a2.251 2.251 0 1 1-1.5 0V5a1 1 0 0 0-1-1h-1v1.646"
    "a.25.25 0 0 1-.427.177L7.177 3.427a.25.25 0 0 1 0-.354ZM3.75 2.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm0 9.5"
    "a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm8.25.75a.75.75 0 1 0 1.5 0 .75.75 0 0 0-1.5 0Z'/></svg>"
)


GEAR_ICON = (
    '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0a8.2 8.2 0 0 1 .701.031C9.444.095 9.99.645 10.16 1.29l.288 1.107'
    'c.018.066.079.158.212.224.231.114.454.243.668.386.123.082.233.09.299.071l1.103-.303c.644-.176 1.392.021 1.82.63.27.385.506.792'
    '.704 1.218.315.675.111 1.422-.364 1.891l-.814.806c-.049.048-.098.147-.088.294.016.257.016.515 0 .772-.01.147.038.246.088.294'
    'l.814.806c.475.469.679 1.216.364 1.891a7.977 7.977 0 0 1-.704 1.217c-.428.61-1.176.807-1.82.63l-1.102-.302c-.067-.019-.177-.011'
    '-.3.071a5.909 5.909 0 0 1-.668.386c-.133.066-.194.158-.211.224l-.29 1.106c-.168.646-.715 1.196-1.458 1.26a8.006 8.006 0 0 1'
    '-1.402 0c-.743-.064-1.289-.614-1.458-1.26l-.289-1.106c-.018-.066-.079-.158-.212-.224a5.738 5.738 0 0 1-.668-.386c-.123-.082'
    '-.233-.09-.299-.071l-1.103.303c-.644.176-1.392-.021-1.82-.63a8.12 8.12 0 0 1-.704-1.218c-.315-.675-.111-1.422.363-1.891l.815'
    '-.806c.05-.048.098-.147.088-.294a6.214 6.214 0 0 1 0-.772c.01-.147-.038-.246-.088-.294l-.815-.806C.635 6.045.431 5.298.746 4.623'
    'a7.92 7.92 0 0 1 .704-1.217c.428-.61 1.176-.807 1.82-.63l1.102.302c.067.019.177.011.3-.071.214-.143.437-.272.668-.386.133-.066'
    '.194-.158.211-.224l.29-1.106C6.009.645 6.556.095 7.299.03 7.53.01 7.764 0 8 0Zm-.571 1.525c-.036.003-.108.036-.137.146l-.289'
    ' 1.105c-.147.561-.549.967-.998 1.189-.173.086-.34.183-.5.29-.417.278-.97.423-1.529.27l-1.103-.303c-.109-.03-.175.016-.195.045'
    '-.22.312-.412.644-.573.99-.014.031-.021.11.059.19l.815.806c.411.406.562.957.53 1.456a4.709 4.709 0 0 0 0 .582c.032.499-.119'
    ' 1.05-.53 1.456l-.815.806c-.081.08-.073.159-.059.19.162.346.353.677.573.989.02.03.085.076.195.046l1.102-.303c.56-.153 1.113'
    '-.008 1.53.27.161.107.328.204.501.29.447.222.85.629.997 1.189l.289 1.105c.029.109.101.143.137.146a6.6 6.6 0 0 0 1.142 0'
    'c.036-.003.108-.036.137-.146l.289-1.105c.147-.561.549-.967.998-1.189.173-.086.34-.183.5-.29.417-.278.97-.423 1.529-.27l1.103'
    '.303c.109.029.175-.016.195-.045.22-.313.411-.644.573-.99.014-.031.021-.11-.059-.19l-.815-.806c-.411-.406-.562-.957-.53-1.456'
    'a4.709 4.709 0 0 0 0-.582c-.032-.499.119-1.05.53-1.456l.815-.806c.081-.08.073-.159.059-.19a6.464 6.464 0 0 0-.573-.989'
    'c-.02-.03-.085-.076-.195-.046l-1.102.303c-.56.153-1.113.008-1.53-.27a4.44 4.44 0 0 0-.501-.29c-.447-.222-.85-.629-.997-1.189'
    'l-.289-1.105c-.029-.11-.101-.143-.137-.146a6.6 6.6 0 0 0-1.142 0ZM11 8a3 3 0 1 1-6 0 3 3 0 0 1 6 0ZM9.5 8a1.5 1.5 0 1 0'
    '-3.001.001A1.5 1.5 0 0 0 9.5 8Z"/></svg>'
)


BACK_ICON = (
    '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M7.78 12.53a.75.75 0 0 1-1.06 0L2.47 8.28a.75.75 0 0 1 0-1.06l4.25-4.25'
    'a.751.751 0 0 1 1.042.018.751.751 0 0 1 .018 1.042L4.81 7h7.44a.75.75 0 0 1 0 1.5H4.81l2.97 2.97a.75.75 0 0 1 0 1.06Z"/></svg>'
)


def banner(settings, viewer, on_settings):
    if viewer:
        account = f'<a class="account" href="{html.escape(viewer["url"])}" target="_blank">@{html.escape(viewer["login"])}</a>'
    else:
        account = "unknown"
    if on_settings:
        button = f'<a class="button icon" href="/" title="Back to the board" aria-label="Back to the board">{BACK_ICON}</a>'
    else:
        button = f'<a class="button icon" href="/settings" title="Settings" aria-label="Settings">{GEAR_ICON}</a>'
    return f"""<header class="banner">
  <h1><a href="/">GitHub PR overview</a></h1>
  <div class="meta">
    <span class="hint">{refresh_hint(settings)}</span>
    <span class="hint">gh account: {account}</span>
    {button}
  </div>
</header>"""


def page(title, banner_html, content, refresh_seconds=None):
    refresh = f'<meta http-equiv="refresh" content="{refresh_seconds}">' if refresh_seconds else ""
    body = f'{banner_html}\n<div class="content">\n{content}\n</div>'
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
{refresh}
<title>{html.escape(title)}</title>
<link rel="icon" href="{ICON}">
<style>{STYLE}</style>
</head>
<body>
{body}
</body>
</html>"""


def checked_attribute(value):
    return " checked" if value else ""


def refresh_hint(settings):
    if not settings["auto_refresh"]:
        return "Reload the page to refresh"
    minutes = settings["refresh_minutes"]
    return f"Refreshes every {minutes} minute{'s' if minutes != 1 else ''}, or reload the page"


def render_board(overview):
    settings = overview.settings
    refresh_seconds = settings["refresh_minutes"] * 60 if settings["auto_refresh"] else None
    try:
        viewer, groups = overview.load_board()
    except subprocess.CalledProcessError as error:
        content = f'<pre class="error">gh failed:\n{html.escape(error.stderr)}</pre>'
        return page("GitHub PR overview", banner(settings, None, False), content, refresh_seconds)

    columns = []
    for section, items in zip(settings["sections"], groups):
        cards = "".join(
            f"""<a class="card" href="{html.escape(pr['url'])}" target="_blank">
                  <div class="repo">{html.escape(pr['repository']['nameWithOwner'])}#{pr['number']}</div>
                  <div class="title">{html.escape(pr['title'])}</div>
                  {''.join(f'<span class="tag">{html.escape(tag)}</span>' for tag in tags)}
                </a>"""
            for pr, tags in items
        )
        collapsed = ' class="collapsed"' if settings["collapse_empty_sections"] and not items else ""
        columns.append(
            f"""<section{collapsed} style="--accent:{section['color']}">
                  <h2>{html.escape(section['title'])} <span class="count">{len(items)}</span></h2>
                  {cards}
                </section>"""
        )

    content = f"<main>{''.join(columns)}</main>"
    return page("GitHub PR overview", banner(settings, viewer, False), content, refresh_seconds)


def section_fields(index, section):
    prefix = f"section{index}_"
    rules = "".join(
        f'<option value="{key}"{" selected" if key == section["rule"] else ""}>{html.escape(label)}</option>'
        for key, label in RULES.items()
    )
    return f"""<input type="color" name="{prefix}color" value="{section['color']}" title="Color">
    <input type="text" name="{prefix}title" value="{html.escape(section['title'])}" required>
    <input type="text" name="{prefix}query" value="{html.escape(section['query'])}" required>
    <select name="{prefix}rule">{rules}</select>
    <label><input type="checkbox" name="{prefix}notify"{checked_attribute(section['notify'])}> Notify</label>"""


def render_settings(overview, saved):
    settings = overview.settings
    sound_options = "".join(
        f'<option value="{html.escape(value)}"{" selected" if value == settings["sound"] else ""}>{html.escape(label)}</option>'
        for value, label in [("", "None"), *((name, name) for name in available_sounds())]
    )
    rows = "".join(section_fields(index, section) for index, section in enumerate(settings["sections"]))
    notice = '<span class="notice">Saved</span>' if saved else ""
    content = f"""<h1>Settings</h1>
<form method="post" action="/settings">
  <fieldset>
    <legend>Board</legend>
    <label><input type="checkbox" name="collapse_empty_sections"{checked_attribute(settings['collapse_empty_sections'])}> Collapse empty sections</label>
    <label><input type="checkbox" name="auto_refresh"{checked_attribute(settings['auto_refresh'])}> Refresh every
      <input type="number" name="refresh_minutes" min="1" value="{settings['refresh_minutes']}"> minutes</label>
    <p class="hint">When refresh is off, the board and notifications only update when you reload the page.</p>
  </fieldset>
  <fieldset>
    <legend>Notifications</legend>
    <label><input type="checkbox" name="notifications"{checked_attribute(settings['notifications'])}> Show a notification when a PR lands in a section marked Notify</label>
    <label>Sound <select name="sound">{sound_options}</select>
      <button type="button" onclick="fetch('/settings/sound', {{method: 'POST', body: new URLSearchParams({{sound: this.form.sound.value}})}})">Play</button></label>
  </fieldset>
  <fieldset>
    <legend>Sections</legend>
    <p class="hint">Each section shows the PRs its <a href="{SEARCH_SYNTAX_URL}" target="_blank">GitHub search query</a> finds
      (<code>@me</code> is the gh account), filtered by its rule.</p>
    <div class="rows">
      <span class="heading">Color</span><span class="heading">Title</span><span class="heading">Query</span>
      <span class="heading">Rule</span><span class="heading"></span>
      {rows}
    </div>
  </fieldset>
  <div class="actions">
    <button type="submit" class="primary">Save</button>
    <button type="submit" formaction="/settings/reset" onclick="return confirm('Reset every setting to its default?')">Reset to defaults</button>
    {notice}
  </div>
</form>"""
    return page("Settings · GitHub PR overview", banner(settings, overview.current_viewer(), True), content)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            self.send_error(403)
            return
        url = urlsplit(self.path)
        overview = self.server.overview
        if url.path == "/":
            self.respond(render_board(overview))
        elif url.path == "/settings":
            self.respond(render_settings(overview, "saved" in parse_qs(url.query)))
        else:
            self.send_error(404)

    def do_POST(self):
        origin = self.headers.get("Origin")
        if self.headers.get("Host") not in ALLOWED_HOSTS or (origin is not None and origin not in ALLOWED_ORIGINS):
            self.send_error(403)
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
        overview = self.server.overview
        path = urlsplit(self.path).path
        if path == "/settings":
            settings = settings_from_form(form)
            save_settings(settings)
            overview.change_settings(settings)
            self.redirect("/settings?saved")
        elif path == "/settings/reset":
            settings = normalized_settings({})
            save_settings(settings)
            overview.change_settings(settings)
            self.redirect("/settings?saved")
        elif path == "/settings/sound":
            play_sound(form.get("sound", [""])[0])
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    def respond(self, text):
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    overview = Overview(load_settings(), Notifier())
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.overview = overview
    url = f"http://127.0.0.1:{PORT}"
    print(f"Serving {url} (Ctrl+C to stop)")
    threading.Thread(target=overview.watch, daemon=True).start()
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
