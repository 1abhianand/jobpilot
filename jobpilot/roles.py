"""Your role titles (SDE 1, Software Engineer, FDE, ...) as each site's own search.

Only sites whose search takes a job title get one: LinkedIn (one Boolean OR search, so a single page load
covers every title), Instahyre (its search box takes skills or titles, OR-ed) and Hirist (one search page per
title, on top of your logged-in feed). Wellfound searches by its own role pages, so those stay in config.yaml.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

from .config import Profile

# LinkedIn's entry/associate filter still lets these through; they ask for 4-10 years
SENIOR = ("Senior", "Lead", "Staff", "Principal", "Manager", "Architect")


def titles(profile: Profile) -> list[str]:
    return list(dict.fromkeys(t.strip() for t in profile.role_titles if t.strip()))


def role_searches(site: str, profile: Profile) -> list[str]:
    wanted = titles(profile)
    if not wanted:
        return []
    if site == "linkedin":
        titles_or = " OR ".join(f'"{t}"' for t in wanted)
        keywords = f"({titles_or}) " + " ".join(f"NOT {w}" for w in SENIOR)
        query = {"keywords": keywords, "location": "India", "f_AL": "true",
                 "f_E": "2,3", "f_TPR": "r604800", "sortBy": "DD"}  # Easy Apply, entry/associate, past week
        return ["https://www.linkedin.com/jobs/search/?" + urlencode(query)]
    if site == "instahyre":
        query = {"search": "true", "years": str(int(profile.years_experience)), "skills": ",".join(wanted)}
        return ["https://www.instahyre.com/candidate/opportunities/?" + urlencode(query, safe=",")]
    if site == "hirist":  # the address Hirist's own search box goes to: /search/forward-deployed-engineer
        slugs = dict.fromkeys(re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-") for t in wanted)
        return [f"https://www.hirist.tech/search/{slug}" for slug in slugs if slug]
    return []
