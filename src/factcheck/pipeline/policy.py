"""URL policy: which URLs the agent may search, fetch or download.

Local context: one small, pure class that answers "is this URL allowed, and if not, why?".
Global context: the same policy object is enforced in three places so they can never disagree —
(1) the WebSearch PreToolUse hook (as `blocked_domains`), (2) the WebFetch PreToolUse hook and
(3) our own `fetch_file` download tool. The app runs with an empty policy (nothing blocked);
only benchmark runs add the leakage blocklists described in reports/preregistration.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

# Archive services that can serve copies of any page; used to stop "fetch the archived copy of a
# blocked page" from bypassing a domain block (eval only).
ARCHIVE_HOSTS = ("web.archive.org", "archive.org", "archive.ph", "archive.today", "archive.is", "archive.li")


def host_matches(host: str, domain: str) -> bool:
    """True if `host` is `domain` or a subdomain of it (case-insensitive, ignores a leading 'www.')."""
    host = host.lower().rstrip(".")                    # normalise case and a trailing root dot
    domain = domain.lower().lstrip(".").rstrip(".")    # allow ".example.com" style entries too
    return host == domain or host.endswith("." + domain)  # exact match or a true subdomain


@dataclass(frozen=True)
class UrlPolicy:
    """Immutable allow/deny rules for URLs.

    blocked_domains: hosts (and their subdomains) that are refused.
    blocked_path_terms: case-insensitive substrings that refuse a URL wherever they appear in its
        host+path+query (e.g. "averitec" catches copies of the benchmark on any site).
    """

    blocked_domains: frozenset[str] = field(default_factory=frozenset)
    blocked_path_terms: tuple[str, ...] = ()

    def check(self, url: str) -> str | None:
        """Return None if `url` is allowed, otherwise a short human-readable reason."""
        parts = urlsplit(url.strip())                  # split into scheme, host, path, query
        if parts.scheme not in ("http", "https"):      # file://, ftp://, data: etc. are never allowed
            return f"scheme '{parts.scheme}' not allowed"
        host = (parts.hostname or "").lower()          # hostname without port or credentials
        if not host:                                   # "http:///x" or garbage
            return "URL has no host"
        for domain in self.blocked_domains:            # direct domain block
            if host_matches(host, domain):
                return f"domain '{domain}' is blocked"
        haystack = unquote(f"{host}{parts.path}?{parts.query}").lower()  # decoded text to scan
        for term in self.blocked_path_terms:           # answer-key guard: term anywhere in the URL
            if term.lower() in haystack:
                return f"URL contains blocked term '{term}'"
        if any(host_matches(host, a) for a in ARCHIVE_HOSTS):  # archive copy of a blocked site?
            for domain in self.blocked_domains:
                if domain.lower() in haystack:
                    return f"archived copy of blocked domain '{domain}'"
        return None                                    # nothing matched: allowed

    def search_blocked_domains(self) -> list[str]:
        """Domains to pass to the WebSearch tool's `blocked_domains` input (sorted for stable logs)."""
        return sorted(self.blocked_domains)


# The app's default: block nothing (users should be able to use any evidence, including arXiv).
OPEN_POLICY = UrlPolicy()
