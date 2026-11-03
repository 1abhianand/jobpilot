"""Prompt text. The system prompt is identical on every call so it stays in the prompt cache."""

from __future__ import annotations

import json

from .config import Profile

BRIEFS = {
    "li_connect": (
        "a LinkedIn connection note. Hard limit: {limit} characters including spaces. Start with \"Hi\", their first name and a comma. "
        "Then one simple, honest line on why {first} is reaching out (something about their "
        "company that genuinely interests them), one plain line about what {first} does, and a short, friendly "
        "close in your own words It should read like a quick note a person typed on their phone: "
        "everyday words, no clever analysis of their business, no tech jargon or list of tools. "
        "No sign-off, no line breaks."
    ),
    "email": (
        "a cold email. Body 70-120 words. Start with \"Hi\", their first name and a comma, on its own line. "
        "Then, like a person writing to someone they'd like to meet: one or two sentences on why their company "
        "caught {first}'s interest, in plain words; a short, simple line on what {first} works on now and "
        "one concrete result from the resume that's relevant to them (one number at most, no list of tools); "
        "and an easy, low-pressure ask: a quick chat about engineering roles, or a pointer to the right "
        "person. Two or three short paragraphs. "
        'End the body with "Thanks," on one line and "{first}" on the next; links and a signature are added '
        "separately. subject: 3-7 plain words, specific to them, like a person would write it."
    ),
    "li_message": (
        "a LinkedIn message to someone who just accepted {first}'s connection request. 200-450 characters. "
        "Start with \"Hi\", their first name and a comma. Thank them for connecting, say in one plain line what "
        "{first} works on and why that made them want to reach out, then ask casually whether their team is "
        "hiring engineers or if they'd be up for a short chat. No sign-off."
    ),
    "followup": (
        "a follow-up to the earlier email below, which got no reply. Body 30-70 words. Add one new, relevant "
        "point instead of repeating the first email, keep the ask easy to answer, and stay warm. "
        'No guilt-tripping ("just bumping this", "circling back"). End with just "{first}".'
    ),
    "apply_note": (
        "the note that goes with {first}'s application to this job. 70-130 words. Why this role at this "
        "company, the two most relevant proofs from the resume, and say plainly that {first} is {start}. "
        "Address the team rather than a named person unless a name is given."
    ),
}


def _first(profile: Profile) -> str:
    return profile.first_name or "the candidate"


def _roles(profile: Profile) -> str:
    roles = ", ".join(profile.target_roles) or "software engineering roles"
    if profile.role_titles:
        roles += f"; the titles {', '.join(profile.role_titles)} all count as this same role"
    return roles


def _places(profile: Profile) -> str:
    return ", ".join(profile.preferred_locations) or "their current city or remote"


def system(profile: Profile, context: str) -> str:
    first = _first(profile)
    return f"""You work for {profile.name} on their job search. You judge how well jobs and companies fit them, answer application-form questions for them, and write outreach in their voice.

Everything you know about {first} is in the profile and resume below. Treat them as the only source of truth: never invent or inflate employers, titles, numbers, skills, projects or links. When something isn't there, say less rather than guess.

When you write as {first}:
- Sound like a friendly, genuine person, not a pitch deck: the way {first} would actually write to someone they'd like to meet. Use everyday words, contractions (I'm, I've, you're) and short, natural sentences.
- Talk about the recipient's company the way a curious person would, mentioning the one specific thing that interests {first}, not with a sharp analysis of their business. Vary how you open: don't fall back on a stock line like "I've been reading about" or "I came across".
- Keep {first}'s own background simple and modest: what they work on in plain terms, one concrete result at most. Say "I work on" or "I've been building", never "I own" or "end to end". No stacks of tools, techniques or numbers.
- Don't copy phrases from the profile or resume word for word; say it the way you'd say it out loud.
- No flattery, no filler openers ("I hope this finds you well", "I came across your profile"), no buzzwords ("passionate", "synergy", "rockstar", "leverage"), no emojis, no em dashes (use a comma or a full stop). At most one exclamation mark.
- Don't bring up salary, pay, CTC or compensation expectations.
- Never say or imply that the message was written with AI or sent by a tool.
- Write plain text only: no HTML tags, no Markdown. Put any link as the bare URL (https://...), never as <a href> or [text](url).

{context}"""


def score_job(profile: Profile, *, site: str, url: str, title: str | None, company: str | None,
              location: str | None, description: str) -> str:
    first = _first(profile)
    years = f"{profile.years_experience:g} years" if profile.years_experience else "their"
    header = "\n".join(f"{label}: {value}" for label, value in
                       (("Title", title), ("Company", company), ("Location", location)) if value)
    return f"""Rate how well this job fits {first}, from 0 to 100.

Weigh, in this order:
1. Role and seniority: is it one of the roles they want ({_roles(profile)}) at a level that suits {years} of experience?
2. Stack and domain: how much of what the job needs shows up in their resume?
3. Location: can they take it from where they are? They prefer {_places(profile)}. A role that needs a foreign work permit or on-site presence abroad is a poor fit unless the posting says it sponsors visas or hires remotely from their country.
4. Company: a well-known product company (FAANG and other top tech firms) or a funded, growing startup beats a staffing agency, a recruiter posting for an unnamed client, or an IT-services/outsourcing firm. Use the profile's pitch for what {first} wants.

Scale: 85-100 apply today, 70-84 good fit, 50-69 stretch, below 50 poor fit.
reason: one short line naming the strongest match or the deal-breaker.
title, company, location: as the posting states them, or null when it doesn't say.
salary_max_lpa: the top of the pay range the posting states for this role, as Indian rupees in lakhs per year (₹18L, 18 LPA and ₹18,00,000 a year are all 18; convert monthly pay to yearly and other currencies at today's rate). Fixed pay only, not equity. null when the posting states no pay.

<job source="{site}" url="{url}">
{header}

{description}
</job>"""


def score_company(profile: Profile, company: str) -> str:
    first = _first(profile)
    return f"""{first} wants roles like: {_roles(profile)}. Rate from 0 to 100 how worthwhile it is for {first} to reach out to this startup about an engineering role now.

Consider:
- whether the product and stack plausibly need someone with {first}'s experience,
- whether it hires where {first} can work ({_places(profile)}),
- stage and size: an early startup that is hiring engineers beats a large company or a mostly non-technical team.

reason: one short line.

<company>
{company}
</company>"""


def write(profile: Profile, channel: str, *, recipient: str | None = None, company: str | None = None,
          job: str | None = None, previous: str | None = None, draft: str | None = None,
          instruction: str | None = None, limit: int | None = None) -> str:
    first = _first(profile)
    brief = BRIEFS[channel].format(first=first, limit=limit or 200, start=profile.start_text())
    parts = [f"Write {brief}"]
    if channel != "email":
        parts.append("subject: null.")
    if recipient:
        parts.append(f"Recipient: {recipient}")
    if company:
        parts.append(f"<company>\n{company}\n</company>")
    if job:
        parts.append(f"<job>\n{job}\n</job>")
    if previous:
        parts.append(f"<earlier_email>\n{previous}\n</earlier_email>")
    if draft:
        parts.append(f"<current_draft>\n{draft}\n</current_draft>")
    if instruction:
        parts.append(f"Revise the current draft. {first}'s instruction: {instruction}")
    return "\n\n".join(parts)


def answer(profile: Profile, *, question: str, kind: str, options: list[str], job: str | None) -> str:
    first = _first(profile)
    where = f" for {job}" if job else ""
    choices = (f"\nOptions (answer with exactly one of these, copied verbatim): {json.dumps(options)}"
               if options else "")
    return f"""An application form{where} asks {first}:
"{question}"
Field type: {kind}.{choices}

Answer as {first}, using only facts from the profile and resume. Keep it as short as the field needs: digits only for numbers (years as a whole number, rounded down), one option for a choice, a sentence or two for free text.
confident: false when the profile and resume don't contain what you need to answer truthfully, such as a skill that isn't mentioned or a legal or personal fact that isn't stated."""


def scout(profile: Profile, *, brief: str, n: int, exclude: list[str]) -> str:
    first = _first(profile)
    wants = brief or profile.pitch or _roles(profile)
    skip = ", ".join(exclude) if exclude else "none yet"
    return f"""Use web search to find {n} startups that {first} should contact about an engineering role now.

What {first} is looking for: {wants}
Target roles: {_roles(profile)}. Preferred locations: {_places(profile)}.

Prefer companies that:
- raised a round in the last 12 months or are clearly growing,
- are hiring engineers (a careers page, job posts, or founders posting about hiring),
- build products where {first}'s experience is directly useful.

Already on the list, so skip them: {skip}

For each company give: name; website (homepage URL); linkedin_url of the company's LinkedIn page if a search result showed it, else null; location; stage (latest round and when, e.g. "Series A, Mar 2026"); why: one specific sentence on why it fits {first}; people: up to 2 people to contact (founders, CTO or engineering leaders) with name, title and linkedin_url. Only give a LinkedIn URL that appeared verbatim in a search result, never one built from a name; use null otherwise. Only include companies you actually found in search results."""


def pick_people(profile: Profile, *, company: str, candidates: list[str], k: int) -> str:
    first = _first(profile)
    listing = "\n".join(f"{i}. {text}" for i, text in enumerate(candidates, 1))
    return f"""{first} wants to reach out to {company} about engineering roles. From these LinkedIn search results, pick up to {k} people most worth contacting, preferring in this order: founders, CTO, VP/Head/Director of Engineering, engineering managers, technical recruiters. Skip anyone who doesn't seem to work there now.
numbers: their numbers from the list, best first; empty when nobody fits.

{listing}"""
