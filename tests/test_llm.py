from __future__ import annotations

from types import SimpleNamespace

import pytest

from jobpilot import prompts
from jobpilot.config import LLMSettings
from jobpilot.llm import LLM, Draft, JobScore, LLMError, ScoutResult, clip

from .conftest import make_settings


class Block:
    def __init__(self, data: dict):
        self.data = data

    def to_dict(self) -> dict:
        return dict(self.data)


class FakeMessages:
    def __init__(self, replies: list):
        self.replies = replies
        self.calls: list[dict] = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


def reply(parsed, stop: str = "end_turn", content: list | None = None):
    return SimpleNamespace(stop_reason=stop, parsed_output=parsed, content=content or [])


def make_llm(replies: list, **llm_settings) -> tuple[LLM, FakeMessages]:
    messages = FakeMessages(replies)
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    settings = make_settings()
    llm = LLM(LLMSettings(**llm_settings), settings.profile, "<resume>Ten years of Python</resume>", client)
    return llm, messages


async def test_score_request_shape():
    llm, messages = make_llm([reply(JobScore(score=130, reason="r", title=None, company=None, location=None))])
    result = await llm.score_job(site="LinkedIn", url="https://x", title="Backend", company="Acme",
                                 location=None, description="Build APIs in Python")
    assert result.score == 100  # clamped
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["betas"] == ["server-side-fallback-2026-07-01"] and call["fallbacks"] == "default"
    assert call["output_config"] == {"effort": "low"}
    assert call["output_format"] is JobScore
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Ten years of Python" in call["system"][0]["text"]
    assert "Build APIs in Python" in call["messages"][0]["content"]


async def test_cheaper_scoring_model_gets_no_effort_or_fallbacks():
    llm, messages = make_llm([reply(JobScore(score=70, reason="r", title=None, company=None, location=None)),
                              reply(Draft(subject=None, body="note"))], scoring_model="claude-haiku-4-5")
    await llm.score_job(site="x", url="u", title=None, company=None, location=None, description="d")
    await llm.write("li_connect", recipient="Dev", company="Acme", limit=200)
    scoring, writing = messages.calls
    assert scoring["model"] == "claude-haiku-4-5"
    assert "output_config" not in scoring and "fallbacks" not in scoring
    assert writing["model"] == "claude-opus-5" and writing["output_config"] == {"effort": "medium"}


async def test_refusal_and_missing_output_raise():
    llm, _ = make_llm([reply(None, stop="refusal")])
    with pytest.raises(LLMError, match="declined"):
        await llm.score_company("Acme")
    llm, _ = make_llm([reply(None)])
    with pytest.raises(LLMError, match="no structured answer"):
        await llm.score_company("Acme")


async def test_paused_research_turn_is_resumed():
    paused = reply(None, stop="pause_turn", content=[Block({"type": "server_tool_use", "id": "s1",
                                                            "parsed_output": None})])
    llm, messages = make_llm([paused, reply(ScoutResult(companies=[]))])
    assert await llm.scout(brief="AI startups", n=5, exclude=["Acme"]) == []
    second = messages.calls[1]
    assert second["tools"][0]["type"] == "web_search_20260209"
    assert second["messages"][1] == {"role": "assistant", "content": [{"type": "server_tool_use", "id": "s1"}]}
    assert "Acme" in messages.calls[0]["messages"][0]["content"]


async def test_connection_note_is_kept_under_the_limit():
    long_note = "word " * 60
    llm, messages = make_llm([reply(Draft(subject="ignored", body=long_note)),
                              reply(Draft(subject=None, body="Still " + long_note))])
    draft = await llm.write("li_connect", recipient="Dev", company="Acme", limit=200)
    assert len(draft.body) <= 200 and draft.subject is None
    assert "hard limit is 200" in messages.calls[1]["messages"][0]["content"]


def test_clip_prefers_sentence_ends():
    assert clip("First sentence is right here. Second one runs past.", 40) == "First sentence is right here."
    # a sentence end that would throw away most of the note loses to a word boundary
    assert clip("Hi Dev. I build LLM support agents at a large ride-hailing company.", 40) == \
        "Hi Dev. I build LLM support agents at a"
    assert clip("no punctuation at all in this long line of words", 20) == "no punctuation at"


def test_prompts_carry_the_right_instructions():
    profile = make_settings().profile
    email = prompts.write(profile, "email", recipient="Dev Patel, CTO", company="Acme")
    assert "subject:" in email and "Dev Patel" in email
    note = prompts.write(profile, "li_connect", recipient="Dev", limit=300)
    assert "300 characters" in note and "subject: null" in note
    answer = prompts.answer(profile, question="Years with Go?", kind="radio", options=["0-2", "3+"], job=None)
    assert '["0-2", "3+"]' in answer
    system = prompts.system(profile, "CONTEXT")
    assert system.endswith("CONTEXT") and "Asha" in system
