import html
import json
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765

QUERY = """
{
  viewer { login }
  mine: search(query: "is:pr is:open author:@me archived:false", type: ISSUE, first: 100) {
    nodes { ...PullRequestFields }
  }
  toReview: search(query: "is:pr is:open archived:false review-requested:@me -author:@me", type: ISSUE, first: 100) {
    nodes { ...PullRequestFields }
  }
}

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

SECTIONS = [
    ("Draft", "#8b949e"),
    ("Ready for review (no reviewer)", "#58a6ff"),
    ("Waiting for reviewer", "#d29922"),
    ("Needs action", "#f85149"),
    ("Ready for merge", "#3fb950"),
    ("Needs my review", "#bc8cff"),
]

REVIEW_SECTION = len(SECTIONS) - 1
NEEDS_ACTION_SECTION = 3
READY_FOR_MERGE_SECTION = 4
NOTIFY_SECTIONS = (NEEDS_ACTION_SECTION, READY_FOR_MERGE_SECTION, REVIEW_SECTION)
POLL_SECONDS = 120
NOTIFICATION_SOUND = "/System/Library/Sounds/Glass.aiff"


def fetch_pull_requests():
    output = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={QUERY}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(output)["data"]


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
        return 0, []
    if problems:
        return 3, problems
    if pr["reviewDecision"] == "APPROVED":
        return 4, ["CI running"] if ci == "PENDING" else []
    if reviewers:
        return 2, reviewers
    return 1, []


def review_tags(pr, viewer_login):
    requested = [node["requestedReviewer"] for node in pr["reviewRequests"]["nodes"] if node["requestedReviewer"]]
    tags = [f"by {pr['author']['login']}"] if pr["author"] else []
    if any(reviewer.get("login") == viewer_login for reviewer in requested):
        tags.append("you")
    tags += [f"team: {reviewer['slug']}" for reviewer in requested if "slug" in reviewer]
    return tags


def by_last_update(pull_requests):
    return sorted(pull_requests, key=lambda p: p["updatedAt"], reverse=True)


def group_pull_requests(data):
    groups = [[] for _ in SECTIONS]
    for pr in by_last_update(data["mine"]["nodes"]):
        section, tags = classify(pr)
        groups[section].append((pr, tags))
    viewer_login = data["viewer"]["login"]
    for pr in by_last_update(data["toReview"]["nodes"]):
        groups[REVIEW_SECTION].append((pr, review_tags(pr, viewer_login)))
    return groups


def pull_requests_to_notify(groups):
    return {
        (section, pr["url"]): (section, pr, tags)
        for section in NOTIFY_SECTIONS
        for pr, tags in groups[section]
    }


def show_notification(section, pr, tags):
    subtitle = " · ".join([f"{pr['repository']['nameWithOwner']}#{pr['number']}", *tags])
    subprocess.Popen([
        "terminal-notifier",
        "-title", SECTIONS[section][0],
        "-subtitle", subtitle,
        "-message", pr["title"],
        "-open", pr["url"],
        "-group", pr["url"],
    ])


def watch_for_new_pull_requests():
    seen = None
    while True:
        try:
            current = pull_requests_to_notify(group_pull_requests(fetch_pull_requests()))
            new_keys = current.keys() - seen.keys() if seen is not None else set()
            if new_keys:
                subprocess.Popen(["afplay", NOTIFICATION_SOUND])
            for key in new_keys:
                show_notification(*current[key])
            seen = current
        except (subprocess.CalledProcessError, KeyError, ValueError):
            pass
        time.sleep(POLL_SECONDS)


def render_page():
    try:
        groups = group_pull_requests(fetch_pull_requests())
    except subprocess.CalledProcessError as error:
        return f"<pre>gh failed:\n{html.escape(error.stderr)}</pre>"

    columns = []
    for (title, color), items in zip(SECTIONS, groups):
        cards = "".join(
            f"""<a class="card" href="{html.escape(pr['url'])}" target="_blank">
                  <div class="repo">{html.escape(pr['repository']['nameWithOwner'])}#{pr['number']}</div>
                  <div class="title">{html.escape(pr['title'])}</div>
                  {''.join(f'<span class="tag">{html.escape(tag)}</span>' for tag in tags)}
                </a>"""
            for pr, tags in items
        )
        collapsed = ' class="collapsed"' if not items else ""
        columns.append(
            f"""<section{collapsed} style="--accent:{color}">
                  <h2>{html.escape(title)} <span class="count">{len(items)}</span></h2>
                  {cards}
                </section>"""
        )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="120">
<title>GitHub PR overview</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='%233fb950'><path d='M1.5 3.25a2.25 2.25 0 1 1 3 2.122v5.256a2.251 2.251 0 1 1-1.5 0V5.372A2.25 2.25 0 0 1 1.5 3.25Zm5.677-.177L9.573.677A.25.25 0 0 1 10 .854V2.5h1A2.5 2.5 0 0 1 13.5 5v5.628a2.251 2.251 0 1 1-1.5 0V5a1 1 0 0 0-1-1h-1v1.646a.25.25 0 0 1-.427.177L7.177 3.427a.25.25 0 0 1 0-.354ZM3.75 2.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm0 9.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm8.25.75a.75.75 0 1 0 1.5 0 .75.75 0 0 0-1.5 0Z'/></svg>">
<style>
  body {{ margin: 0; padding: 24px; background: #0d1117; color: #e6edf3;
         font: 14px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  header {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 16px; }}
  h1 {{ font-size: 20px; margin: 0; }}
  .hint {{ color: #8b949e; font-size: 12px; }}
  main {{ display: flex; gap: 12px; align-items: flex-start; }}
  section {{ flex: 1 1 0; min-width: 200px; background: #161b22; border: 1px solid #30363d;
            border-top: 3px solid var(--accent); border-radius: 8px; padding: 12px; }}
  section.collapsed {{ flex: 0 0 auto; min-width: 0; padding: 12px 8px; }}
  section.collapsed h2 {{ writing-mode: vertical-rl; margin: 0; gap: 8px; color: #8b949e; }}
  h2 {{ font-size: 13px; margin: 0 0 10px; display: flex; justify-content: space-between; }}
  .count {{ color: var(--accent); }}
  .card {{ display: block; padding: 10px; margin-bottom: 8px; border-radius: 6px; background: #0d1117;
          border: 1px solid #30363d; color: inherit; text-decoration: none; }}
  .card:hover {{ border-color: var(--accent); }}
  .repo {{ color: #8b949e; font-size: 12px; }}
  .title {{ margin: 4px 0; }}
  .tag {{ display: inline-block; font-size: 11px; padding: 1px 6px; margin: 2px 4px 0 0;
         border-radius: 10px; color: var(--accent); border: 1px solid var(--accent); }}
</style>
</head>
<body>
<header><h1>My open PRs</h1><span class="hint">Refreshes every 2 minutes, or reload the page</span></header>
<main>{''.join(columns)}</main>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        body = render_page().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Serving {url} (Ctrl+C to stop)")
    threading.Thread(target=watch_for_new_pull_requests, daemon=True).start()
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
