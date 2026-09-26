"""Claude calls: job and company scoring, drafting, form answers and startup research.

Every call returns a validated pydantic object through structured outputs, opts into the server-side
refusal fallback where the model supports it, and reuses one cached system prompt (instructions + your
profile + resume).
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

from anthropic import AsyncAnthropic
from pydantic import BaseModel

from . import prompts
from .config import Effort, LLMSettings, Profile

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5")  # models whose refusals can be retried server-side
NO_EFFORT_MODELS = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-1")
WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": 12}
MAX_RESUMES = 5  # how many times a paused web-research turn is resumed before giving up

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class JobScore(BaseModel):
    score: int
    reason: str
    title: str | None
    company: str | None
    location: str | None


class Fit(BaseModel):
    score: int
    reason: str


class Draft(BaseModel):
    subject: str | None
    body: str


class FieldAnswer(BaseModel):
    answer: str
    confident: bool


class ScoutPerson(BaseModel):
    name: str
    title: str
    linkedin_url: str | None


class ScoutCompany(BaseModel):
    name: str
    website: str
    linkedin_url: str | None
    location: str
    stage: str
    why: str
    people: list[ScoutPerson]


class ScoutResult(BaseModel):
    companies: list[ScoutCompany]


class Picks(BaseModel):
    numbers: list[int]


def clip(text: str, limit: int) -> str:
    """Trim to `limit` characters, preferring a sentence end, then a word boundary."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = [cut.rfind(mark) for mark in (". ", "? ", "! ")]
    end = max(ends)
    if end >= limit * 0.6:
        return cut[: end + 1]
    if cut.endswith((".", "?", "!")):
        return cut
    space = cut.rfind(" ")
    return cut[:space].rstrip(",;:- ") if space > 0 else cut


def _as_param(block: Any) -> dict[str, Any]:
    data = block.to_dict()
    data.pop("parsed_output", None)
    return data


class LLM:
    def __init__(self, settings: LLMSettings, profile: Profile, context: str,
                 client: AsyncAnthropic | None = None):
        self.settings = settings
        self.profile = profile
        self.client = client or AsyncAnthropic(max_retries=4)
        self.system = [{"type": "text", "text": prompts.system(profile, context),
                        "cache_control": {"type": "ephemeral"}}]

    @property
    def scoring_model(self) -> str:
        return self.settings.scoring_model or self.settings.model

    async def _ask(self, prompt: str, schema: type[T], effort: Effort,
                   tools: list[dict[str, Any]] | None = None, model: str | None = None) -> T:
        model = model or self.settings.model
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        extra: dict[str, Any] = {"tools": tools} if tools else {}
        if not model.startswith(NO_EFFORT_MODELS):
            extra["output_config"] = {"effort": effort}
        if model == FALLBACK_MODELS[0] or model.startswith(FALLBACK_MODELS[1]):
            extra.update(betas=[FALLBACK_BETA], fallbacks="default")
        for _ in range(MAX_RESUMES + 1):
            response = await self.client.beta.messages.parse(
                model=model,
                max_tokens=16000,
                system=self.system,
                messages=messages,
                output_format=schema,
                **extra,
            )
            if response.stop_reason != "pause_turn":
                break
            # A long server-side research turn paused; sending it back resumes it where it stopped.
            messages = [messages[0], {"role": "assistant", "content": [_as_param(b) for b in response.content]}]
        else:
            raise LLMError("the research turn kept pausing; try again later")
        if response.stop_reason == "refusal":
            raise LLMError("the model declined this request")
        if response.stop_reason == "max_tokens":
            raise LLMError("the answer was cut off at max_tokens")
        if response.parsed_output is None:
            raise LLMError(f"no structured answer (stop_reason={response.stop_reason})")
        return response.parsed_output

    async def score_job(self, *, site: str, url: str, title: str | None, company: str | None,
                        location: str | None, description: str) -> JobScore:
        prompt = prompts.score_job(self.profile, site=site, url=url, title=title, company=company,
                                   location=location, description=description)
        result = await self._ask(prompt, JobScore, self.settings.scoring_effort, model=self.scoring_model)
        result.score = max(0, min(100, result.score))
        return result

    async def score_company(self, company: str) -> Fit:
        result = await self._ask(prompts.score_company(self.profile, company), Fit, self.settings.scoring_effort,
                                 model=self.scoring_model)
        result.score = max(0, min(100, result.score))
        return result

    async def write(self, channel: str, *, recipient: str | None = None, company: str | None = None,
                    job: str | None = None, previous: str | None = None, draft: str | None = None,
                    instruction: str | None = None, limit: int | None = None) -> Draft:
        def ask(draft_text: str | None, note: str | None) -> Any:
            prompt = prompts.write(self.profile, channel, recipient=recipient, company=company, job=job,
                                   previous=previous, draft=draft_text, instruction=note, limit=limit)
            return self._ask(prompt, Draft, self.settings.writing_effort)

        result = await ask(draft, instruction)
        result.body = result.body.strip()
        if limit and len(result.body) > limit:
            shorter = f"This version is {len(result.body)} characters; the hard limit is {limit}. Cut it to fit."
            result = await ask(result.body, shorter)
            result.body = clip(result.body, limit)
        if channel != "email":
            result.subject = None
        return result

    async def answer(self, *, question: str, kind: str, options: list[str], job: str | None) -> FieldAnswer:
        prompt = prompts.answer(self.profile, question=question, kind=kind, options=options, job=job)
        return await self._ask(prompt, FieldAnswer, self.settings.scoring_effort, model=self.scoring_model)

    async def scout(self, *, brief: str, n: int, exclude: list[str]) -> list[ScoutCompany]:
        prompt = prompts.scout(self.profile, brief=brief, n=n, exclude=exclude)
        result = await self._ask(prompt, ScoutResult, self.settings.research_effort, tools=[WEB_SEARCH])
        return result.companies

    async def pick_people(self, *, company: str, candidates: list[str], k: int) -> list[int]:
        if not candidates:
            return []
        prompt = prompts.pick_people(self.profile, company=company, candidates=candidates, k=k)
        picks = await self._ask(prompt, Picks, self.settings.scoring_effort, model=self.scoring_model)
        chosen: list[int] = []
        for number in picks.numbers:
            if 1 <= number <= len(candidates) and number - 1 not in chosen:
                chosen.append(number - 1)
        return chosen[:k]
