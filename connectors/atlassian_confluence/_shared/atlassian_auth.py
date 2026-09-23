"""Atlassian permission groups and the scopes each one needs.

Shared by the atlassian-jira and atlassian-confluence servers so the two cannot
drift on the group names a connection selects or the scopes those imply.

Deliberately contains no OAuth logic. The platform runs the consent flow, holds
the refresh token, and hands each tool call a live access token; a server that
also minted tokens would be reimplementing caching, locking and refresh-token
rotation that already exist one layer up.

Why scopes are grouped rather than listed flat: asking for every scope the
server could ever need makes consent all-or-nothing, so a user who only wants to
read Confluence pages is shown "delete Jira issues", and widening the list later
forces every existing connection to re-consent. Scopes are therefore declared
per permission group, the same unit Atlassian's own Rovo server grants at and
the unit a connection selects. A read-only Confluence connection asks for three
scopes instead of sixty.

Two things outside this file have to agree with it:

1. The OAuth app in the Atlassian developer console must declare every scope in
   ALL_SCOPES. Authorization is rejected for anything it does not.
2. A connection requests only the scopes its selected groups need. The platform
   computes that from the required_scopes each tool publishes in its
   registration manifest; scopes_for() is the same calculation, kept here so it
   can be reasoned about and tested.
"""

from typing import Dict, Iterable, List


# Requested by every connection. offline_access is what makes a refresh token be
# issued at all, and read:me backs the two common tools every connection gets.
BASE_SCOPES: List[str] = ["read:me", "offline_access"]

# Scopes per group. Tools import these constants for their own
# @tool(required_scopes=...), so what a tool declares and what its group
# requests cannot fall out of step.
#
# Confluence is granular per resource, which is the point: reading a page and
# reading a comment are separate grants, so a page-reading connection is never
# handed comment access it did not ask for. Jira's classic scopes are coarser,
# one scope covering every read. That is Atlassian's model, not a shortcut here.
JIRA_READ = ["read:jira-work"]
JIRA_WRITE = ["write:jira-work"]
JIRA_SEARCH = ["search:jira-work"]
# Agile boards, sprints and epics live under the jira-software product scope,
# not jira-work: a connection that can read and write plain issues cannot see
# a board or move an issue between sprints without these too. NEW - the
# Atlassian dev console app must add these, and existing connections need to
# reconnect before an agile tool will work for them.
#
# Split per tool, verified against Atlassian's own OpenAPI spec
# (developer.atlassian.com/cloud/jira/software/swagger.v3.json): each agile
# endpoint's `security` block is an AND of every scope listed, and they are
# not the same set per endpoint - GET board additionally requires
# read:project:jira, and GET epic issues additionally requires
# read:issue-details:jira and read:jql:jira. A tool that only requested
# read:*:jira-software would 401 with "scope does not match" despite the
# jira-software scope being present and correct.
JIRA_SOFTWARE_READ_BOARD = ["read:board-scope:jira-software", "read:project:jira"]
JIRA_SOFTWARE_READ_SPRINT = ["read:sprint:jira-software"]
JIRA_SOFTWARE_READ_EPIC = [
    "read:epic:jira-software",
    "read:issue-details:jira",
    "read:jql:jira",
]
JIRA_SOFTWARE_READ = JIRA_SOFTWARE_READ_BOARD + JIRA_SOFTWARE_READ_SPRINT + JIRA_SOFTWARE_READ_EPIC
JIRA_SOFTWARE_WRITE = ["write:sprint:jira-software"]
# Project-level admin objects (versions, components) sit behind
# manage:jira-project, not write:jira-work: creating a release is a project
# configuration change, not an edit to an issue. NEW, same reconnect caveat.
JIRA_MANAGE_PROJECT = ["manage:jira-project"]

CONFLUENCE_READ_PAGE = ["read:page:confluence"]
CONFLUENCE_READ_HIERARCHY = ["read:hierarchical-content:confluence"]
CONFLUENCE_READ_COMMENT = ["read:comment:confluence"]
CONFLUENCE_READ_SPACE = ["read:space:confluence"]
# Folder metadata is its own grant. Listing what is INSIDE a folder rides on
# read:hierarchical-content, which read_confluence already requests.
CONFLUENCE_READ_FOLDER = ["read:folder:confluence"]
CONFLUENCE_WRITE_PAGE = ["write:page:confluence"]
CONFLUENCE_SEARCH = ["search:confluence"]
# Attachments are their own grant too, same reasoning as folders: a
# page-reading connection should not be handed attachment access it never
# asked for. NEW as of the attachment tools below - the Atlassian dev console
# app must add these two scopes, and existing connections need to reconnect
# before an attachment tool will work for them; until then Atlassian answers
# 401/403, which the tool surfaces with a "reconnect" hint (see _client.py).
CONFLUENCE_READ_ATTACHMENT = ["read:attachment:confluence"]
# Deleting a page is bundled with write_confluence rather than its own group,
# matching Jira's own precedent (deleteJiraIssueAttachment rides write_jira):
# a connection already trusted to create and edit pages is the natural unit
# to also trust with removing one, and delete:page:confluence is its own
# scope so that trust is still visible in the consent screen and in
# required_scopes, not folded silently into write:page:confluence.
CONFLUENCE_DELETE_PAGE = ["delete:page:confluence"]

# Everything below this line was corrected against Atlassian's own OpenAPI
# specs (developer.atlassian.com/cloud/confluence/{swagger.v3,openapi-v2.v3}.json)
# after a live 401 "scope does not match" on a scope that WAS granted. Cause:
# each operation's spec lists a scope per lifecycle `state` - "Current" is
# what Atlassian's authorization server actually checks today; "Beta" scopes
# are declared for a future migration and do nothing yet. The names below had
# been picked for how well they described the resource (a template tool
# asking for read:template:confluence reads as obviously right), which is
# exactly how you end up requesting a Beta scope Atlassian never granted
# anything against. Comments cite the endpoint each constant backs and give
# its verified "Current" scope, not its most descriptive-sounding one.

# GET /wiki/api/v2/pages/{id}/labels -> read:page:confluence (Current).
# read:label:confluence exists but is Beta-only; a label-reading connection
# rides on the same scope a page-reading one already has.
CONFLUENCE_READ_LABEL = ["read:page:confluence"]
# POST and DELETE /wiki/rest/api/content/{id}/label -> write:confluence-content
# (Current, classic). Both v1 label endpoints share this one; the granular
# read/write:label:confluence pair is Beta-only for this endpoint.
CONFLUENCE_WRITE_LABEL = ["write:confluence-content"]
# GET /wiki/api/v2/blogposts(/{id}) -> read:page:confluence (Current).
# Blogposts are a content type under the same v2 pages surface for scope
# purposes; read:blogpost:confluence is Beta-only.
CONFLUENCE_READ_BLOGPOST = ["read:page:confluence"]
# POST /wiki/api/v2/blogposts -> write:page:confluence (Current), same
# reasoning; write:blogpost:confluence is Beta-only.
CONFLUENCE_WRITE_BLOGPOST = ["write:page:confluence"]
# GET /wiki/rest/api/template/page -> read:confluence-content.summary
# (Current, classic). read:template:confluence is Beta-only.
CONFLUENCE_READ_TEMPLATE = ["read:confluence-content.summary"]
# POST /wiki/api/v2/footer-comments and /wiki/api/v2/inline-comments ->
# write:comment:confluence (Current). Comment creation is its own scope, not
# a page-write; the original code asked for write:page:confluence, which is
# wrong for this endpoint specifically, not just Beta vs Current.
CONFLUENCE_WRITE_COMMENT = ["write:comment:confluence"]
# POST /wiki/rest/api/content/{id}/child/attachment (v1, the only endpoint
# that can create an attachment) -> write:confluence-file (Current, classic).
# write:attachment:confluence exists but is Beta-only here.
CONFLUENCE_UPLOAD_ATTACHMENT = ["write:confluence-file"]
# DELETE /wiki/api/v2/attachments/{id} -> delete:attachment:confluence
# (Current). A distinct scope from upload, not the write:attachment:confluence
# this used to share with it - deleting and creating are different grants.
CONFLUENCE_DELETE_ATTACHMENT = ["delete:attachment:confluence"]

GROUP_SCOPES: Dict[str, List[str]] = {
    "common": [],  # covered by BASE_SCOPES
    "read_jira": JIRA_READ,
    "write_jira": JIRA_WRITE,
    "search_jira": JIRA_SEARCH,
    "read_jira_software": JIRA_SOFTWARE_READ,
    "write_jira_software": JIRA_SOFTWARE_WRITE,
    "manage_jira_project": JIRA_MANAGE_PROJECT,
    "read_confluence": (
        CONFLUENCE_READ_PAGE
        + CONFLUENCE_READ_HIERARCHY
        + CONFLUENCE_READ_COMMENT
        + CONFLUENCE_READ_SPACE
        + CONFLUENCE_READ_FOLDER
        + CONFLUENCE_READ_ATTACHMENT
    ),
    "write_confluence": CONFLUENCE_WRITE_PAGE
    + CONFLUENCE_DELETE_PAGE
    + CONFLUENCE_UPLOAD_ATTACHMENT
    + CONFLUENCE_DELETE_ATTACHMENT
    + CONFLUENCE_WRITE_COMMENT,
    "search_confluence": CONFLUENCE_SEARCH,
    "read_confluence_label": CONFLUENCE_READ_LABEL,
    "write_confluence_label": CONFLUENCE_WRITE_LABEL,
    "read_confluence_blogpost": CONFLUENCE_READ_BLOGPOST,
    "write_confluence_blogpost": CONFLUENCE_WRITE_BLOGPOST,
    "read_confluence_template": CONFLUENCE_READ_TEMPLATE,
}

JIRA_GROUPS = [
    "common",
    "read_jira",
    "write_jira",
    "search_jira",
    "read_jira_software",
    "write_jira_software",
    "manage_jira_project",
]
CONFLUENCE_GROUPS = [
    "common",
    "read_confluence",
    "write_confluence",
    "search_confluence",
    "read_confluence_label",
    "write_confluence_label",
    "read_confluence_blogpost",
    "write_confluence_blogpost",
    "read_confluence_template",
]


def scopes_for(groups: Iterable[str]) -> List[str]:
    """The minimal scope set for a selection of permission groups.

    Unknown group names are ignored rather than raising: a connection may carry
    a selection made against a newer build of the server, and failing the whole
    authorization because one name is unrecognised is worse than granting the
    subset that is understood.

    Sorted, because the result lands in a consent URL and in test assertions and
    neither wants set ordering.
    """
    scopes = set(BASE_SCOPES)
    for group in groups:
        scopes.update(GROUP_SCOPES.get(group, []))
    return sorted(scopes)


# Every scope either server can request: what the OAuth app must declare. An
# individual connection asks for a subset.
ALL_SCOPES: List[str] = scopes_for(GROUP_SCOPES)


# `build_auth_config` lived here and has been removed.
#
# It produced the platform-facing connection form, which v1 sent as
# `auth_config=` at registration. The v2 ConnectorServer has no such
# parameter, so nothing can send it and nothing called this: it was dead code
# whose only remaining effect was to import the retired v1 framework
# (`base.models`), which stopped this module - and therefore the whole
# connector - from importing at all.
#
# Nothing is lost. The platform holds each template's auth_config and keeps it
# locked. Changing it is a deliberate step on the platform side, not something
# a connector asserts.
#
# `scopes_for` above stays: the scope sets are a real part of what this
# connector needs, and the tools reference them.

