"""Your resume and profile, turned into the context every LLM call starts from."""

from __future__ import annotations

import re

from pypdf import PdfReader

from .config import Paths, Profile


def load_resume(paths: Paths) -> str:
    """resume.md wins when present (edit it if the PDF extracts badly); otherwise the PDF."""
    if paths.resume_md.exists():
        text = paths.resume_md.read_text()
    elif paths.resume_pdf.exists():
        text = "\n".join(page.extract_text() or "" for page in PdfReader(paths.resume_pdf).pages)
    else:
        raise FileNotFoundError(f"Put your resume at {paths.resume_pdf} (or a text version at {paths.resume_md}).")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 200:
        raise ValueError("Resume text is too short. If the PDF is a scan, save a text version as profile/resume.md.")
    return text


def candidate_context(profile: Profile, resume: str) -> str:
    """Facts the model may rely on. Salary, phone and email stay out; forms get those from config directly."""
    facts = [
        ("Name", profile.name),
        ("Based in", profile.city),
        ("LinkedIn", profile.linkedin_url),
        ("GitHub", profile.github_url),
        ("Website", profile.website),
        ("Total experience", f"{profile.years_experience:g} years" if profile.years_experience else ""),
        ("Notice period", f"{profile.notice_days()} days ({profile.start_text()})"),
        ("Target roles", ", ".join(profile.target_roles)),
        ("Preferred locations", ", ".join(profile.preferred_locations)),
        ("Work authorization", profile.work_authorization),
        ("What they want next", profile.pitch),
    ]
    lines = "\n".join(f"{label}: {value}" for label, value in facts if value)
    return f"<candidate_profile>\n{lines}\n</candidate_profile>\n\n<resume>\n{resume}\n</resume>"
