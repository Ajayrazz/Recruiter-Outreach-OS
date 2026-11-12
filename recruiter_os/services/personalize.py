"""Deterministic personalization: no AI needed. Selects *relevant* profile facts per recruiter/role.

Nothing is invented: every fact comes from the editable profile; missing data is simply omitted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

ROLE_TAGS = {
    "SDE Intern": ["sde", "dsa", "backend", "fullstack"],
    "SWE Intern": ["swe", "dsa", "backend", "fullstack"],
    "Backend Intern": ["backend", "sde"],
    "Full-Stack Intern": ["fullstack", "backend"],
    "AI Engineer Intern": ["ai", "backend", "fullstack"],
}
ROLE_SUBJECTS = {
    "SDE Intern": "Application for SDE Intern Opportunities",
    "SWE Intern": "Exploring Software Engineering Internship Opportunities",
    "Backend Intern": "Exploring Backend Engineering Internship Opportunities",
    "Full-Stack Intern": "Exploring Full-Stack Development Internship Opportunities",
    "AI Engineer Intern": "Exploring AI Engineering Internship Opportunities",
}
_env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, trim_blocks=True, lstrip_blocks=True)


class TemplateError(ValueError):
    pass


def greeting_name(name: str) -> str:
    """First name only for greeting; neutral greeting if unusable. Never guesses gender/honorifics."""
    n = re.sub(r"\b(dr|mr|mrs|ms|prof)\.?\s+", "", (name or "").strip(), flags=re.I)
    first = n.split()[0] if n.split() else ""
    first = re.sub(r"[^A-Za-zÀ-ɏ'\-]", "", first)
    if len(first) < 2 or "@" in name:
        return ""
    return first[0].upper() + first[1:] if first.islower() else first


def _score(tags: list[str], wanted: list[str]) -> int:
    return sum(len(wanted) - wanted.index(t) for t in tags if t in wanted)


def _top(items: list[dict], wanted: list[str], n: int) -> list[dict]:
    ranked = sorted(items, key=lambda i: -_score(i.get("tags", []), wanted))
    return [i for i in ranked if _score(i.get("tags", []), wanted) > 0][:n] or ranked[:min(n, 1)]


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def build_context(profile: dict[str, Any], contact: dict[str, str], role: str, mode: str) -> dict[str, Any]:
    from recruiter_os.services.settings import DEFAULT_PROFILE
    profile = {**DEFAULT_PROFILE, **profile}
    wanted = ROLE_TAGS.get(role, ROLE_TAGS["SDE Intern"])
    exp = _top(profile.get("experience", []), wanted, 1)
    projects = _top(profile.get("projects", []), wanted, 1 if mode == "A" else 2)
    ach = _top(profile.get("achievements", []), wanted, 1 if mode != "B" else 2)
    skills_pool = {"Backend Intern": profile.get("backend", []) + profile.get("languages", [])[:3],
                   "Full-Stack Intern": profile.get("frontend", []) + profile.get("backend", []),
                   "AI Engineer Intern": ["Python"] + profile.get("backend", [])[:2],
                   }.get(role, profile.get("languages", [])[:4] + profile.get("backend", [])[:2])
    skills = list(dict.fromkeys(skills_pool))[:6]
    e = exp[0] if exp else None
    return {
        "recruiter_name": greeting_name(contact.get("name", "")),
        "recruiter_title": contact.get("title", ""),
        "company": contact.get("company", ""),
        "role": role,
        "role_title": role.replace("Intern", "Internship").strip(),
        "role_subject": ROLE_SUBJECTS.get(role, ROLE_SUBJECTS["SDE Intern"]),
        "me": profile,
        "my_name": profile.get("name", ""),
        "education_line": ", ".join(x for x in [
            f"{profile.get('status', '')} pursuing {profile.get('education', '')}".strip(),
            f"at {profile.get('university', '')}" if profile.get("university") else ""] if x).replace(", at", " at"),
        "graduation_year": profile.get("graduation_year", ""),
        "skills": _join(skills),
        "experience": ({"role": e["role"], "company": e["company"], "points": e["points"][:1 if mode == "A" else 2]} if e else None),
        "projects": [{"name": p["name"], "description": p["description"]} for p in projects],
        "achievements": _join([a["text"] for a in ach]),
        "linkedin": profile.get("linkedin", ""), "github": profile.get("github", ""), "portfolio": profile.get("portfolio", ""),
    }


def render(template_str: str, ctx: dict[str, Any]) -> str:
    try:
        out = _env.from_string(template_str).render(**ctx)
    except Exception as exc:
        raise TemplateError(f"Template error: {exc}") from exc
    out = re.sub(r"[ \t]+\n", "\n", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip() + "\n"


def render_subject(template_str: str, ctx: dict[str, Any]) -> str:
    return " ".join(render(template_str, ctx).split())


SIGNATURE = """{{ my_name }}
{% if me.education %}{{ me.education }}{% endif %}{% if me.university %}, {{ me.university }}{% endif %}{% if graduation_year %} (Class of {{ graduation_year }}){% endif %}

{% if linkedin %}LinkedIn: {{ linkedin }}
{% endif %}{% if github %}GitHub: {{ github }}
{% endif %}{% if portfolio %}Portfolio: {{ portfolio }}{% endif %}"""

GREETING = "{% if recruiter_name %}Hi {{ recruiter_name }},{% else %}Hello,{% endif %}"

DEFAULT_TEMPLATES: list[dict[str, Any]] = [
    {"name": "A – Standard Internship Outreach", "category": "A", "is_default": True,
     "subject": "Exploring Software Engineering Internship Opportunities",
     "body": GREETING + """

I am {{ my_name }}, a {{ education_line }}, graduating in {{ graduation_year }}. I am writing to ask whether {{ company or 'your organisation' }} is considering software engineering interns, and whether I might be considered for any suitable opening.
{% if experience %}
Most recently I worked as a {{ experience.role }} at {{ experience.company }}, where I {{ experience.points[0] }}.
{% endif %}{% if skills %}
My core strengths are {{ skills }}{% if projects %}, and I recently built {{ projects[0].name }}, {{ projects[0].description }}{% endif %}.
{% endif %}
My resume is attached for your reference. I would be grateful if you could consider my profile, or point me to the right person or process if this is not the correct channel.

Thank you for your time.

Best regards,
""" + SIGNATURE},
    {"name": "B – Technical Outreach", "category": "B", "is_default": False,
     "subject": "Internship Opportunity Inquiry – Final-Year CSE Student",
     "body": GREETING + """

I am {{ my_name }}, a {{ education_line }} (graduating {{ graduation_year }}), and I am interested in software engineering internship opportunities at {{ company or 'your organisation' }}.
{% if achievements %}
On the engineering side, I have {{ achievements }}.
{% endif %}{% if experience %}
As a {{ experience.role }} at {{ experience.company }}, I {{ experience.points | join('; and I ') }}.
{% endif %}{% if projects %}
Projects I can discuss in detail:
{% for p in projects %}- {{ p.name }}: {{ p.description }}
{% endfor %}{% endif %}
My toolkit includes {{ skills }}. My resume is attached, and I would welcome any guidance on whether there is a suitable internship opening.

Thank you for considering my request.

Best regards,
""" + SIGNATURE},
    {"name": "C – Role-Specific Outreach", "category": "C", "is_default": False,
     "subject": "{{ role_subject }}",
     "body": GREETING + """

I am {{ my_name }}, a {{ education_line }}, and I am reaching out about {{ role }} opportunities at {{ company or 'your organisation' }}.
{% if experience %}
For this kind of role, the most relevant part of my background is my time as a {{ experience.role }} at {{ experience.company }}, where I {{ experience.points[0] }}.
{% endif %}{% if projects %}
I have also built {{ projects[0].name }}, {{ projects[0].description }}.
{% endif %}{% if skills %}
I work primarily with {{ skills }}.{% endif %}

If {{ company or 'your team' }} has, or expects to have, a {{ role }} opening, I would appreciate being considered. My resume is attached; if you are not the right contact, I would be grateful for a pointer to the right person.

Thank you for your time.

Best regards,
""" + SIGNATURE},
    {"name": "Follow-up (gentle)", "category": "followup", "is_default": False, "is_followup": True,
     "subject": "Re: {{ original_subject }}",
     "body": GREETING + """

I wanted to follow up briefly on my earlier note about {{ role }} opportunities at {{ company or 'your organisation' }}. I understand you are busy; if there is no suitable opening right now, no problem at all, and please tell me if you would prefer that I do not write again.

Thank you for your time.

Best regards,
""" + SIGNATURE},
]
