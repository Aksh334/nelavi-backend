"""Nelavi deterministic advice: a rules engine that applies documented rules to a validated Record."""

from advice.engine import EMPTY_RULESET, evaluate, validate_ruleset

__all__ = ["EMPTY_RULESET", "evaluate", "validate_ruleset"]
