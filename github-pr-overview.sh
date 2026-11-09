#!/bin/bash
set -euo pipefail

gh api graphql -f query='
{
  search(query: "is:pr is:open author:@me archived:false", type: ISSUE, first: 100) {
    nodes {
      ... on PullRequest {
        title
        url
        number
        isDraft
        mergeable
        reviewDecision
        repository { nameWithOwner }
        reviewRequests(first: 10) {
          nodes {
            requestedReviewer {
              ... on User { login }
              ... on Team { slug }
            }
          }
        }
        latestOpinionatedReviews(first: 20) { nodes { state author { login } } }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
    }
  }
}' --jq '
  def ci: (.commits.nodes[0].commit.statusCheckRollup.state // "NONE");
  def reviewers: [.reviewRequests.nodes[].requestedReviewer | (.login // .slug)];
  def change_requesters: [.latestOpinionatedReviews.nodes[] | select(.state == "CHANGES_REQUESTED") | .author.login // empty];
  def problems: [
    (if (change_requesters - reviewers | length) > 0 then "changes requested" else empty end),
    (if (ci | IN("FAILURE", "ERROR")) then "CI failing" else empty end),
    (if .mergeable == "CONFLICTING" then "conflicts" else empty end)
  ];
  def state:
    if .isDraft then 0
    elif (problems | length) > 0 then 3
    elif .reviewDecision == "APPROVED" then 4
    elif (reviewers | length) > 0 then 2
    else 1 end;
  def note:
    if state == 3 then (problems | join(", "))
    elif state == 2 then (reviewers | join(", "))
    elif state == 4 and ci == "PENDING" then "CI running"
    else "" end;
  def titles: ["Draft", "Ready for review (no reviewer)", "Waiting for reviewer", "Needs action", "Ready for merge"];

  .data.search.nodes as $prs
  | range(0; 5) as $s
  | [$prs[] | select(state == $s)] as $group
  | "\n\u001b[1m\(titles[$s]) (\($group | length))\u001b[0m",
    (if ($group | length) == 0 then "  -"
     else ($group[] | "  \(.repository.nameWithOwner)#\(.number)  \(.title)"
       + (if note != "" then "  \u001b[33m[\(note)]\u001b[0m" else "" end)
       + "\n    \u001b[2m\(.url)\u001b[0m")
     end)
'
