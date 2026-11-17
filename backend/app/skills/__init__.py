"""Skills / tools registry (Feature 2.3).

Each tool declares its required scope; the agent is exposed ONLY the tools the
principal's role allows (role.policy["skills"] allowlist; absent = all). Every
data tool still goes through governance — skills never bypass govern().
"""
from .registry import Ctx, SKILLS, allowed_skills, run_skill

__all__ = ["Ctx", "SKILLS", "allowed_skills", "run_skill"]
