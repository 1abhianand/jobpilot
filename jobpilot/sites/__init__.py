"""Job-site adapters, by name."""

from __future__ import annotations

from ..config import Settings
from ..forms import FormFiller
from .base import ApplyResult, JobPage, JobStub, Outcome, Site
from .generic import SPECS, GenericSite
from .linkedin import LinkedIn

__all__ = ["ApplyResult", "JobPage", "JobStub", "Outcome", "Site", "build_sites", "login_urls"]


def build_sites(settings: Settings, filler: FormFiller) -> dict[str, Site]:
    sites: dict[str, Site] = {"linkedin": LinkedIn(filler)}
    for name, spec in SPECS.items():
        sites[name] = GenericSite(spec, filler, settings.site(name).selectors)
    return sites


def login_urls() -> dict[str, str]:
    return {"linkedin": LinkedIn.login_url, **{name: spec.login_url for name, spec in SPECS.items()}}
