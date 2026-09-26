"""Filling application forms on any site.

A form is read generically (labels, options, required flags) and each empty field is answered
from, in order: answers you gave before, the rules in profile/answers.yaml, your profile, and
finally Claude. When none of them can answer truthfully, filling stops and the question is sent
to you on Telegram.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from playwright.async_api import Locator

from .browser import first_visible, pause
from .config import Profile
from .db import DB
from .llm import LLM

log = logging.getLogger(__name__)

SCAN_JS = r"""(root) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').replace(/\s*\*\s*$/, '').replace(/\s*\(?required\)?\s*$/i, '').trim();
  const shown = el => { if (!el) return false; const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const textById = id => { const n = id && document.getElementById(id); return n ? n.innerText : ''; };
  const markedRequired = (el, holder) => el.required || el.getAttribute('aria-required') === 'true' ||
    /\*\s*$/.test(((holder && holder.innerText) || '').trim()) ||
    !!(holder && (/required/i.test(holder.className || '') || holder.querySelector('[class*="required" i]')));
  const labelOf = el => {
    if (el.labels && el.labels.length) { const t = clean([...el.labels].map(l => l.innerText).join(' ')); if (t) return [t, el.labels[0]]; }
    const ids = el.getAttribute('aria-labelledby');
    if (ids) { const t = clean(ids.split(/\s+/).map(textById).join(' ')); if (t) return [t, document.getElementById(ids.split(/\s+/)[0])]; }
    const aria = clean(el.getAttribute('aria-label')); if (aria) return [aria, null];
    const fs = el.closest('fieldset'); const legend = fs && fs.querySelector('legend');
    if (legend && clean(legend.innerText)) return [clean(legend.innerText), legend];
    let node = el.parentElement;
    for (let i = 0; i < 4 && node && node !== root; i++, node = node.parentElement) {
      const first = clean((node.innerText || '').split('\n')[0]);
      if (first && first.length < 250) return [first, node];
    }
    return [clean(el.getAttribute('placeholder') || el.getAttribute('name') || ''), null];
  };
  const groupLabel = (radios, options) => {
    const fs = radios[0].closest('fieldset'); const legend = fs && fs.querySelector('legend');
    if (legend && clean(legend.innerText)) return [clean(legend.innerText), legend];
    const group = radios[0].closest('[role=radiogroup]');
    if (group) {
      const t = clean(group.getAttribute('aria-label') || (group.getAttribute('aria-labelledby') || '').split(/\s+/).map(textById).join(' '));
      if (t) return [t, group];
    }
    let node = radios[0].parentElement;
    while (node && node !== root && !radios.every(r => node.contains(r))) node = node.parentElement;
    for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
      const first = clean((node.innerText || '').split('\n').find(line => line.trim() && !options.includes(clean(line))) || '');
      if (first && first.length < 250) return [first, node];
      if (node === root) break;
    }
    return [clean(radios[0].name), null];
  };
  document.querySelectorAll('[data-jp-field]').forEach(e => { e.removeAttribute('data-jp-field'); e.removeAttribute('data-jp-opt'); });
  const fields = [];
  const seenGroups = new Set();
  for (const el of root.querySelectorAll('input, select, textarea')) {
    const type = (el.getAttribute('type') || '').toLowerCase();
    const tag = el.tagName.toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type) || el.disabled || el.readOnly) continue;
    const idx = fields.length;
    if (type === 'radio') {
      if (!el.name || seenGroups.has(el.name)) continue;
      seenGroups.add(el.name);
      const radios = [...root.querySelectorAll('input[type=radio]')].filter(r => r.name === el.name);
      if (!radios.some(r => shown(r) || (r.labels && shown(r.labels[0])))) continue;
      const options = radios.map(r => clean(r.labels && r.labels.length ? r.labels[0].innerText : r.value));
      const [label, holder] = groupLabel(radios, options);
      radios.forEach((r, j) => { r.setAttribute('data-jp-field', idx); r.setAttribute('data-jp-opt', j); });
      fields.push({idx, kind: 'radio', label, options, value: radios.some(r => r.checked) ? 'checked' : '',
                   required: radios.some(r => markedRequired(r, holder))});
      continue;
    }
    const visible = shown(el) || (el.labels && el.labels.length && shown(el.labels[0]));
    if (type !== 'file' && !visible) continue;
    el.setAttribute('data-jp-field', idx);
    let kind = tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : (type || 'text');
    if (kind === 'text' && (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete'))) kind = 'combobox';
    const [label, holder] = labelOf(el);
    let options = [], value = '';
    if (kind === 'select') {
      options = [...el.options].map(o => clean(o.text)).filter(t => t && !/^(select|choose|--)/i.test(t));
      const chosen = el.options[el.selectedIndex];
      value = chosen && chosen.value && !/^(select|choose|--)/i.test(chosen.text.trim()) ? clean(chosen.text) : '';
    } else if (kind === 'checkbox') {
      value = el.checked ? 'checked' : '';
    } else if (kind === 'file') {
      value = el.files && el.files.length ? 'file' : '';
    } else {
      value = (el.value || '').trim();
    }
    fields.push({idx, kind, label, options, value, required: markedRequired(el, holder)});
  }
  return fields;
}"""

CHECK_JS = """(el, want) => { if (el.checked !== want) (el.labels && el.labels.length ? el.labels[0] : el).click(); }"""
PICK_JS = """(el) => (el.labels && el.labels.length ? el.labels[0] : el).click()"""

UNTICK = re.compile(r"\b(follow|newsletter|updates|marketing|job alerts?|promotional)\b", re.IGNORECASE)
TICK = re.compile(r"\b(agree|consent|terms|privacy|acknowledge|certify|confirm|accurate)\b", re.IGNORECASE)
COUNTS = re.compile(r"\bhow many\b|\bnumber of\b|\b(years?|months?) of (work |professional |relevant )?experience\b"
                    r"|\((in )?(years|months)\)", re.IGNORECASE)


@dataclass
class Field:
    idx: int
    kind: str
    label: str
    required: bool
    options: list[str]
    value: str


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9+]+", " ", text.lower()).strip()


def _in_range(number: float, option: str) -> bool:
    text = option.lower()
    if found := re.search(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)", text):
        return float(found.group(1)) <= number <= float(found.group(2))
    if found := re.search(r"(\d+(?:\.\d+)?)\s*\+|(?:more than|over|above|at least)\s*(\d+(?:\.\d+)?)", text):
        return number >= float(found.group(1) or found.group(2))
    if found := re.search(r"(?:less than|under|below|up to)\s*(\d+(?:\.\d+)?)", text):
        return number < float(found.group(1))
    return False


def pick_option(answer: str, options: list[str]) -> str | None:
    """Map a free answer onto one of the offered options, or None when nothing fits clearly."""
    wanted = _norm(answer)
    if not wanted or not options:
        return None
    for option in options:
        if _norm(option) == wanted:
            return option
    wanted = {"y": "yes", "true": "yes", "n": "no", "false": "no"}.get(wanted, wanted)
    prefixed = [o for o in options if _norm(o) and (_norm(o).startswith(wanted) or wanted.startswith(_norm(o)))]
    if len(prefixed) == 1 or (prefixed and wanted in ("yes", "no")):
        return prefixed[0]
    contained = [o for o in options if _norm(o) and (wanted in _norm(o) or _norm(o) in wanted)]
    if len(contained) == 1:
        return contained[0]
    if number := re.fullmatch(r"\d+(?:\.\d+)?", wanted.split(" ")[0]):
        ranged = [o for o in options if _in_range(float(number.group()), o)]
        if len(ranged) == 1:
            return ranged[0]
    return prefixed[0] if len(prefixed) == 1 else None


def fit(answer: str, field: Field) -> str | None:
    """Shape an answer for the field: an exact option, digits for counts, or the text itself."""
    answer = answer.strip()
    if not answer:
        return None
    if field.kind in ("radio", "select"):
        return pick_option(answer, field.options)
    if field.kind in ("number", "text", "tel") and (field.kind == "number" or COUNTS.search(field.label)):
        found = re.search(r"\d+(?:\.\d+)?", answer.replace(",", ""))
        if not found:
            return None
        value = float(found.group())
        return str(int(value)) if COUNTS.search(field.label) or value.is_integer() else f"{value:g}"
    return answer


def money(value: str, question: str) -> str | None:
    """'24 LPA' stays as is, unless the form asks in rupees: then 2400000."""
    if not value:
        return None
    asks_rupees = re.search(r"\b(inr|rupees?|per annum|annual)\b|₹", question, re.IGNORECASE)
    asks_lakhs = re.search(r"\b(lakhs?|lacs?|lpa)\b", question, re.IGNORECASE)
    amount = re.search(r"\d+(?:\.\d+)?", value.replace(",", ""))
    if asks_rupees and not asks_lakhs and amount and re.search(r"\b(lpa|lakhs?|lacs?|l)\b", value, re.IGNORECASE):
        return str(int(float(amount.group()) * 100_000))
    return value


def from_profile(profile: Profile, field: Field) -> str | None:
    """Answers that come straight from config.yaml, without asking a model."""
    q = field.label.lower()
    names = profile.name.split()
    if re.search(r"\bfirst name\b", q):
        return names[0] if names else None
    if re.search(r"\b(last name|surname|family name)\b", q):
        return names[-1] if len(names) > 1 else None
    if re.fullmatch(r"\s*(full )?name\s*", q) or re.search(r"\byour (full )?name\b", q):
        return profile.name or None
    if re.search(r"\be-?mail\b", q):
        return profile.email or None
    if re.search(r"\bcountry code\b", q):
        return None
    if re.search(r"\b(mobile|phone|contact number|whatsapp)\b", q):
        return profile.phone or None
    if "linkedin" in q:
        return profile.linkedin_url or None
    if "github" in q:
        return profile.github_url or None
    if re.search(r"\b(website|portfolio|personal site|blog)\b", q):
        return profile.website or profile.github_url or None
    if "notice period" in q or re.search(r"\b(when can you (start|join)|availability to (start|join))\b", q):
        days = profile.notice_period_days
        return "Immediately" if days == 0 and field.kind in ("radio", "select") else f"{days} days"
    if re.search(r"\b(current|present|existing) (ctc|salary|compensation|pay|package)\b", q):
        return money(profile.current_ctc, q)
    if re.search(r"\b(expected|desired) (ctc|salary|compensation|pay|package)\b|salary expectation", q):
        return money(profile.expected_ctc, q)
    if re.search(r"\b(current city|current location|where are you (currently )?(based|located))\b", q) or q.strip() in (
            "city", "location", "location (city)"):
        return profile.city or None
    if "experience" in q and re.search(r"\b(total|overall|years)\b", q):
        tail = q.split("experience", 1)[1]
        if not re.search(r"\b(with|in|using|on)\b", tail) and profile.years_experience:
            return f"{profile.years_experience:g}"
    return None


def load_rules(path: Path) -> list[tuple[re.Pattern[str], str]]:
    """profile/answers.yaml: a list of {match: <regex>, answer: <text>}."""
    if not path.exists():
        return []
    rules = []
    for item in yaml.safe_load(path.read_text()) or []:
        rules.append((re.compile(str(item["match"]), re.IGNORECASE), str(item["answer"])))
    return rules


class AnswerBook:
    def __init__(self, db: DB, profile: Profile, llm: LLM | None, rules: list[tuple[re.Pattern[str], str]]):
        self.db = db
        self.profile = profile
        self.llm = llm
        self.rules = rules

    async def answer(self, field: Field, job: str | None) -> str | None:
        question = field.label
        if not question:
            return None
        candidates = [self.db.learned_answer(question)]
        candidates += [answer for pattern, answer in self.rules if pattern.search(question)]
        candidates.append(from_profile(self.profile, field))
        for candidate in candidates:
            if candidate is not None and (shaped := fit(candidate, field)) is not None:
                return shaped
        if self.llm is None:
            return None
        reply = await self.llm.answer(question=question, kind=field.kind, options=field.options, job=job)
        shaped = fit(reply.answer, field) if reply.confident else None
        if shaped is not None and (field.kind in ("radio", "select", "number") or len(shaped) <= 40):
            self.db.learn_answer(question, shaped, "llm")  # short factual answers stay consistent across forms
        return shaped


class FormFiller:
    def __init__(self, book: AnswerBook, resume: Path | None):
        self.book = book
        self.resume = resume

    async def fill(self, root: Locator, job: str | None, include_optional: bool = False) -> Field | None:
        """Fill every empty field that needs it. Returns the first field nobody could answer."""
        for raw in await root.evaluate(SCAN_JS):
            field = Field(**raw)
            element = root.locator(f'[data-jp-field="{field.idx}"]').first
            if field.kind == "checkbox":
                await self._checkbox(element, field)
                continue
            if field.value:
                continue
            if field.kind == "file":
                if self.resume and (field.required or (include_optional and re.search(r"resume|cv", field.label, re.I))):
                    await element.set_input_files(str(self.resume))
                continue
            if not (field.required or include_optional):
                continue
            answer = await self.book.answer(field, job)
            if answer is None:
                return field
            await self._set(root, element, field, answer)
            await pause(0.3, 0.9)
        return None

    async def _checkbox(self, element: Locator, field: Field) -> None:
        if UNTICK.search(field.label):
            await element.evaluate(CHECK_JS, False)
        elif TICK.search(field.label) or (field.required and not field.value):
            await element.evaluate(CHECK_JS, True)

    async def _set(self, root: Locator, element: Locator, field: Field, answer: str) -> None:
        if field.kind == "radio":
            option = root.locator(f'[data-jp-field="{field.idx}"][data-jp-opt="{field.options.index(answer)}"]')
            await option.first.evaluate(PICK_JS)
        elif field.kind == "select":
            await element.select_option(label=answer)
        elif field.kind == "combobox":
            await element.fill(answer)
            await pause(1.0, 1.8)
            suggestion = await first_visible(root.page.locator("[role='listbox'] [role='option']"))
            if suggestion is not None:
                await suggestion.click()
        else:
            await element.fill(answer)
