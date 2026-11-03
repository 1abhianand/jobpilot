"""Find a work email for a person: Hunter.io when you have a key, else the first-name guess.

Early-stage founders very often use first@company.com, so the guess is useful, but it is
always labelled as a guess on the approval card.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from urllib.parse import urlparse

import dns.asyncresolver
import dns.exception
import httpx

log = logging.getLogger(__name__)

HUNTER_FINDER = "https://api.hunter.io/v2/email-finder"
MIN_HUNTER_SCORE = 50
NOT_COMPANY_HOSTS = ("linkedin.com", "github.com", "medium.com", "notion.site", "google.com", "angel.co",
                     "wellfound.com", "ycombinator.com", "twitter.com", "x.com", "facebook.com", "instagram.com")


def domain_of(website: str | None) -> str | None:
    if not website:
        return None
    url = website if "//" in website else f"https://{website}"
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if "." not in host or any(host == h or host.endswith("." + h) for h in NOT_COMPANY_HOSTS):
        return None
    return host


def _ascii(part: str) -> str:
    plain = unicodedata.normalize("NFKD", part).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", plain.lower())


def split_name(full_name: str) -> tuple[str, str]:
    """('Anand Kumar Prajapati') -> ('anand', 'prajapati'); initials and titles are dropped."""
    parts = [p for p in re.split(r"[\s,]+", full_name.strip())
             if len(_ascii(p)) > 1 and _ascii(p) not in ("dr", "mr", "ms", "mrs", "jr", "sr", "phd")]
    if not parts:
        return "", ""
    return _ascii(parts[0]), (_ascii(parts[-1]) if len(parts) > 1 else "")


async def has_mx(domain: str) -> bool:
    try:
        answer = await dns.asyncresolver.resolve(domain, "MX", lifetime=8)
        return len(answer) > 0
    except dns.exception.DNSException:
        return False


class EmailFinder:
    def __init__(self, http: httpx.AsyncClient, hunter_key: str = ""):
        self.http = http
        self.hunter_key = hunter_key

    async def find(self, full_name: str, domain: str | None) -> tuple[str | None, str]:
        """Returns (email or None, how it was found)."""
        first, last = split_name(full_name)
        if not first or not domain:
            return None, "no name or domain"
        if self.hunter_key:
            found = await self._hunter(first, last, domain, full_name)
            if found:
                return found
        if not await has_mx(domain):
            return None, "domain takes no email"
        return f"{first}@{domain}", "guess"

    async def _hunter(self, first: str, last: str, domain: str, full_name: str) -> tuple[str, str] | None:
        params = {"domain": domain, "api_key": self.hunter_key}
        if last:
            params |= {"first_name": first, "last_name": last}
        else:
            # Hunter wants both names or the whole one, and split_name drops initials: "Abhishek E" arrives
            # with no surname, which the two-field form answers with HTTP 400.
            params["full_name"] = full_name.strip()
        try:
            response = await self.http.get(HUNTER_FINDER, params=params, timeout=20)
        except httpx.HTTPError as exc:
            log.warning("Hunter lookup failed for %s: %s", domain, exc)
            return None
        if response.status_code != 200:
            why = {400: "not enough of a name to search on", 401: "bad key",
                   403: "plan limit reached", 429: "quota used up"}.get(response.status_code, "unexpected reply")
            log.warning("Hunter returned HTTP %s for %s at %s (%s)", response.status_code, full_name, domain, why)
            return None
        data = response.json().get("data") or {}
        score = data.get("score") or 0
        if data.get("email") and score >= MIN_HUNTER_SCORE:
            return data["email"].lower(), f"hunter:{score}"
        return None
