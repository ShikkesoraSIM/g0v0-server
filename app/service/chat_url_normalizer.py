"""Rewrite known-broken host references in inbound chat messages.

Background
----------
The Torii client's ``/np`` (now playing) action posts links of the form
``[<websiteUrl>/b/<id> <title>]`` where ``<websiteUrl>`` is taken from
the local ``OsuGameBase.CreateEndpoints`` config. Until the matching
client-side fix lands and propagates to all releases, ``websiteUrl``
gets the same value as ``apiUrl`` for users with a custom API URL —
so /np ends up linking to the API subdomain
(``lazer-api.shikkesora.com``) which only serves JSON. Two visible
consequences:

  * the in-client chat link parser doesn't recognise the URL as a
    beatmap link (its WebsiteRootUrl matcher is keyed off the website
    host, not the API host), so clicking opens it externally;
  * the browser then hits the API subdomain, which has no HTML for
    ``/b/<id>`` — 404.

Fixing this here as well as client-side because:

  * Older client releases that haven't auto-updated yet keep producing
    broken /np URLs. We can't depend on Velopack rolling everyone
    forward before the next chat session.
  * Web/IRC clients and any third-party integrations that paste
    ``/np`` text bypass the client patch entirely.
  * The server is the single ingest point for chat content, so a
    rewrite here is comprehensive by construction.

Scope
-----
Conservative: only swaps the EXACT API hostname for its matching
website host. Doesn't try to parse URLs or be clever with paths;
that's the client's job. Idempotent — applying twice produces the
same string. Safe to call on arbitrary message content; no-ops on
text that doesn't contain the API host substring.
"""

from __future__ import annotations

import re

# Hardcoded subdomain pair for the deployed Torii hosts. The API
# subdomain serves only JSON; the website subdomain serves the HTML
# pages that ``/b/<id>`` etc. should resolve to. New environments
# (staging, dev, alternate domains) are added here as additional
# tuples; the rewriter applies each rule independently.
#
# ``\b`` word boundary anchors the match so we don't accidentally
# rewrite hostnames that happen to contain "lazer-api" as a substring
# of a longer label (extremely unlikely in chat but free defensiveness).
_REWRITE_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\blazer-api\.shikkesora\.com\b", re.IGNORECASE),
        "lazer.shikkesora.com",
    ),
]


def normalize_chat_urls(content: str | None) -> str | None:
    """Apply known host-swap rules to a chat message body.

    Returns the rewritten string, or the input unchanged if none of the
    rules matched. Returns ``None`` for ``None`` input (so callers can
    apply this transparently to optional fields).
    """
    if not content:
        return content

    for pattern, replacement in _REWRITE_RULES:
        content = pattern.sub(replacement, content)

    return content
