import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PORT = 8765
FRESH_SECONDS = 15
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{host}" for host in ALLOWED_HOSTS}
SETTINGS_PATH = Path.home() / ".config" / "github-pr-overview" / "settings.json"
SOUNDS_DIRECTORY = Path("/System/Library/Sounds")
SEARCH_SYNTAX_URL = "https://docs.github.com/en/search-github/searching-on-github/searching-issues-and-pull-requests"

MY_PULL_REQUESTS = "is:pr is:open author:@me archived:false"
REVIEW_REQUESTS = "is:pr is:open archived:false review-requested:@me -author:@me"

YES_NO = ["yes", "no"]

CONDITIONS = {
    "draft": {
        "label": "Draft",
        "values": YES_NO,
        "description": "the PR is marked as a draft",
        "phrases": {"yes": "draft", "no": "not a draft"},
    },
    "approved": {
        "label": "Approved",
        "values": YES_NO,
        "description": "GitHub's review decision is Approved, following the repo's required reviews",
        "phrases": {"yes": "approved", "no": "not approved"},
    },
    "changes_requested": {
        "label": "Changes requested",
        "values": YES_NO,
        "description": "a reviewer's latest review requests changes, and they haven't been asked to review again",
        "phrases": {"yes": "changes requested", "no": "no changes requested"},
    },
    "ci": {
        "label": "CI",
        "values": ["passing", "failing", "pending", "none"],
        "description": "the combined result of the checks on the latest commit; none means it has no checks",
        "phrases": {"passing": "CI passing", "failing": "CI failing", "pending": "CI running", "none": "no CI checks"},
    },
    "conflicts": {
        "label": "Merge conflicts",
        "values": YES_NO,
        "description": "GitHub reports the PR can't merge cleanly into its base branch (no while GitHub is still checking)",
        "phrases": {"yes": "merge conflicts", "no": "no merge conflicts"},
    },
    "reviewers_requested": {
        "label": "Reviewers requested",
        "values": YES_NO,
        "description": "at least one person or team is currently asked to review",
        "phrases": {"yes": "reviewers requested", "no": "no reviewers requested"},
    },
    "requested_from_me": {
        "label": "Review requested from me",
        "values": YES_NO,
        "description": "the gh account itself is asked to review; requests to its teams don't count",
        "phrases": {"yes": "review requested from me", "no": "review not requested from me"},
    },
    "author_is_me": {
        "label": "Author is me",
        "values": YES_NO,
        "description": "the gh account opened the PR",
        "phrases": {"yes": "opened by me", "no": "opened by someone else"},
    },
    "rule": {
        "label": "Matches rule",
        "values": None,
        "description": "the PR matches the chosen rule",
        "phrases": {},
    },
    "not_rule": {
        "label": "Doesn't match rule",
        "values": None,
        "description": "the PR doesn't match the chosen rule",
        "phrases": {},
    },
}

RULE_CONDITIONS = ("rule", "not_rule")
MATCH_MODES = {"all": "all", "any": "any"}

CI_STATES = {"SUCCESS": "passing", "FAILURE": "failing", "ERROR": "failing", "PENDING": "pending", "EXPECTED": "pending"}

EVERY_PULL_REQUEST = "Every PR the query finds"
RULE_ID = re.compile(r"[a-z0-9_-]{1,40}")
OLD_RULE_PRIORITIES = {"draft": 1, "needs_action": 2, "approved": 3, "waiting": 4, "no_reviewer": 5, "all": 6}
OLD_RULE_QUALIFIERS = {"draft": "draft:true", "approved": "review:approved"}

DEFAULT_SETTINGS = {
    "collapse_empty_sections": True,
    "auto_refresh": True,
    "refresh_minutes": 2,
    "notifications": True,
    "sound": "Glass",
    "first_match_only": True,
    "rules": [
        {"id": "needs_action", "name": "Has a problem", "match": "any", "conditions": [
            {"condition": "changes_requested", "value": "yes"},
            {"condition": "ci", "value": "failing"},
            {"condition": "conflicts", "value": "yes"},
        ]},
        {"id": "waiting", "name": "Reviewers requested", "match": "all",
         "conditions": [{"condition": "reviewers_requested", "value": "yes"}]},
    ],
    "sections": [
        {"title": "Draft", "color": "#8b949e", "query": f"{MY_PULL_REQUESTS} draft:true", "priority": 1, "notify": False, "rule": ""},
        {"title": "Ready for review (no reviewer)", "color": "#58a6ff", "query": MY_PULL_REQUESTS, "priority": 5, "notify": False, "rule": ""},
        {"title": "Waiting for reviewer", "color": "#d29922", "query": MY_PULL_REQUESTS, "priority": 4, "notify": False, "rule": "waiting"},
        {"title": "Needs action", "color": "#f85149", "query": MY_PULL_REQUESTS, "priority": 2, "notify": True, "rule": "needs_action"},
        {"title": "Ready for merge", "color": "#3fb950", "query": f"{MY_PULL_REQUESTS} review:approved", "priority": 3, "notify": True, "rule": ""},
        {"title": "Needs my review", "color": "#bc8cff", "query": REVIEW_REQUESTS, "priority": 6, "notify": True, "rule": ""},
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


def is_priority(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def is_sound(value):
    return isinstance(value, str) and (value == "" or value in available_sounds())


VALIDATORS = {
    "collapse_empty_sections": is_bool,
    "auto_refresh": is_bool,
    "refresh_minutes": is_minutes,
    "notifications": is_bool,
    "sound": is_sound,
    "first_match_only": is_bool,
    "title": is_text,
    "color": is_color,
    "query": is_text,
    "priority": is_priority,
    "notify": is_bool,
}


def pick(source, defaults, key):
    value = source.get(key)
    return value if VALIDATORS[key](value) else defaults[key]


def is_valid_condition(raw, rule_ids):
    if not isinstance(raw, dict) or not isinstance(raw.get("condition"), str) or raw["condition"] not in CONDITIONS:
        return False
    if raw["condition"] in RULE_CONDITIONS:
        return isinstance(raw.get("value"), str) and raw["value"] in rule_ids
    return raw.get("value") in CONDITIONS[raw["condition"]]["values"]


def without_reference_loops(rules):
    references = {rule["id"]: [] for rule in rules}

    def reaches(start, goal):
        pending, visited = [start], set()
        while pending:
            current = pending.pop()
            if current == goal:
                return True
            if current not in visited:
                visited.add(current)
                pending.extend(references[current])
        return False

    for rule in rules:
        kept = []
        for condition in rule["conditions"]:
            if condition["condition"] in RULE_CONDITIONS:
                if reaches(condition["value"], rule["id"]):
                    continue
                references[rule["id"]].append(condition["value"])
            kept.append(condition)
        rule["conditions"] = kept
    return rules


def normalized_rules(raw_rules):
    valid = []
    for raw in raw_rules:
        rule_id = raw.get("id") if isinstance(raw, dict) else None
        if isinstance(rule_id, str) and RULE_ID.fullmatch(rule_id) and rule_id not in {rule["id"] for rule in valid}:
            valid.append(raw)
    rule_ids = {raw["id"] for raw in valid}
    rules = []
    for raw in valid:
        raw_conditions = raw.get("conditions") if isinstance(raw.get("conditions"), list) else []
        rules.append({
            "id": raw["id"],
            "name": raw["name"].strip() if is_text(raw.get("name")) else "Rule",
            "match": raw.get("match") if raw.get("match") in MATCH_MODES else "all",
            "conditions": [
                {"condition": condition["condition"], "value": condition["value"]}
                for condition in raw_conditions
                if is_valid_condition(condition, rule_ids)
            ],
        })
    return without_reference_loops(rules)


def normalized_section(raw, index, rule_ids, from_before_rules):
    rule = raw.get("rule") if isinstance(raw.get("rule"), str) else ""
    fallback = {
        "title": "Section",
        "color": "#8b949e",
        "query": MY_PULL_REQUESTS,
        "priority": OLD_RULE_PRIORITIES.get(rule, index + 1),
        "notify": False,
    }
    section = {key: pick(raw, fallback, key) for key in fallback}
    if from_before_rules and rule in OLD_RULE_QUALIFIERS:
        section["query"] = f"{section['query']} {OLD_RULE_QUALIFIERS[rule]}"
    section["rule"] = rule if rule in rule_ids else ""
    return section


def normalized_settings(raw):
    raw = raw if isinstance(raw, dict) else {}
    raw_rules = raw.get("rules") if isinstance(raw.get("rules"), list) else DEFAULT_SETTINGS["rules"]
    raw_sections = raw.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raw_sections = DEFAULT_SETTINGS["sections"]
    rules = normalized_rules(raw_rules)
    rule_ids = {rule["id"] for rule in rules}
    settings = {key: pick(raw, DEFAULT_SETTINGS, key) for key in DEFAULT_SETTINGS if key not in ("rules", "sections")}
    settings["rules"] = rules
    settings["sections"] = [
        normalized_section(section if isinstance(section, dict) else {}, index, rule_ids, "rules" not in raw)
        for index, section in enumerate(raw_sections)
    ]
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


def section_order(form, count):
    try:
        order = [int(index) for index in form.get("section_order", [""])[0].split(",")]
    except ValueError:
        return list(range(count))
    return order if sorted(order) == list(range(count)) else list(range(count))


def settings_from_form(form):
    def text(name):
        return form.get(name, [""])[0].strip()

    def checked(name):
        return name in form

    def number(name):
        value = text(name)
        return int(value) if value.isdigit() else None

    count = 0
    while f"section{count}_title" in form:
        count += 1
    rule_indices = sorted(int(match.group(1)) for key in form if (match := re.fullmatch(r"rule(\d+)_id", key)))
    return normalized_settings({
        "collapse_empty_sections": checked("collapse_empty_sections"),
        "auto_refresh": checked("auto_refresh"),
        "refresh_minutes": number("refresh_minutes"),
        "notifications": checked("notifications"),
        "sound": text("sound"),
        "first_match_only": checked("first_match_only"),
        "rules": [
            {
                "id": text(f"rule{index}_id"),
                "name": text(f"rule{index}_name"),
                "match": text(f"rule{index}_match"),
                "conditions": [
                    {"condition": condition, "value": value}
                    for condition, value in zip(form.get(f"rule{index}_condition", []), form.get(f"rule{index}_value", []))
                ],
            }
            for index in rule_indices
        ],
        "sections": [
            {
                "title": text(f"section{index}_title"),
                "color": text(f"section{index}_color"),
                "query": text(f"section{index}_query"),
                "priority": number(f"section{index}_priority"),
                "notify": checked(f"section{index}_notify"),
                "rule": text(f"section{index}_rule"),
            }
            for index in section_order(form, count)
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


def pull_request_facts(pr, viewer_login):
    commits = pr["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    ci = CI_STATES.get(rollup["state"], "none") if rollup else "none"
    requested = [node["requestedReviewer"] for node in pr["reviewRequests"]["nodes"] if node["requestedReviewer"]]
    reviewers = [reviewer.get("login") or reviewer.get("slug") for reviewer in requested]
    change_requesters = {
        review["author"]["login"]
        for review in pr["latestOpinionatedReviews"]["nodes"]
        if review["state"] == "CHANGES_REQUESTED" and review["author"]
    }
    changes_requested = bool(change_requesters - set(reviewers))
    conflicts = pr["mergeable"] == "CONFLICTING"
    problems = [
        label
        for label, present in (("changes requested", changes_requested), ("CI failing", ci == "failing"), ("conflicts", conflicts))
        if present
    ]
    return {
        "draft": pr["isDraft"],
        "approved": pr["reviewDecision"] == "APPROVED",
        "changes_requested": changes_requested,
        "ci": ci,
        "conflicts": conflicts,
        "reviewers_requested": bool(reviewers),
        "requested_from_me": any(reviewer.get("login") == viewer_login for reviewer in requested),
        "author_is_me": bool(pr["author"]) and pr["author"]["login"] == viewer_login,
        "problems": problems,
        "reviewers": reviewers,
        "teams": [reviewer["slug"] for reviewer in requested if "slug" in reviewer],
    }


def condition_holds(condition, facts, rules_by_id):
    if condition["condition"] == "rule":
        return rule_matches(rules_by_id[condition["value"]], facts, rules_by_id)
    if condition["condition"] == "not_rule":
        return not rule_matches(rules_by_id[condition["value"]], facts, rules_by_id)
    actual = facts[condition["condition"]]
    if isinstance(actual, bool):
        actual = "yes" if actual else "no"
    return actual == condition["value"]


def rule_matches(rule, facts, rules_by_id):
    if not rule["conditions"]:
        return True
    results = (condition_holds(condition, facts, rules_by_id) for condition in rule["conditions"])
    return all(results) if rule["match"] == "all" else any(results)


def card_tags(pr, facts):
    if not facts["author_is_me"]:
        tags = [f"by {pr['author']['login']}"] if pr["author"] else []
        if facts["requested_from_me"]:
            tags.append("you")
        return tags + [f"team: {team}" for team in facts["teams"]]
    if facts["draft"]:
        return []
    if facts["problems"]:
        return facts["problems"]
    if facts["approved"]:
        return ["CI running"] if facts["ci"] == "pending" else []
    return facts["reviewers"]


def by_last_update(pull_requests):
    return sorted(pull_requests, key=lambda p: p["updatedAt"], reverse=True)


def group_pull_requests(data, settings):
    sections = settings["sections"]
    queries = section_queries(sections)
    viewer_login = data["viewer"]["login"]
    rules_by_id = {rule["id"]: rule for rule in settings["rules"]}
    groups = [[] for _ in sections]
    placed = set()
    for index in sorted(range(len(sections)), key=lambda position: (sections[position]["priority"], position)):
        section = sections[index]
        nodes = [node for node in data[f"q{queries.index(section['query'])}"]["nodes"] if node and "url" in node]
        for pr in by_last_update(nodes):
            if settings["first_match_only"] and pr["url"] in placed:
                continue
            facts = pull_request_facts(pr, viewer_login)
            rule = rules_by_id.get(section["rule"])
            if rule is None or rule_matches(rule, facts, rules_by_id):
                groups[index].append((pr, card_tags(pr, facts)))
                placed.add(pr["url"])
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
        self.board = None
        self._refresh_lock = threading.Lock()

    def change_settings(self, settings):
        self.settings = settings
        self.settings_changed.set()

    def cached_board(self):
        board = self.board
        return board if board is not None and board["settings"] is self.settings else None

    def fresh_board(self, max_age):
        with self._refresh_lock:
            board = self.cached_board()
            if board is not None and time.monotonic() - board["loaded_at"] <= max_age:
                return board
            settings = self.settings
            board = {"settings": settings, "groups": None, "error": None, "loaded_at": time.monotonic()}
            try:
                data = fetch_pull_requests(settings["sections"])
                board["groups"] = group_pull_requests(data, settings)
                self.viewer = data["viewer"]
            except subprocess.CalledProcessError as error:
                board["error"] = error.stderr
            except (KeyError, ValueError) as error:
                board["error"] = f"Unexpected response from gh: {error!r}"
            if board["groups"] is not None:
                self.notifier.update(board["groups"], settings)
            self.board = board
            return board

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
                self.fresh_board(FRESH_SECONDS)
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
  .section-card { border: 1px solid #30363d; border-left: 3px solid var(--accent); border-radius: 6px;
                  background: #0d1117; padding: 10px 12px; margin-top: 10px; }
  .section-card input, .section-card select { background: #161b22; }
  .section-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .section-row + .section-row { margin-top: 8px; }
  .section-row .grow { flex: 1 1 260px; }
  .section-row .field { display: inline-flex; gap: 6px; align-items: center; color: #8b949e; font-size: 12px; }
  .section-card:first-child .move-up, .section-card:last-child .move-down { visibility: hidden; }
  .move { padding: 2px 8px; }
  .rule-card { border: 1px solid #30363d; border-radius: 6px; background: #0d1117; padding: 10px 12px; margin-top: 10px; }
  .rule-card input, .rule-card select { background: #161b22; }
  .conditions { display: flex; flex-wrap: wrap; gap: 8px; }
  .conditions:not(:empty) { margin-top: 8px; }
  .condition-row { display: inline-flex; gap: 4px; align-items: center; padding: 4px; border: 1px solid #30363d; border-radius: 6px; }
  .add-rule { margin-top: 10px; }
  .condition-row .description { color: #8b949e; font-size: 12px; padding: 0 4px; }
  .conditions { flex-direction: column; align-items: flex-start; }
  .summary { margin: 8px 0 0; }
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
    <span class="hint" id="status"></span>
    <span class="hint">{refresh_hint(settings)}</span>
    <span class="hint">gh account: {account}</span>
    {button}
  </div>
</header>"""


def page(title, banner_html, content):
    body = f'{banner_html}\n<div class="content">\n{content}\n</div>'
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
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


def board_content(board):
    if board["error"] is not None:
        return f'<pre class="error">gh failed:\n{html.escape(board["error"])}</pre>'
    settings = board["settings"]
    columns = []
    for section, items in zip(settings["sections"], board["groups"]):
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
    return "".join(columns)


def board_script(settings):
    interval = settings["refresh_minutes"] * 60 if settings["auto_refresh"] else 0
    return f"""
const board = document.querySelector('main');
const status = document.getElementById('status');
async function loadBoard(maxAge) {{
  status.textContent = 'Updating…';
  try {{
    const response = await fetch('/board?max_age=' + maxAge);
    if (response.ok) board.innerHTML = await response.text();
    status.textContent = response.ok ? '' : 'Update failed';
  }} catch (error) {{
    status.textContent = 'Update failed';
  }}
}}
loadBoard({FRESH_SECONDS});
if ({interval}) setInterval(() => loadBoard({interval}), {interval} * 1000);
"""


def render_board(overview):
    settings = overview.settings
    board = overview.cached_board()
    content = board_content(board) if board is not None else '<p class="hint">Loading pull requests…</p>'
    content = f"<main>{content}</main>\n<script>{board_script(settings)}</script>"
    return page("GitHub PR overview", banner(settings, overview.viewer, False), content)


def option_list(options, selected):
    return "".join(
        f'<option value="{html.escape(value)}"{" selected" if value == selected else ""}>{html.escape(label)}</option>'
        for value, label in options
    )


def value_choices(condition, rules, own_id):
    if condition in RULE_CONDITIONS:
        return [(rule["id"], rule["name"]) for rule in rules if rule["id"] != own_id]
    return [(value, value) for value in CONDITIONS[condition]["values"]]


def condition_row(rule_index, condition, value, rules, own_id):
    conditions = [(key, details["label"]) for key, details in CONDITIONS.items()]
    return f"""<span class="condition-row">
    <select class="condition-select" name="rule{rule_index}_condition">{option_list(conditions, condition)}</select>
    <select class="value-select" name="rule{rule_index}_value">{option_list(value_choices(condition, rules, own_id), value)}</select>
    <button type="button" class="move" onclick="this.parentElement.remove(); updateRules()" title="Remove condition">&times;</button>
    <span class="description">{html.escape(CONDITIONS[condition]["description"])}</span>
  </span>"""


def rule_card(rule_index, rule, rules):
    conditions = "".join(
        condition_row(rule_index, condition["condition"], condition["value"], rules, rule["id"])
        for condition in rule["conditions"]
    )
    match_modes = [("all", "all"), ("any", "any")]
    return f"""<div class="rule-card" data-rule-index="{rule_index}">
  <div class="section-row">
    <input type="hidden" name="rule{rule_index}_id" value="{html.escape(rule['id'])}">
    <input type="text" class="grow rule-name" name="rule{rule_index}_name" value="{html.escape(rule['name'])}" required>
    <label class="field">Match <select name="rule{rule_index}_match">{option_list(match_modes, rule['match'])}</select> of these conditions</label>
    <button type="button" onclick="addCondition(this)">Add condition</button>
    <button type="button" onclick="removeRule(this)">Delete</button>
  </div>
  <div class="conditions">{conditions}</div>
  <p class="hint summary"></p>
</div>"""


def section_fields(index, section, rules):
    prefix = f"section{index}_"
    rule_choices = [("", EVERY_PULL_REQUEST), *((rule["id"], rule["name"]) for rule in rules)]
    return f"""<div class="section-card" data-index="{index}" style="--accent:{section['color']}">
  <div class="section-row">
    <button type="button" class="move move-up" onclick="moveSection(this, -1)" title="Move up">&uarr;</button>
    <button type="button" class="move move-down" onclick="moveSection(this, 1)" title="Move down">&darr;</button>
    <input type="color" name="{prefix}color" value="{section['color']}" title="Color" oninput="this.closest('.section-card').style.setProperty('--accent', this.value)">
    <input type="text" class="grow" name="{prefix}title" value="{html.escape(section['title'])}" required>
    <label class="field">Priority <input type="number" name="{prefix}priority" min="1" value="{section['priority']}" required></label>
    <label class="field"><input type="checkbox" name="{prefix}notify"{checked_attribute(section['notify'])}> Notify</label>
  </div>
  <div class="section-row">
    <label class="field grow">Query <input type="text" class="grow" name="{prefix}query" value="{html.escape(section['query'])}" required></label>
    <label class="field">Rule <select class="rule-select" name="{prefix}rule">{option_list(rule_choices, section['rule'])}</select></label>
  </div>
</div>"""


SETTINGS_SCRIPT = """
const CONDITIONS = __CONDITIONS__;
const RULE_CONDITIONS = ['rule', 'not_rule'];
const EVERY_PULL_REQUEST = __EVERY_PULL_REQUEST__;
let nextRuleIndex = __NEXT_RULE_INDEX__;

function moveSection(button, step) {
  const card = button.closest('.section-card');
  const sibling = step < 0 ? card.previousElementSibling : card.nextElementSibling;
  if (sibling) {
    step < 0 ? sibling.before(card) : sibling.after(card);
  }
}

function ruleCards() {
  return [...document.querySelectorAll('.rules .rule-card')];
}

function ruleId(card) {
  return card.querySelector('input[type=hidden]').value;
}

function ruleChoices(exceptId) {
  return ruleCards()
    .filter(card => ruleId(card) !== exceptId)
    .map(card => [ruleId(card), card.querySelector('.rule-name').value || 'Rule']);
}

function setOptions(select, choices, fallback) {
  const selected = select.value;
  select.replaceChildren(...choices.map(([value, label]) => new Option(label, value)));
  select.value = choices.some(([value]) => value === selected) ? selected : fallback;
}

function fillValues(row) {
  const condition = row.querySelector('.condition-select').value;
  const values = row.querySelector('.value-select');
  const ownId = ruleId(row.closest('.rule-card'));
  const choices = RULE_CONDITIONS.includes(condition)
    ? ruleChoices(ownId)
    : CONDITIONS[condition].values.map(value => [value, value]);
  setOptions(values, choices, choices.length ? choices[0][0] : '');
  row.querySelector('.description').textContent = CONDITIONS[condition].description;
}

function ruleData() {
  const rules = {};
  for (const card of ruleCards()) {
    rules[ruleId(card)] = {
      name: card.querySelector('.rule-name').value || 'Rule',
      match: card.querySelector('[name$=_match]').value,
      conditions: [...card.querySelectorAll('.condition-row')].map(row =>
        [row.querySelector('.condition-select').value, row.querySelector('.value-select').value]),
    };
  }
  return rules;
}

function describe(id, rules, visited) {
  const rule = rules[id];
  if (!rule.conditions.length) {
    return 'every PR';
  }
  const parts = rule.conditions.map(([condition, value]) => {
    if (!RULE_CONDITIONS.includes(condition)) {
      return CONDITIONS[condition].phrases[value] || '?';
    }
    const target = rules[value];
    if (!target) {
      return '?';
    }
    const verb = condition === 'rule' ? 'matches' : "doesn't match";
    const inner = visited.has(value) ? '' : ' (' + describe(value, rules, new Set([...visited, value])) + ')';
    return verb + ' \u201c' + target.name + '\u201d' + inner;
  });
  if (parts.length === 1) {
    return parts[0];
  }
  return (rule.match === 'all' ? 'all of: ' : 'any of: ') + parts.join(', ');
}

function updateRules() {
  const sectionChoices = [['', EVERY_PULL_REQUEST], ...ruleChoices(null)];
  for (const select of document.querySelectorAll('.rule-select')) {
    setOptions(select, sectionChoices, '');
  }
  for (const row of document.querySelectorAll('.rules .condition-row')) {
    if (RULE_CONDITIONS.includes(row.querySelector('.condition-select').value)) {
      const values = row.querySelector('.value-select');
      const choices = ruleChoices(ruleId(row.closest('.rule-card')));
      setOptions(values, choices, choices.length ? choices[0][0] : '');
    }
  }
  const rules = ruleData();
  for (const card of ruleCards()) {
    const text = describe(ruleId(card), rules, new Set([ruleId(card)]));
    card.querySelector('.summary').textContent = 'Summary: ' + text + '.';
  }
}

function addCondition(button) {
  const card = button.closest('.rule-card');
  const row = document.getElementById('condition-template').innerHTML.replaceAll('__R__', card.dataset.ruleIndex);
  card.querySelector('.conditions').insertAdjacentHTML('beforeend', row);
  updateRules();
}

function addRule() {
  const index = nextRuleIndex++;
  const rules = document.querySelector('.rules');
  rules.insertAdjacentHTML('beforeend', document.getElementById('rule-template').innerHTML.replaceAll('__R__', index));
  const card = rules.lastElementChild;
  card.querySelector('input[type=hidden]').value = 'rule-' + Date.now().toString(36) + '-' + index;
  card.querySelector('.rule-name').select();
  updateRules();
}

function removeRule(button) {
  const card = button.closest('.rule-card');
  const id = ruleId(card);
  const usedBySection = [...document.querySelectorAll('.rule-select')].some(select => select.value === id);
  const usedByRule = [...document.querySelectorAll('.rules .condition-row')].some(row =>
    RULE_CONDITIONS.includes(row.querySelector('.condition-select').value) && row.querySelector('.value-select').value === id);
  if ((usedBySection || usedByRule) && !confirm('Other sections or rules use this rule. Sections using it will show every PR their query finds, and rules lose the conditions that point to it. Delete it?')) {
    return;
  }
  card.remove();
  for (const row of document.querySelectorAll('.rules .condition-row')) {
    if (RULE_CONDITIONS.includes(row.querySelector('.condition-select').value) && row.querySelector('.value-select').value === id) {
      row.remove();
    }
  }
  updateRules();
}

const rulesBox = document.querySelector('.rules');
rulesBox.addEventListener('change', event => {
  if (event.target.classList.contains('condition-select')) {
    fillValues(event.target.closest('.condition-row'));
  }
  updateRules();
});
rulesBox.addEventListener('input', updateRules);
updateRules();

document.querySelector('form').addEventListener('submit', () => {
  const order = [...document.querySelectorAll('.section-card')].map(card => card.dataset.index);
  document.querySelector('[name=section_order]').value = order.join(',');
});
"""


def settings_script(rules):
    return (
        SETTINGS_SCRIPT
        .replace("__CONDITIONS__", json.dumps(CONDITIONS).replace("</", "<\\/"))
        .replace("__EVERY_PULL_REQUEST__", json.dumps(EVERY_PULL_REQUEST))
        .replace("__NEXT_RULE_INDEX__", str(len(rules)))
    )


def render_settings(overview, saved):
    settings = overview.settings
    sound_options = "".join(
        f'<option value="{html.escape(value)}"{" selected" if value == settings["sound"] else ""}>{html.escape(label)}</option>'
        for value, label in [("", "None"), *((name, name) for name in available_sounds())]
    )
    rules = settings["rules"]
    rule_cards = "".join(rule_card(index, rule, rules) for index, rule in enumerate(rules))
    rows = "".join(section_fields(index, section, rules) for index, section in enumerate(settings["sections"]))
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
      (<code>@me</code> is the gh account) that match its rule. Sections appear on the board left to right in this order; use the arrows to move them.</p>
    <label><input type="checkbox" name="first_match_only"{checked_attribute(settings['first_match_only'])}> Show each PR only in one section:
      the matching section with the lowest priority number</label>
    <input type="hidden" name="section_order" value="">
    <div class="sections">{rows}</div>
  </fieldset>
  <fieldset>
    <legend>Rules</legend>
    <p class="hint">A rule is a named set of conditions. It matches a PR when all (or, set to any, at least one) of its conditions hold;
      a rule without conditions matches every PR. A condition can also use another rule. Pick a rule for each section above,
      and reuse a rule in as many sections as you like. A condition that would make rules use each other in a loop is dropped when you save.
      What the query can say is simpler there: for example <code>draft:true</code>, <code>review:approved</code>, <code>status:failure</code> or <code>label:bug</code>.</p>
    <div class="rules">{rule_cards}</div>
    <button type="button" class="add-rule" onclick="addRule()">Add rule</button>
    <template id="rule-template">{rule_card("__R__", {"id": "", "name": "New rule", "match": "all", "conditions": []}, rules)}</template>
    <template id="condition-template">{condition_row("__R__", "draft", "yes", rules, "")}</template>
  </fieldset>
  <div class="actions">
    <button type="submit" class="primary">Save</button>
    <button type="submit" formaction="/settings/reset" onclick="return confirm('Reset every setting to its default?')">Reset to defaults</button>
    {notice}
  </div>
</form>
<script>{settings_script(rules)}</script>"""
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
        elif url.path == "/board":
            max_age = parse_qs(url.query).get("max_age", [""])[0]
            max_age = int(max_age) if max_age.isdigit() else FRESH_SECONDS
            self.respond(board_content(overview.fresh_board(max_age)))
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
