import errno
import html
import json
import os
import shutil
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

DEFAULT_PORT = 8765
FRESH_SECONDS = 15
UPDATE_CHECK_SECONDS = 60 * 60
APP_DIRECTORY = Path(__file__).resolve().parent
LOGIN_ITEM_LABEL = "github-pr-overview"
LOGIN_ITEM_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LOGIN_ITEM_LABEL}.plist"
LOGIN_ITEM_TEMPLATE = APP_DIRECTORY / "github-pr-overview.plist"
LOGIN_ITEM_PATH_DIRECTORIES = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")
LOG_PATH = Path("/tmp/github-pr-overview.log")
INSTANCE_ID = uuid.uuid4().hex
BIND_ATTEMPTS = 40
GITHUB_REMOTE = re.compile(r"(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?")
SETTINGS_PATH = Path.home() / ".config" / "github-pr-overview" / "settings.json"
REPOSITORIES_PATH = SETTINGS_PATH.with_name("repositories.json")
SOUNDS_DIRECTORY = Path("/System/Library/Sounds")
SEARCH_SYNTAX_URL = "https://docs.github.com/en/search-github/searching-on-github/searching-issues-and-pull-requests"

MY_PULL_REQUESTS = "is:pr is:open author:@me archived:false"
REVIEW_REQUESTS = "is:pr is:open archived:false review-requested:@me -author:@me"

YES_NO = ["yes", "no"]
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
MAX_PER_REPO = 100
PARALLEL_FETCHES = 6
GATEWAY_ERRORS = ("HTTP 502", "HTTP 503", "HTTP 504")

CONDITIONS = {
    "state": {
        "label": "State",
        "values": ["open", "merged", "closed"],
        "description": "whether the PR is open, merged, or closed without merging",
        "phrases": {"open": "open", "merged": "merged", "closed": "closed without merging"},
    },
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
    "port": DEFAULT_PORT,
    "collapse_empty_sections": True,
    "auto_refresh": True,
    "refresh_minutes": 2,
    "auto_update": False,
    "notifications": True,
    "sound": "Glass",
    "first_match_only": True,
    "hide_my_prs_in_tracked": True,
    "tracked_per_repo": 20,
    "tracked_repos": [],
    "rules": [
        {"id": "needs_action", "name": "Has a problem", "match": "any", "conditions": [
            {"condition": "changes_requested", "value": "yes"},
            {"condition": "ci", "value": "failing"},
            {"condition": "conflicts", "value": "yes"},
        ]},
        {"id": "waiting", "name": "Reviewers requested", "match": "all",
         "conditions": [{"condition": "reviewers_requested", "value": "yes"}]},
        {"id": "is_draft", "name": "Draft", "match": "all", "conditions": [{"condition": "draft", "value": "yes"}]},
        {"id": "approved", "name": "Approved", "match": "all", "conditions": [{"condition": "approved", "value": "yes"}]},
        {"id": "merged", "name": "Merged", "match": "all", "conditions": [{"condition": "state", "value": "merged"}]},
        {"id": "closed", "name": "Closed", "match": "all", "conditions": [{"condition": "state", "value": "closed"}]},
    ],
    "sections": [
        {"title": "Draft", "color": "#8b949e", "query": f"{MY_PULL_REQUESTS} draft:true", "priority": 1, "notify": False, "rule": ""},
        {"title": "Ready for review (no reviewer)", "color": "#58a6ff", "query": MY_PULL_REQUESTS, "priority": 5, "notify": False, "rule": ""},
        {"title": "Waiting for reviewer", "color": "#d29922", "query": MY_PULL_REQUESTS, "priority": 4, "notify": False, "rule": "waiting"},
        {"title": "Needs action", "color": "#f85149", "query": MY_PULL_REQUESTS, "priority": 2, "notify": True, "rule": "needs_action"},
        {"title": "Ready for merge", "color": "#3fb950", "query": f"{MY_PULL_REQUESTS} review:approved", "priority": 3, "notify": True, "rule": ""},
        {"title": "Needs my review", "color": "#bc8cff", "query": REVIEW_REQUESTS, "priority": 6, "notify": True, "rule": ""},
    ],
    "tracked_sections": [
        {"title": "Draft", "color": "#8b949e", "priority": 3, "notify": False, "rule": "is_draft"},
        {"title": "Open", "color": "#58a6ff", "priority": 7, "notify": False, "rule": ""},
        {"title": "Ready for review", "color": "#d29922", "priority": 6, "notify": False, "rule": "waiting"},
        {"title": "Needs action", "color": "#f85149", "priority": 4, "notify": False, "rule": "needs_action"},
        {"title": "Ready for merge", "color": "#3fb950", "priority": 5, "notify": False, "rule": "approved"},
        {"title": "Merged", "color": "#a371f7", "priority": 1, "notify": False, "rule": "merged"},
        {"title": "Closed", "color": "#6e7681", "priority": 2, "notify": False, "rule": "closed"},
    ],
}

LIST_SETTINGS = ("rules", "sections", "tracked_sections", "tracked_repos")

PULL_REQUEST_FIELDS = """
fragment PullRequestFields on PullRequest {
        title
        url
        number
        isDraft
        mergeable
        state
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


def is_per_repo(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_PER_REPO


def is_port(value):
    return isinstance(value, int) and not isinstance(value, bool) and 1024 <= value <= 65535


def is_sound(value):
    return isinstance(value, str) and (value == "" or value in available_sounds())


VALIDATORS = {
    "port": is_port,
    "collapse_empty_sections": is_bool,
    "auto_refresh": is_bool,
    "refresh_minutes": is_minutes,
    "auto_update": is_bool,
    "notifications": is_bool,
    "sound": is_sound,
    "first_match_only": is_bool,
    "hide_my_prs_in_tracked": is_bool,
    "tracked_per_repo": is_per_repo,
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


def normalized_tracked_section(raw, index, rule_ids):
    fallback = {"title": "Section", "color": "#8b949e", "priority": index + 1, "notify": False}
    section = {key: pick(raw, fallback, key) for key in fallback}
    rule = raw.get("rule") if isinstance(raw.get("rule"), str) else ""
    section["rule"] = rule if rule in rule_ids else ""
    return section


def normalized_repositories(raw):
    names = raw if isinstance(raw, list) else []
    return list(dict.fromkeys(name for name in names if isinstance(name, str) and REPO_NAME.fullmatch(name)))


def non_empty_list(raw, key):
    value = raw.get(key)
    return value if isinstance(value, list) and value else DEFAULT_SETTINGS[key]


def normalized_settings(raw):
    raw = raw if isinstance(raw, dict) else {}
    raw_rules = list(raw["rules"]) if isinstance(raw.get("rules"), list) else list(DEFAULT_SETTINGS["rules"])
    if "tracked_sections" not in raw:
        known = {rule.get("id") for rule in raw_rules if isinstance(rule, dict)}
        raw_rules += [rule for rule in DEFAULT_SETTINGS["rules"] if rule["id"] not in known]
    rules = normalized_rules(raw_rules)
    rule_ids = {rule["id"] for rule in rules}
    settings = {key: pick(raw, DEFAULT_SETTINGS, key) for key in DEFAULT_SETTINGS if key not in LIST_SETTINGS}
    settings["rules"] = rules
    settings["sections"] = [
        normalized_section(section if isinstance(section, dict) else {}, index, rule_ids, "rules" not in raw)
        for index, section in enumerate(non_empty_list(raw, "sections"))
    ]
    settings["tracked_sections"] = [
        normalized_tracked_section(section if isinstance(section, dict) else {}, index, rule_ids)
        for index, section in enumerate(non_empty_list(raw, "tracked_sections"))
    ]
    settings["tracked_repos"] = normalized_repositories(raw.get("tracked_repos"))
    return settings


def load_settings():
    try:
        return normalized_settings(json.loads(SETTINGS_PATH.read_text()))
    except (FileNotFoundError, ValueError):
        return normalized_settings({})


def port_is_free(port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def board_url(port):
    return f"http://127.0.0.1:{port}"


def save_settings(settings):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = SETTINGS_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(settings, indent=2))
    os.replace(temporary_path, SETTINGS_PATH)


def load_repository_list():
    try:
        return json.loads(REPOSITORIES_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return None


def save_repository_list(repository_list):
    REPOSITORIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = REPOSITORIES_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(repository_list))
    os.replace(temporary_path, REPOSITORIES_PATH)


def section_order(form, count, field):
    try:
        order = [int(index) for index in form.get(field, [""])[0].split(",")]
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

    def count_of(prefix):
        count = 0
        while f"{prefix}{count}_title" in form:
            count += 1
        return count

    rule_indices = sorted(int(match.group(1)) for key in form if (match := re.fullmatch(r"rule(\d+)_id", key)))
    return normalized_settings({
        "port": number("port"),
        "collapse_empty_sections": checked("collapse_empty_sections"),
        "auto_refresh": checked("auto_refresh"),
        "refresh_minutes": number("refresh_minutes"),
        "auto_update": checked("auto_update"),
        "notifications": checked("notifications"),
        "sound": text("sound"),
        "first_match_only": checked("first_match_only"),
        "hide_my_prs_in_tracked": checked("hide_my_prs_in_tracked"),
        "tracked_per_repo": number("tracked_per_repo"),
        "tracked_repos": form.get("tracked_repo", []),
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
            for index in section_order(form, count_of("section"), "section_order")
        ],
        "tracked_sections": [
            {
                "title": text(f"tracked{index}_title"),
                "color": text(f"tracked{index}_color"),
                "priority": number(f"tracked{index}_priority"),
                "notify": checked(f"tracked{index}_notify"),
                "rule": text(f"tracked{index}_rule"),
            }
            for index in section_order(form, count_of("tracked"), "tracked_order")
        ],
    })


def section_queries(sections):
    return list(dict.fromkeys(section["query"] for section in sections))


def build_query(query_count, repository_count, per_repo):
    variables = ", ".join(
        [f"$q{index}: String!" for index in range(query_count)]
        + [f"$o{index}: String!, $n{index}: String!" for index in range(repository_count)]
    )
    searches = "".join(
        f"  q{index}: search(query: $q{index}, type: ISSUE, first: 100) {{ nodes {{ ...PullRequestFields }} }}\n"
        for index in range(query_count)
    )
    repositories = "".join(
        f"  r{index}: repository(owner: $o{index}, name: $n{index}) {{ pullRequests(first: {per_repo}, "
        f"orderBy: {{field: UPDATED_AT, direction: DESC}}) {{ nodes {{ ...PullRequestFields }} }} }}\n"
        for index in range(repository_count)
    )
    return f"query({variables}) {{\n  viewer {{ login url }}\n{searches}{repositories}}}\n{PULL_REQUEST_FIELDS}"


def run_graphql_once(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True)
    try:
        response = json.loads(result.stdout) if result.stdout.strip() else {}
    except ValueError:
        message = result.stderr.strip() or "gh returned a response that isn't JSON"
        if any(code in message for code in GATEWAY_ERRORS):
            message += " (GitHub gave up on the query, usually because it took longer than 10 seconds)"
        raise subprocess.CalledProcessError(result.returncode, arguments, result.stdout, message)
    if not response.get("data"):
        raise subprocess.CalledProcessError(result.returncode, arguments, result.stdout, result.stderr or "gh returned no data")
    return response["data"], response.get("errors") or []


def run_graphql(arguments):
    try:
        return run_graphql_once(arguments)
    except subprocess.CalledProcessError as error:
        if not any(code in (error.stderr or "") for code in GATEWAY_ERRORS):
            raise
        return run_graphql_once(arguments)


def search_arguments(queries):
    arguments = ["gh", "api", "graphql", "-f", f"query={build_query(len(queries), 0, 0)}"]
    for index, search in enumerate(queries):
        arguments += ["-f", f"q{index}={search}"]
    return arguments


def fetch_searches(queries):
    data, errors = run_graphql(search_arguments(queries))
    if errors:
        raise subprocess.CalledProcessError(1, "gh api graphql", "", "; ".join(error.get("message", json.dumps(error)) for error in errors))
    return data


def fetch_repository(repository, per_repo):
    owner, name = repository.split("/", 1)
    arguments = ["gh", "api", "graphql", "-f", f"query={build_query(0, 1, per_repo)}", "-f", f"o0={owner}", "-f", f"n0={name}"]
    try:
        data, errors = run_graphql(arguments)
    except subprocess.CalledProcessError as error:
        return None, (error.stderr or "gh failed").strip()
    if errors or not data.get("r0"):
        return None, "; ".join(error.get("message", "unknown error") for error in errors) or "no data"
    return data["r0"], None


def fetch_pull_requests(settings):
    queries = section_queries(settings["sections"])
    repositories = settings["tracked_repos"]
    with ThreadPoolExecutor(PARALLEL_FETCHES) as pool:
        searches = pool.submit(fetch_searches, queries)
        results = list(pool.map(lambda repository: fetch_repository(repository, settings["tracked_per_repo"]), repositories))
        data = searches.result()
    repository_errors = {}
    for index, (repository, (result, error)) in enumerate(zip(repositories, results)):
        data[f"r{index}"] = result
        if error is not None:
            repository_errors[repository] = error
    return data, repository_errors


REPOSITORY_LIST_QUERY = """
query($after: String) {
  viewer {
    login
    organizations(first: 100) { nodes { login } }
    repositories(first: 100, after: $after, orderBy: {field: NAME, direction: ASC},
                 affiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER],
                 ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
      pageInfo { hasNextPage endCursor }
      nodes { nameWithOwner isPrivate isArchived owner { login } }
    }
  }
}
"""


def fetch_repository_list():
    repositories, after = [], None
    while True:
        arguments = ["gh", "api", "graphql", "-f", f"query={REPOSITORY_LIST_QUERY}"]
        if after:
            arguments += ["-f", f"after={after}"]
        viewer = run_graphql(arguments)[0]["viewer"]
        page = viewer["repositories"]
        repositories += [
            {"name": node["nameWithOwner"], "owner": node["owner"]["login"], "private": node["isPrivate"], "archived": node["isArchived"]}
            for node in page["nodes"]
            if node
        ]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    return {
        "login": viewer["login"],
        "organizations": [node["login"] for node in viewer["organizations"]["nodes"] if node],
        "repositories": repositories,
        "loaded_at": time.strftime("%Y-%m-%d %H:%M"),
    }


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
        "state": pr["state"].lower(),
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
    if facts["state"] != "open":
        return [f"by {pr['author']['login']}"] if pr["author"] else []
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


def place_in_sections(sections, nodes_of, settings, viewer_login, hidden):
    rules_by_id = {rule["id"]: rule for rule in settings["rules"]}
    groups = [[] for _ in sections]
    placed = set()
    for index in sorted(range(len(sections)), key=lambda position: (sections[position]["priority"], position)):
        section = sections[index]
        for pr in by_last_update(nodes_of(section)):
            if pr["url"] in hidden or (settings["first_match_only"] and pr["url"] in placed):
                continue
            facts = pull_request_facts(pr, viewer_login)
            rule = rules_by_id.get(section["rule"])
            if rule is None or rule_matches(rule, facts, rules_by_id):
                groups[index].append((pr, card_tags(pr, facts)))
                placed.add(pr["url"])
    return groups


def valid_nodes(nodes):
    return [node for node in nodes if node and "url" in node]


def group_pull_requests(data, settings):
    queries = section_queries(settings["sections"])
    viewer_login = data["viewer"]["login"]
    groups = place_in_sections(
        settings["sections"],
        lambda section: valid_nodes(data[f"q{queries.index(section['query'])}"]["nodes"]),
        settings,
        viewer_login,
        set(),
    )
    tracked_nodes = [
        node
        for index in range(len(settings["tracked_repos"]))
        if data.get(f"r{index}")
        for node in valid_nodes(data[f"r{index}"]["pullRequests"]["nodes"])
    ]
    shown = {pr["url"] for items in groups for pr, _ in items} if settings["hide_my_prs_in_tracked"] else set()
    tracked = place_in_sections(settings["tracked_sections"], lambda section: tracked_nodes, settings, viewer_login, shown)
    return groups, tracked


def board_entries(settings, groups, tracked):
    return [
        *((("mine", index), section, items) for index, (section, items) in enumerate(zip(settings["sections"], groups))),
        *((("tracked", index), section, items) for index, (section, items) in enumerate(zip(settings["tracked_sections"], tracked))),
    ]


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

    def update(self, entries, settings):
        current = {
            (key, pr["url"]): (section["title"], pr, tags)
            for key, section, items in entries
            if section["notify"]
            for pr, tags in items
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
        self.repository_list = load_repository_list()
        self._repository_lock = threading.Lock()

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
            board = {
                "settings": settings,
                "groups": None,
                "tracked": None,
                "repository_errors": {},
                "error": None,
                "loaded_at": time.monotonic(),
            }
            try:
                data, board["repository_errors"] = fetch_pull_requests(settings)
                board["groups"], board["tracked"] = group_pull_requests(data, settings)
                self.viewer = data["viewer"]
            except subprocess.CalledProcessError as error:
                board["error"] = error.stderr
            except (KeyError, ValueError) as error:
                board["error"] = f"Unexpected response from gh: {error!r}"
            if board["groups"] is not None:
                self.notifier.update(board_entries(settings, board["groups"], board["tracked"]), settings)
            self.board = board
            return board

    def repositories(self, reload):
        with self._repository_lock:
            if reload or self.repository_list is None:
                self.repository_list = fetch_repository_list()
                save_repository_list(self.repository_list)
            return self.repository_list

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


def run_git(*arguments):
    return subprocess.run(
        ["git", "-C", str(APP_DIRECTORY), *arguments],
        capture_output=True, text=True, check=True, timeout=120,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    ).stdout.strip()


def git_failure(error):
    if isinstance(error, subprocess.CalledProcessError):
        return (error.stderr or error.stdout or "").strip() or f"git exited with {error.returncode}"
    if isinstance(error, subprocess.TimeoutExpired):
        return "git took too long"
    return "git isn't available"


GIT_ERRORS = (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError)


def commit_url(commit):
    try:
        match = GITHUB_REMOTE.fullmatch(run_git("remote", "get-url", "origin"))
    except GIT_ERRORS:
        return ""
    return f"https://github.com/{match.group(1)}/commit/{commit}" if match else ""


def current_version():
    try:
        commit = run_git("rev-parse", "HEAD")
        subject = run_git("log", "-1", "--format=%s")
    except GIT_ERRORS:
        return None
    return {"commit": commit, "short": commit[:7], "subject": subject, "url": commit_url(commit)}


class Updater:
    def __init__(self, overview, restart):
        self.overview = overview
        self.restart = restart
        self.version = current_version()
        self.pending = 0
        self.checked_at = None
        self.error = None
        self.wake = threading.Event()
        self._lock = threading.Lock()

    def check(self):
        if self.version is None:
            return 0
        with self._lock:
            try:
                run_git("fetch", "--quiet", "origin")
                self.pending = int(run_git("rev-list", "--count", "HEAD", "@{upstream}", f"^{self.version['commit']}"))
                self.error = None
            except GIT_ERRORS as error:
                self.error = git_failure(error)
            except ValueError:
                self.error = "Unexpected answer from git"
            self.checked_at = time.time()
            return self.pending

    def install(self):
        with self._lock:
            try:
                run_git("pull", "--ff-only", "--quiet")
                commit = run_git("rev-parse", "HEAD")
            except GIT_ERRORS as error:
                self.error = git_failure(error)
                return None
            self.error = None
            self.pending = 0
            return commit

    def install_and_restart(self):
        commit = self.install()
        if commit is not None and commit != self.version["commit"]:
            self.restart()
        return commit

    def watch(self):
        if self.version is None:
            return
        while True:
            if self.check() and self.overview.settings["auto_update"]:
                self.install_and_restart()
            self.wake.wait(UPDATE_CHECK_SECONDS)
            self.wake.clear()


def app_command():
    return [shutil.which("python3") or sys.executable, str(APP_DIRECTORY / Path(__file__).name), "--no-browser"]


def restart_process():
    command = app_command()
    os.execv(command[0], command)


def stop_server(server, after_stop):
    server.after_stop = after_stop
    threading.Thread(target=server.shutdown, daemon=True).start()


def restart_server(server):
    stop_server(server, restart_process)


def launchd_service():
    return f"gui/{os.getuid()}/{LOGIN_ITEM_LABEL}"


def run_launchctl(*arguments):
    return subprocess.run(["launchctl", *arguments], capture_output=True, text=True, timeout=30)


def runs_as_login_item():
    try:
        result = run_launchctl("print", launchd_service())
    except (OSError, subprocess.TimeoutExpired):
        return False
    match = re.search(r"^\s*pid = (\d+)$", result.stdout, re.MULTILINE)
    return result.returncode == 0 and match is not None and int(match.group(1)) == os.getpid()


def read_property_list(path):
    result = subprocess.run(["plutil", "-convert", "json", "-o", "-", str(path)], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or f"plutil couldn't read {path}")
    return json.loads(result.stdout)


def login_item_program():
    if not LOGIN_ITEM_PATH.exists():
        return None
    try:
        arguments = read_property_list(LOGIN_ITEM_PATH).get("ProgramArguments", [])
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return ""
    return arguments[1] if isinstance(arguments, list) and len(arguments) > 1 else ""


def login_item_search_path():
    tools = (shutil.which(name) for name in ("gh", "terminal-notifier", "git"))
    directories = [str(Path(tool).parent) for tool in tools if tool]
    directories += [directory for directory in LOGIN_ITEM_PATH_DIRECTORIES if Path(directory).is_dir()]
    return ":".join(dict.fromkeys(directories))


def write_login_item():
    login_item = read_property_list(LOGIN_ITEM_TEMPLATE)
    login_item["Label"] = LOGIN_ITEM_LABEL
    login_item["ProgramArguments"] = app_command()
    login_item["EnvironmentVariables"] = {**login_item.get("EnvironmentVariables", {}), "PATH": login_item_search_path()}
    LOGIN_ITEM_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = LOGIN_ITEM_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(login_item))
    result = subprocess.run(["plutil", "-convert", "xml1", str(temporary_path)], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        temporary_path.unlink(missing_ok=True)
        raise ValueError(result.stderr.strip() or "plutil couldn't write the login item")
    os.replace(temporary_path, LOGIN_ITEM_PATH)


def hand_over_to_login_item():
    run_launchctl("bootout", launchd_service())
    if run_launchctl("bootstrap", f"gui/{os.getuid()}", str(LOGIN_ITEM_PATH)).returncode != 0:
        restart_process()
    sys.exit(0)


def leave_login_item():
    with LOG_PATH.open("ab") as log:
        subprocess.Popen(app_command(), stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    run_launchctl("bootout", launchd_service())
    sys.exit(0)


STYLE = """
  body { margin: 0; background: #0d1117; color: #e6edf3;
         font: 14px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  a { color: #58a6ff; }
  .banner { position: sticky; top: 0; z-index: 1; display: flex; justify-content: space-between; align-items: center;
            gap: 16px; padding: 12px 24px; background: #161b22; border-bottom: 1px solid #30363d; }
  .banner h1 { font-size: 18px; margin: 0; }
  .banner h1 a { color: inherit; text-decoration: none; }
  .content { padding: 24px; }
  .settings { padding: 0 15%; }
  @media (max-width: 900px) { .settings { padding: 0; } }
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
  .columns { display: flex; gap: 12px; align-items: flex-start; }
  .row + .row { margin-top: 24px; }
  h2.row-title { font-size: 15px; margin: 0 0 10px; display: block; }
  .row-errors { margin-bottom: 10px; }
  section { flex: 1 1 0; min-width: 200px; background: #161b22; border: 1px solid #30363d;
            border-top: 3px solid var(--accent); border-radius: 8px; padding: 12px; }
  section.collapsed { flex: 0 0 auto; min-width: 0; padding: 12px 8px; }
  section.collapsed h2 { writing-mode: vertical-rl; margin: 0; gap: 8px; color: #8b949e; }
  section.collapsed .card { display: none; }
  section h2, section.collapsed { cursor: pointer; user-select: none; }
  section h2:hover, section.collapsed:hover h2 { color: var(--accent); }
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
  .notice.warning { color: #d29922; }
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
  dialog { background: #161b22; color: #e6edf3; border: 1px solid #30363d; border-radius: 8px; padding: 20px; max-width: 420px; }
  dialog::backdrop { background: rgba(1, 4, 9, 0.7); }
  dialog h2 { font-size: 16px; margin: 0 0 8px; display: block; }
  dialog p { margin: 0 0 16px; color: #8b949e; }
  dialog .actions { justify-content: flex-end; }
  .repo-tools { display: flex; gap: 8px; align-items: center; margin: 8px 0; }
  .repo-tools input[type=search] { flex: 1 1 260px; }
  .repo-picker { max-height: 360px; overflow: auto; border: 1px solid #30363d; border-radius: 6px; background: #0d1117; padding: 4px 12px; }
  .repo-group h3 { font-size: 12px; color: #8b949e; margin: 10px 0 4px; }
  .repo-option { display: flex; gap: 6px; align-items: center; padding: 2px 0; }
  .badge { font-size: 11px; padding: 0 6px; border-radius: 10px; border: 1px solid #30363d; color: #8b949e; }
  .badge.private { color: #d29922; border-color: #d29922; }
  .badge.missing { color: #f85149; border-color: #f85149; }
  .repo-option[hidden], .repo-group[hidden] { display: none; }
  .title-block { display: flex; gap: 10px; align-items: center; }
  .version { color: #8b949e; font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; text-decoration: none; }
  .version:hover { color: #e6edf3; text-decoration: underline; }
  .update-form { display: inline-flex; margin: 0; }
  .update-form[hidden] { display: none; }
  .update-button { display: inline-flex; padding: 2px; border: none; background: none; color: #3fb950; }
  .update-button svg { width: 16px; height: 16px; fill: currentColor; }
  .update-button:hover { color: #56d364; }
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


UPDATE_ICON = (
    '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0a8 8 0 1 1 0 16A8 8 0 0 1 8 0Zm.53 4.22a.75.75 0 0 0-1.06 0'
    'L4.72 6.97a.75.75 0 0 0 1.06 1.06l1.47-1.47v4.69a.75.75 0 0 0 1.5 0V6.56l1.47 1.47a.75.75 0 1 0 1.06-1.06Z"/></svg>'
)


def update_title(pending):
    return f"{pending} new commit{'s' if pending != 1 else ''} on GitHub. Click to update and restart the board."


def version_badge(updater):
    version = updater.version
    if version is None:
        return ""
    label = html.escape(version["short"])
    title = html.escape(version["subject"], quote=True)
    if version["url"]:
        badge = f'<a class="version" href="{html.escape(version["url"])}" target="_blank" title="{title}">{label}</a>'
    else:
        badge = f'<span class="version" title="{title}">{label}</span>'
    hidden = "" if updater.pending else " hidden"
    return f"""{badge}
    <form class="update-form" id="update-form" method="post" action="/update"{hidden}>
      <button class="update-button" type="submit" title="{html.escape(update_title(updater.pending))}" aria-label="Update">{UPDATE_ICON}</button>
    </form>"""


def banner(settings, viewer, on_settings, updater):
    if viewer:
        account = f'<a class="account" href="{html.escape(viewer["url"])}" target="_blank">@{html.escape(viewer["login"])}</a>'
    else:
        account = "unknown"
    if on_settings:
        button = f'<a class="button icon" href="/" title="Back to the board" aria-label="Back to the board">{BACK_ICON}</a>'
    else:
        button = f'<a class="button icon" href="/settings" title="Settings" aria-label="Settings">{GEAR_ICON}</a>'
    return f"""<header class="banner">
  <div class="title-block">
    <h1><a href="/">GitHub PR overview</a></h1>
    {version_badge(updater)}
  </div>
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


def section_columns(settings, kind, sections, groups):
    columns = []
    for section, items in zip(sections, groups):
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
            f"""<section{collapsed} data-key="{kind}:{html.escape(section['title'])}" style="--accent:{section['color']}">
                  <h2 title="Collapse or expand">{html.escape(section['title'])} <span class="count">{len(items)}</span></h2>
                  {cards}
                </section>"""
        )
    return f'<div class="columns">{"".join(columns)}</div>'


def board_content(board):
    if board["error"] is not None:
        return f'<pre class="error">gh failed:\n{html.escape(board["error"])}</pre>'
    settings = board["settings"]
    mine = section_columns(settings, "mine", settings["sections"], board["groups"])
    if not settings["tracked_repos"]:
        return mine
    errors = "".join(
        f'<pre class="error row-errors">Couldn\'t load {html.escape(repository)}: {html.escape(message)}</pre>'
        for repository, message in board["repository_errors"].items()
    )
    tracked = section_columns(settings, "tracked", settings["tracked_sections"], board["tracked"])
    count = len(settings["tracked_repos"])
    return f"""<div class="row"><h2 class="row-title">My PRs</h2>{mine}</div>
<div class="row"><h2 class="row-title">Tracked repositories <span class="hint">{count} repo{'s' if count != 1 else ''}</span></h2>{errors}{tracked}</div>"""


def board_script(settings, updater):
    interval = settings["refresh_minutes"] * 60 if settings["auto_refresh"] else 0
    commit = json.dumps(updater.version["commit"] if updater.version else "")
    return f"""
const board = document.querySelector('main');
const PAGE_COMMIT = {commit};
function applyVersion(response) {{
  const commit = response.headers.get('X-App-Commit') || '';
  if (commit && PAGE_COMMIT && commit !== PAGE_COMMIT) {{
    location.reload();
    return;
  }}
  const form = document.getElementById('update-form');
  if (!form) return;
  const pending = Number(response.headers.get('X-App-Updates') || 0);
  form.hidden = !pending;
  form.querySelector('button').title = response.headers.get('X-App-Update-Title') || '';
}}
const status = document.getElementById('status');
const COLLAPSE_KEY = 'collapsed-sections';
function collapseChoices() {{
  try {{
    return JSON.parse(localStorage.getItem(COLLAPSE_KEY)) || {{}};
  }} catch (error) {{
    return {{}};
  }}
}}
function applyCollapseChoices() {{
  const choices = collapseChoices();
  for (const section of board.querySelectorAll('section[data-key]')) {{
    if (section.dataset.key in choices) section.classList.toggle('collapsed', choices[section.dataset.key]);
  }}
}}
board.addEventListener('click', event => {{
  const section = event.target.closest('section[data-key]');
  if (!section || event.target.closest('.card') || !(event.target.closest('h2') || section.classList.contains('collapsed'))) return;
  const choices = collapseChoices();
  choices[section.dataset.key] = section.classList.toggle('collapsed');
  localStorage.setItem(COLLAPSE_KEY, JSON.stringify(choices));
}});
applyCollapseChoices();
async function loadBoard(maxAge) {{
  status.textContent = 'Updating…';
  try {{
    const response = await fetch('/board?max_age=' + maxAge);
    applyVersion(response);
    if (response.ok) {{
      board.innerHTML = await response.text();
      applyCollapseChoices();
    }}
    status.textContent = response.ok ? '' : 'Update failed';
  }} catch (error) {{
    status.textContent = 'Update failed';
  }}
}}
loadBoard({FRESH_SECONDS});
if ({interval}) setInterval(() => loadBoard({interval}), {interval} * 1000);
"""


def render_board(overview, updater):
    settings = overview.settings
    board = overview.cached_board()
    content = board_content(board) if board is not None else '<p class="hint">Loading pull requests…</p>'
    content = f"<main>{content}</main>\n<script>{board_script(settings, updater)}</script>"
    return page("GitHub PR overview", banner(settings, overview.viewer, False, updater), content)


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


def section_fields(kind, index, section, rules):
    prefix = f"{kind}{index}_"
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
    {query_field(prefix, section)}
    <label class="field">Rule <select class="rule-select" name="{prefix}rule">{option_list(rule_choices, section['rule'])}</select></label>
  </div>
</div>"""


def query_field(prefix, section):
    if "query" not in section:
        return '<span class="field grow">Every PR in the tracked repositories</span>'
    return f'<label class="field grow">Query <input type="text" class="grow" name="{prefix}query" value="{html.escape(section["query"])}" required></label>'


def repository_badges(repository):
    if repository is None:
        return ""
    badges = ['<span class="badge private">private</span>' if repository["private"] else '<span class="badge">public</span>']
    if repository["archived"]:
        badges.append('<span class="badge">archived</span>')
    return "".join(badges)


def repository_option(name, checked, repository):
    return (
        f'<label class="repo-option" data-name="{html.escape(name.lower())}">'
        f'<input type="checkbox" name="tracked_repo" value="{html.escape(name)}"{checked_attribute(checked)}> '
        f'{html.escape(name)} {repository_badges(repository)}</label>'
    )


def repository_group(title, options, empty_hint):
    body = "".join(options) if options else f'<p class="hint">{empty_hint}</p>'
    return f'<div class="repo-group"><h3>{html.escape(title)} <span class="count">{len(options)}</span></h3>{body}</div>'


def selected_repositories(settings, repository_list):
    known = {repository["name"]: repository for repository in repository_list["repositories"]} if repository_list else {}
    options = [repository_option(name, True, known.get(name)) for name in settings["tracked_repos"]]
    body = "".join(options) if options else '<p class="hint">None yet. Check repositories in the list below to track them.</p>'
    return f'<div class="repo-group" id="repo-selected"><h3>Tracked</h3>{body}</div>'


def repository_groups(repository_list):
    login = repository_list["login"]
    by_owner = {}
    for repository in repository_list["repositories"]:
        by_owner.setdefault(repository["owner"], []).append(repository)
    own = by_owner.pop(login, [])
    sso_hint = (
        "No repositories visible. If this org uses SSO, run <code>gh auth refresh</code> "
        "and authorize the token for it on github.com/settings/tokens."
    )
    groups = [
        repository_group(f"Personal · public ({login})", [repository_option(r["name"], False, r) for r in own if not r["private"]], "None."),
        repository_group(f"Personal · private ({login})", [repository_option(r["name"], False, r) for r in own if r["private"]], "None."),
    ]
    organizations = sorted(set(repository_list["organizations"]), key=str.lower)
    for organization in organizations:
        options = [repository_option(r["name"], False, r) for r in by_owner.pop(organization, [])]
        groups.append(repository_group(f"Org · {organization}", options, sso_hint))
    for owner in sorted(by_owner, key=str.lower):
        groups.append(repository_group(f"{owner} (collaborator)", [repository_option(r["name"], False, r) for r in by_owner[owner]], "None."))
    total = len(repository_list["repositories"])
    summary = (
        f'<p class="hint">{total} repositories the gh account can see, listed {html.escape(repository_list["loaded_at"])}. '
        "Orgs only show repositories the gh token is authorized for.</p>"
    )
    return summary + "".join(groups)


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

document.getElementById('settings-form').addEventListener('submit', () => {
  for (const [box, field] of [['.sections', 'section_order'], ['.tracked-sections', 'tracked_order']]) {
    const order = [...document.querySelectorAll(box + ' .section-card')].map(card => card.dataset.index);
    document.querySelector('[name=' + field + ']').value = order.join(',');
  }
});

const repoGroups = document.getElementById('repo-groups');
const repoStatus = document.getElementById('repo-status');
const repoFilter = document.getElementById('repo-filter');

function filterRepositories() {
  const term = repoFilter.value.trim().toLowerCase();
  for (const option of document.querySelectorAll('.repo-picker .repo-option')) {
    option.hidden = Boolean(term) && !option.dataset.name.includes(term);
  }
  for (const group of repoGroups.querySelectorAll('.repo-group')) {
    group.hidden = Boolean(term) && !group.querySelector('.repo-option:not([hidden])');
  }
}

async function loadRepositories(reload) {
  const chosen = new Set([...document.querySelectorAll('input[name=tracked_repo]:checked')].map(input => input.value));
  repoStatus.textContent = reload ? 'Reloading the list from GitHub…' : 'Loading your repositories…';
  let response;
  try {
    response = await fetch('/repositories' + (reload ? '?reload=1' : ''));
    repoGroups.innerHTML = await response.text();
    repoStatus.textContent = response.ok ? '' : 'Loading the list failed';
  } catch (error) {
    repoStatus.textContent = 'Loading the list failed';
    return;
  }
  const selected = new Set([...document.querySelectorAll('#repo-selected input')].map(input => input.value));
  const listed = new Set();
  for (const input of repoGroups.querySelectorAll('input[name=tracked_repo]')) {
    listed.add(input.value);
    if (selected.has(input.value)) {
      input.closest('.repo-option').remove();
    } else {
      input.checked = chosen.has(input.value);
    }
  }
  for (const input of document.querySelectorAll('#repo-selected input')) {
    const option = input.closest('.repo-option');
    option.querySelector('.missing')?.remove();
    if (response.ok && !listed.has(input.value)) {
      option.insertAdjacentHTML('beforeend', '<span class="badge missing">not visible to the gh account</span>');
    }
  }
  filterRepositories();
}

repoFilter.addEventListener('input', filterRepositories);
loadRepositories(false);

const settingsForm = document.getElementById('settings-form');
const leaveDialog = document.getElementById('leave-dialog');
function formState() {
  return JSON.stringify([...new FormData(settingsForm)].filter(([name]) => name !== 'next'));
}
let savedState = formState();
let leaving = false;
let destination = null;
setTimeout(() => { savedState = formState(); }, 0);
const hasChanges = () => !leaving && formState() !== savedState;

settingsForm.addEventListener('submit', () => { leaving = true; });
window.addEventListener('beforeunload', event => {
  if (hasChanges()) {
    event.preventDefault();
    event.returnValue = '';
  }
});
document.addEventListener('click', event => {
  const link = event.target.closest('a[href]');
  if (!link || link.target === '_blank' || event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || !hasChanges()) {
    return;
  }
  event.preventDefault();
  destination = link.getAttribute('href');
  leaveDialog.showModal();
});
document.getElementById('leave-save').addEventListener('click', () => {
  leaveDialog.close();
  settingsForm.querySelector('[name=next]').value = destination;
  settingsForm.requestSubmit(settingsForm.querySelector('button.primary'));
});
document.getElementById('leave-discard').addEventListener('click', () => {
  leaving = true;
  location.href = destination;
});
document.getElementById('leave-stay').addEventListener('click', () => leaveDialog.close());

const checkUpdates = document.getElementById('check-updates');
checkUpdates.addEventListener('click', async () => {
  const updateStatus = document.getElementById('update-status');
  checkUpdates.disabled = true;
  updateStatus.textContent = 'Checking…';
  try {
    const response = await fetch('/update/check', {method: 'POST'});
    updateStatus.textContent = await response.text();
    const form = document.getElementById('update-form');
    if (form) {
      form.hidden = !Number(response.headers.get('X-App-Updates') || 0);
      form.querySelector('button').title = response.headers.get('X-App-Update-Title') || '';
    }
  } catch (error) {
    updateStatus.textContent = 'Check failed';
  }
  checkUpdates.disabled = false;
});
"""


def settings_script(rules):
    return (
        SETTINGS_SCRIPT
        .replace("__CONDITIONS__", json.dumps(CONDITIONS).replace("</", "<\\/"))
        .replace("__EVERY_PULL_REQUEST__", json.dumps(EVERY_PULL_REQUEST))
        .replace("__NEXT_RULE_INDEX__", str(len(rules)))
    )


def local_path(value):
    parts = urlsplit(value)
    if value.startswith("/") and not value.startswith("//") and "\\" not in value and not parts.scheme and not parts.netloc:
        return value
    return ""


def render_restarting(title, message_html, next_path):
    target = json.dumps(next_path or "/").replace("</", "<\\/")
    content = f"""<h1>{html.escape(title)}</h1>
<p>{message_html} This page goes back when it's up.</p>
<script>
async function follow() {{
  try {{
    const response = await fetch('/instance', {{cache: 'no-store'}});
    if (response.ok && (await response.text()) !== {json.dumps(INSTANCE_ID)}) {{
      location.href = {target};
      return;
    }}
  }} catch (error) {{
  }}
  setTimeout(follow, 300);
}}
setTimeout(follow, 300);
</script>"""
    return page(f"{title} · GitHub PR overview", "", content)


def render_updating(commit, next_path):
    message = f"The board updated to <code>{html.escape(commit[:7])}</code> and is restarting."
    return render_restarting("Updating", message, next_path)


def update_status(updater):
    if updater.version is None:
        return "Updates need the board to run from a git clone."
    parts = []
    if updater.checked_at is not None:
        parts.append(f"Last checked at {time.strftime('%H:%M', time.localtime(updater.checked_at))}")
    if updater.error:
        parts.append(f"git failed: {updater.error}")
    elif updater.checked_at is not None:
        pending = updater.pending
        parts.append(f"{pending} new commit{'s' if pending != 1 else ''} waiting; click the green arrow to install" if pending else "Up to date")
    return ". ".join(parts)


def render_moving(port, next_path):
    url = board_url(port)
    target = json.dumps(url + (next_path or "/settings?saved")).replace("</", "<\\/")
    content = f"""<h1>Moving to port {port}</h1>
<p>The board is restarting on <a href="{url}{html.escape(next_path or '/settings?saved')}">{url}</a>. This page follows it when it's up.</p>
<script>
async function follow() {{
  try {{
    await fetch('{url}/', {{mode: 'no-cors', cache: 'no-store'}});
    location.href = {target};
  }} catch (error) {{
    setTimeout(follow, 300);
  }}
}}
setTimeout(follow, 300);
</script>"""
    return page("Moving · GitHub PR overview", "", content)


def version_line(updater):
    version = updater.version
    if version is None:
        return "an unknown version"
    short = html.escape(version["short"])
    commit = f'<a href="{html.escape(version["url"])}" target="_blank"><code>{short}</code></a>' if version["url"] else f"<code>{short}</code>"
    return f'{commit} <span class="hint">{html.escape(version["subject"])}</span>'


def login_item_fields():
    program = login_item_program()
    this_app = str(APP_DIRECTORY / Path(__file__).name)
    if program is None:
        status = "The board doesn't start at login. It runs only as long as the process that started it."
        button = '<button type="submit" form="login-item-form" formaction="/login-item/enable">Start at login</button>'
    else:
        status = "The board starts at login and restarts by itself if it stops."
        if program != this_app:
            status += f' <span class="notice warning">The login item runs another copy: <code>{html.escape(program)}</code>.</span>'
        button = '<button type="submit" form="login-item-form" formaction="/login-item/disable">Don\'t start at login</button>'
        if program != this_app:
            button = '<button type="submit" form="login-item-form" formaction="/login-item/enable">Use this copy</button> ' + button
    return f"""<p>{status}</p>
    <p class="actions">{button}</p>
    <p class="hint">This adds or removes a macOS login item (<code>{html.escape(str(LOGIN_ITEM_PATH).replace(str(Path.home()), "~", 1))}</code>),
      the same one the installer sets up; it logs to <code>{LOG_PATH}</code>. Switching restarts the board, and this page follows it.
      Unsaved settings on this page are lost, so save them first.</p>"""


def render_settings(overview, updater, saved, port_in_use):
    settings = overview.settings
    sound_options = "".join(
        f'<option value="{html.escape(value)}"{" selected" if value == settings["sound"] else ""}>{html.escape(label)}</option>'
        for value, label in [("", "None"), *((name, name) for name in available_sounds())]
    )
    rules = settings["rules"]
    rule_cards = "".join(rule_card(index, rule, rules) for index, rule in enumerate(rules))
    rows = "".join(section_fields("section", index, section, rules) for index, section in enumerate(settings["sections"]))
    tracked_rows = "".join(section_fields("tracked", index, section, rules) for index, section in enumerate(settings["tracked_sections"]))
    notice = '<span class="notice">Saved</span>' if saved else ""
    if port_in_use:
        notice = (
            f'<span class="notice warning">Saved, but port {html.escape(port_in_use)} is in use, '
            f'so the board stays on port {settings["port"]}</span>'
        )
    content = f"""<div class="settings">
<h1>Settings</h1>
<form id="settings-form" method="post" action="/settings">
  <fieldset>
    <legend>Board</legend>
    <label><input type="checkbox" name="collapse_empty_sections"{checked_attribute(settings['collapse_empty_sections'])}> Collapse empty sections</label>
    <label><input type="checkbox" name="auto_refresh"{checked_attribute(settings['auto_refresh'])}> Refresh every
      <input type="number" name="refresh_minutes" min="1" value="{settings['refresh_minutes']}"> minutes</label>
    <p class="hint">When refresh is off, the board and notifications only update when you reload the page.</p>
  </fieldset>
  <fieldset>
    <legend>Updates</legend>
    <p>Running {version_line(updater)}</p>
    <label><input type="checkbox" name="auto_update"{checked_attribute(settings['auto_update'])}> Install updates automatically and restart the board</label>
    <p class="hint">The board looks for new commits on GitHub when it starts and then once an hour, with a <code>git fetch</code> in the background,
      never on page refreshes. A green arrow next to the version in the banner means an update is waiting; click it to install it.
      Updates only fast-forward: local changes or commits that conflict stop them, and the error shows here.</p>
    <p class="actions"><button type="button" id="check-updates"{" disabled" if updater.version is None else ""}>Check now</button>
      <span class="hint" id="update-status">{html.escape(update_status(updater))}</span></p>
  </fieldset>
  <fieldset>
    <legend>Start at login</legend>
    {login_item_fields()}
  </fieldset>
  <fieldset>
    <legend>Server</legend>
    <label>Port <input type="number" name="port" min="1024" max="65535" value="{settings['port']}" style="width: 6em"></label>
    <p class="hint">The board is served on http://127.0.0.1:<em>port</em>. When you change it, the board restarts there and this page follows;
      the old address and its bookmarks stop working, while <code>gh pr-overview-web</code> follows the change.
      Collapsed sections are remembered per address, so they start over on the new port.</p>
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
    <legend>Tracked repositories</legend>
    <p class="hint">The board gets a second row with the PRs of these repositories, in any state: open, draft, merged or closed.
      Pick from every repository the gh account can see: your own public and private ones, orgs you belong to, and ones you collaborate on.</p>
    <label>Show the <input type="number" name="tracked_per_repo" min="1" max="{MAX_PER_REPO}" value="{settings['tracked_per_repo']}">
      most recently updated PRs of each repository</label>
    <label><input type="checkbox" name="hide_my_prs_in_tracked"{checked_attribute(settings['hide_my_prs_in_tracked'])}> Hide PRs that already show in the My PRs row</label>
    <div class="repo-tools">
      <input type="search" id="repo-filter" placeholder="Filter repositories">
      <button type="button" onclick="loadRepositories(true)">Reload list</button>
      <span class="hint" id="repo-status"></span>
    </div>
    <div class="repo-picker">
      {selected_repositories(settings, overview.repository_list)}
      <div id="repo-groups"></div>
    </div>
  </fieldset>
  <fieldset>
    <legend>Tracked repository sections</legend>
    <p class="hint">The same sections apply to every tracked repository. They work like the sections above, without a query:
      each one shows the tracked PRs that match its rule. With "Show each PR only in one section" on, a PR goes to the matching
      section with the lowest priority number, which is how Merged and Closed keep finished PRs out of the others.</p>
    <input type="hidden" name="tracked_order" value="">
    <div class="tracked-sections">{tracked_rows}</div>
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
  <input type="hidden" name="next" value="">
  <div class="actions">
    <button type="submit" class="primary">Save</button>
    <button type="submit" formaction="/settings/reset" onclick="return confirm('Reset every setting to its default?')">Reset to defaults</button>
    {notice}
  </div>
</form>
<form id="login-item-form" method="post" action="/login-item/enable"></form>
<dialog id="leave-dialog">
  <h2>Unsaved changes</h2>
  <p>You changed settings without saving them. Save them before leaving?</p>
  <div class="actions">
    <button type="button" id="leave-stay">Stay</button>
    <button type="button" id="leave-discard">Leave without saving</button>
    <button type="button" id="leave-save" class="primary">Save and leave</button>
  </div>
</dialog>
</div>
<script>{settings_script(rules)}</script>"""
    return page("Settings · GitHub PR overview", banner(settings, overview.current_viewer(), True, updater), content)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Host") not in self.server.allowed_hosts:
            self.send_error(403)
            return
        url = urlsplit(self.path)
        overview = self.server.overview
        updater = self.server.updater
        if url.path == "/":
            self.respond(render_board(overview, updater))
        elif url.path == "/board":
            max_age = parse_qs(url.query).get("max_age", [""])[0]
            max_age = int(max_age) if max_age.isdigit() else FRESH_SECONDS
            self.respond(board_content(overview.fresh_board(max_age)), headers=self.update_headers())
        elif url.path == "/instance":
            self.respond(INSTANCE_ID, content_type="text/plain")
        elif url.path == "/version":
            self.respond(updater.version["commit"] if updater.version else "", content_type="text/plain")
        elif url.path == "/settings":
            query = parse_qs(url.query, keep_blank_values=True)
            self.respond(render_settings(overview, updater, "saved" in query, query.get("port_in_use", [""])[0]))
        elif url.path == "/repositories":
            try:
                self.respond(repository_groups(overview.repositories("reload" in parse_qs(url.query))))
            except subprocess.CalledProcessError as error:
                self.respond(f'<pre class="error">gh failed:\n{html.escape(error.stderr or "")}</pre>', 502)
            except (KeyError, TypeError, ValueError) as error:
                self.respond(f'<pre class="error">Unexpected response from gh: {html.escape(repr(error))}</pre>', 502)
        else:
            self.send_error(404)

    def do_POST(self):
        origin = self.headers.get("Origin")
        allowed_origins = {f"http://{host}" for host in self.server.allowed_hosts}
        if self.headers.get("Host") not in self.server.allowed_hosts or (origin is not None and origin not in allowed_origins):
            self.send_error(403)
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
        overview = self.server.overview
        path = urlsplit(self.path).path
        if path == "/settings":
            self.apply_settings(settings_from_form(form), local_path(form.get("next", [""])[0]))
        elif path == "/settings/reset":
            self.apply_settings(normalized_settings({}), "")
        elif path == "/login-item/enable":
            self.enable_login_item()
        elif path == "/login-item/disable":
            self.disable_login_item()
        elif path == "/update":
            self.install_update(local_path(form.get("next", [""])[0]))
        elif path == "/update/check":
            self.server.updater.check()
            self.respond(update_status(self.server.updater), content_type="text/plain", headers=self.update_headers())
        elif path == "/settings/sound":
            play_sound(form.get("sound", [""])[0])
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    def update_headers(self):
        updater = self.server.updater
        if updater.version is None:
            return {}
        return {
            "X-App-Commit": updater.version["commit"],
            "X-App-Updates": str(updater.pending),
            "X-App-Update-Title": update_title(updater.pending),
        }

    def enable_login_item(self):
        try:
            write_login_item()
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            self.respond(page("Start at login failed · GitHub PR overview", "", f"""<h1>Couldn't set up the login item</h1>
<pre class="error">{html.escape(str(error))}</pre>
<p><a href="/settings">Back to the settings</a></p>"""), 500)
            return
        if runs_as_login_item():
            self.redirect("/settings")
            return
        message = "The board now starts at login and is restarting as the login item."
        self.respond(render_restarting("Starting at login", message, "/settings"))
        stop_server(self.server, hand_over_to_login_item)

    def disable_login_item(self):
        LOGIN_ITEM_PATH.unlink(missing_ok=True)
        if not runs_as_login_item():
            run_launchctl("bootout", launchd_service())
            self.redirect("/settings")
            return
        message = "The board no longer starts at login and is restarting as a normal process, so it keeps running until you log out."
        self.respond(render_restarting("Leaving the login item", message, "/settings"))
        stop_server(self.server, leave_login_item)

    def install_update(self, next_path):
        updater = self.server.updater
        if updater.version is None:
            self.send_error(404)
            return
        commit = updater.install()
        if commit is None:
            self.respond(page("Update failed · GitHub PR overview", "", f"""<h1>Update failed</h1>
<pre class="error">{html.escape(updater.error or "")}</pre>
<p><a href="/">Back to the board</a></p>"""), 500)
        elif commit == updater.version["commit"]:
            self.redirect(next_path or "/")
        else:
            self.respond(render_updating(commit, next_path or local_path(urlsplit(self.headers.get("Referer", "")).path)))
            restart_server(self.server)

    def apply_settings(self, settings, next_path):
        previous = self.server.overview.settings
        if settings["auto_update"] and not previous["auto_update"]:
            self.server.updater.wake.set()
        running_port = self.server.server_port
        wanted_port = settings["port"]
        if wanted_port != running_port and not port_is_free(wanted_port):
            settings["port"] = running_port
        save_settings(settings)
        self.server.overview.change_settings(settings)
        if settings["port"] != running_port:
            self.respond(render_moving(settings["port"], next_path))
            restart_server(self.server)
        elif wanted_port != running_port:
            self.redirect(f"/settings?saved&port_in_use={wanted_port}")
        else:
            self.redirect(next_path or "/settings?saved")

    def respond(self, text, status=200, content_type="text/html", headers=None):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
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


USAGE = """Usage: github-pr-overview.py [--no-browser] [--show-port] [--set-port PORT]

  (no option)      serve the board and open it in the browser
  --no-browser     serve the board without opening it
  --show-port      print the port the board uses, then exit
  --set-port PORT  save PORT (1024-65535) as the port to use, then exit;
                   a running board keeps its port until it restarts"""


def set_port(value):
    port = int(value) if value.isdigit() else None
    if not is_port(port):
        sys.exit(f"Not a port between 1024 and 65535: {value}")
    settings = load_settings()
    settings["port"] = port
    save_settings(settings)
    print(f"Saved port {port}")


def serve(open_browser):
    settings = load_settings()
    port = settings["port"]
    for attempt in range(BIND_ATTEMPTS):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError as error:
            if error.errno != errno.EADDRINUSE or attempt == BIND_ATTEMPTS - 1:
                sys.exit(f"Can't serve on port {port}: {error.strerror}. Pick another one with --set-port PORT.")
            time.sleep(0.25)
    overview = Overview(settings, Notifier())
    server.overview = overview
    updater = Updater(overview, lambda: restart_server(server))
    server.updater = updater
    server.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    server.after_stop = None
    url = board_url(port)
    print(f"Serving {url} (Ctrl+C to stop)", flush=True)
    threading.Thread(target=overview.watch, daemon=True).start()
    threading.Thread(target=updater.watch, daemon=True).start()
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    if server.after_stop is not None:
        server.after_stop()


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if "-h" in arguments or "--help" in arguments:
        print(USAGE)
    elif "--show-port" in arguments:
        print(load_settings()["port"])
    elif "--set-port" in arguments:
        position = arguments.index("--set-port")
        if position + 1 >= len(arguments):
            sys.exit("--set-port needs a port")
        set_port(arguments[position + 1])
    else:
        serve("--no-browser" not in arguments)
