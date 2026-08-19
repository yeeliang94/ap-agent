"""The employee's listing category — decided by the client's rule.

The listing row for an employee carries ONE category and GL code, taken
from the client's own category list. Which one a mixed report gets follows
the client's confirmed rule (for LinkedIn: the report's overall purpose —
the "Business Reason" header plus the line reasons). The AI applies the
rule and QUOTES the text it relied on; code checks the answer is on the
list. If the rule does not settle it, or there is no rule yet, the answer
is "unsure" and the worker raises CATEGORY_UNCLEAR for a person to decide
— never a silent guess.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..model_layer import USAGE_LIMITS, create_agent
from .evidence import ai_call


def _bare(text: str) -> str:
    """'Taxi (GL 713070)' / 'Taxi (713070)' → 'taxi'."""
    import re

    return re.sub(r"\s*\((?:GL\s*)?\d{4,8}\)\s*$", "", text or "", flags=re.IGNORECASE).strip().lower()


# Category names that mean "none of the above". A confident answer here is
# treated as unsure whenever the report states a purpose.
CATCH_ALL = {"miscellaneous", "misc", "other", "others", "sundry", "sundries", "general"}


class CategoryJudgment(BaseModel):
    category: str = Field(max_length=80, description="the item name only, exactly as listed (without the GL code)")
    quoted_text: str = Field(max_length=300, description="the header / line text relied on, verbatim")
    sure: bool
    why: str = Field(max_length=300)


_INSTRUCTIONS = (
    "You decide which ONE category from a client's own list an employee "
    "expense report should be booked under on the payment listing. Apply "
    "the client's rule if one is given; if no rule is given, be sure only "
    "when every line plainly belongs to the same category. Quote, verbatim, "
    "the header text or line text you relied on. If the rule does not "
    "settle it, answer sure=false and say why — a person will decide. Never "
    "invent a category that is not on the list. Prefer a specific category "
    "that the stated purpose names over a catch-all such as Miscellaneous or "
    "Other; use a catch-all only when nothing else fits at all."
)


async def judge_category(categories: list[dict], purpose: str, rows: list[dict],
                         rule: str, examples: list[str], usage=None) -> tuple[CategoryJudgment, str]:
    """Returns (judgment, gl). The category is validated against the list
    by code; an off-list answer is treated as unsure."""
    listing = "\n".join(f"- {c['item']}" + (f" (GL {c['gl']})" if c.get("gl") else "")
                        for c in categories)
    lines = "\n".join(f"- {r.get('date', '')} | {r.get('item_name') or r.get('item', '')} | "
                      f"{r.get('reason', '')} | {r.get('currency', 'MYR')} {r.get('amount', '')}"
                      for r in rows[:60])
    prompt = (f"# Category list\n{listing}\n\n"
              f"# The client's rule for a mixed report\n{rule.strip() or '(no rule confirmed yet)'}\n\n"
              f"# The report's stated business reason / purpose\n{purpose.strip() or '(none)'}\n\n"
              f"# The report's lines (date | item | reason | amount)\n{lines}\n")
    if examples:
        prompt += "\n# How this client categorised similar reports before\n" + "\n".join(f"- {e}" for e in examples[:12])
    agent = create_agent("judge", CategoryJudgment, _INSTRUCTIONS, temperature=0)
    if usage is not None:
        usage.reserve()
    result = await ai_call(agent.run(prompt, usage_limits=USAGE_LIMITS), "the category judge")
    if usage is not None:
        usage.add(result)
    j = result.output
    by_name = {c["item"].strip().lower(): c for c in categories}
    hit = by_name.get(_bare(j.category))
    # A catch-all is not a decision. Code, not the model, decides that a
    # confident "Miscellaneous" for a report whose purpose names something
    # else goes to a person.
    if hit is not None and _bare(hit["item"]) in CATCH_ALL and purpose.strip():
        j = j.model_copy(update={"sure": False,
                                 "why": (f"'{hit['item']}' is a catch-all; the report states a purpose, so a "
                                         f"person should choose. {j.why}")[:300]})
        return j, hit.get("gl", "")
    if hit is None:
        j = j.model_copy(update={"sure": False, "why": f"'{j.category}' is not on the client's list. {j.why}"[:300]})
        return j, ""
    return j.model_copy(update={"category": hit["item"]}), hit.get("gl", "")
