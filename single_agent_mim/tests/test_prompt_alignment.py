"""Prompt contracts for experience-internalized, compact runtime Skills."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mim.agents.access import AccessAgent
from mim.agents.construction import ConstructionAgent
from mim.schemas import Side, SkillRecord


def _skill(side: Side) -> SkillRecord:
    return SkillRecord(
        skill_id=f"sk_{side.value}_test",
        side=side,
        name="Keep dated transitions",
        description=(
            "When an explicit old-to-new state transition includes a date; "
            "not when no transition is stated."
        ),
        content=["Preserve both states and the explicit transition date."],
    )


def test_access_runtime_treats_skill_as_triggered_learned_prior():
    agent = AccessAgent.__new__(AccessAgent)
    agent._prompt = "{skills_section}\nsteps={max_steps}"
    agent._max_steps = 6

    system = agent._build_system([_skill(Side.ACCESS)])

    assert "learned behavioral priors" in system
    assert "no prior failed attempt is required" in system
    assert "When an explicit old-to-new state transition" in system
    assert "default behaviour has failed" not in system


def test_construction_runtime_renders_description_as_trigger():
    rendered = ConstructionAgent._render_skills(
        [_skill(Side.CONSTRUCTION)],
        empty="none",
    )

    assert "learned behavioral priors" in rendered
    assert "**When:** When an explicit old-to-new state transition" in rendered
    assert "**Do:**" in rendered
    assert "no failed default attempt is required" in rendered
    assert "default policy would fail" not in rendered


def test_maintenance_prompts_accept_experience_and_keep_runtime_skill_compact():
    names = (
        "cluster_summarizer_access.md",
        "cluster_summarizer_construction.md",
        "batch_crud_access.md",
        "batch_crud_construction.md",
    )
    for name in names:
        text = (ROOT / "prompts" / "skill_maker" / name).read_text(
            encoding="utf-8"
        )
        compact = " ".join(text.split())
        assert "standard or contrastive" in compact
        assert "REPAIR, ADOPT, or PRESERVE_AVOID" in compact
        for field in ('"name"', '"description"', '"content"'):
            assert field in compact
        assert "every content item MUST" not in compact
