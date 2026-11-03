from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from jobpilot import pipeline, prompts
from jobpilot.roles import role_searches
from jobpilot.sites.base import JobStub

from .conftest import make_settings

TITLES = ["Software Engineer", "SDE 1", "SDE I", "Forward Deployed Engineer", "FDE", "SDE 1"]


def profile(titles=TITLES):
    return make_settings(profile={"role_titles": titles, "years_experience": 1.3}).profile


def test_linkedin_searches_every_title_in_one_or_query():
    [url] = role_searches("linkedin", profile())
    query = parse_qs(urlsplit(url).query)
    assert query["keywords"] == ['("Software Engineer" OR "SDE 1" OR "SDE I" OR "Forward Deployed Engineer" OR "FDE") '
                                 'NOT Senior NOT Lead NOT Staff NOT Principal NOT Manager NOT Architect']
    assert query["f_AL"] == ["true"] and query["location"] == ["India"]


def test_instahyre_searches_the_titles_as_or_ed_skills():
    [url] = role_searches("instahyre", profile())
    query = parse_qs(urlsplit(url).query)
    assert query["search"] == ["true"] and query["years"] == ["1"]
    assert query["skills"] == ["Software Engineer,SDE 1,SDE I,Forward Deployed Engineer,FDE"]


def test_hirist_gets_one_search_page_per_title():
    assert role_searches("hirist", profile()) == [
        "https://www.hirist.tech/search/software-engineer", "https://www.hirist.tech/search/sde-1",
        "https://www.hirist.tech/search/sde-i", "https://www.hirist.tech/search/forward-deployed-engineer",
        "https://www.hirist.tech/search/fde"]


def test_sites_without_title_search_and_empty_titles_add_nothing():
    assert role_searches("wellfound", profile()) == role_searches("waas", profile()) == []
    assert role_searches("linkedin", profile([])) == []


def test_the_scorer_treats_the_titles_as_one_role():
    text = prompts.score_job(profile(), site="X", url="u", title="SDE I", company=None, location=None, description="")
    assert "Forward Deployed Engineer" in text and "same role" in text


async def test_role_searches_run_alongside_the_sites_own(app):
    app.settings.profile.role_titles = ["SDE 1", "FDE"]
    site = app.sites["instahyre"]
    seen: list[list[str]] = []

    async def discover(page, urls, limit, known=()):
        seen.append(urls)
        return [JobStub("1", "https://instahyre.example/job-1")]

    site.discover = discover
    await pipeline.run_site(app, "instahyre")
    assert any("skills=SDE+1,FDE" in u for u in seen[0]) and len(seen[0]) == len(set(seen[0])) >= 2
