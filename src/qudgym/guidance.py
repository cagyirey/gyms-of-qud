"""Action space derived from an Observation, as a grammar.

One place decides which actions are legal at a decision boundary. The grammar
is used three ways:

- to validate what the operating model returned, so an illegal action is
  rejected with the candidate list rather than being sent to the game;
- to drive true constrained decoding, by handing the same grammar to
  llguidance's LLMatcher when a local tokenizer is available;
- to state the contract in the operating instructions, so the model is told the
  same thing the validator enforces.

The operating model is hosted, so token masks are not available from it and
constrained decoding cannot run against it in flight. The grammar is still the
authoritative description of the action space, which is what makes the hosted
path safe and makes swapping in a local model a configuration change rather
than a rewrite.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from .models import Observation

try:  # Optional: validation works without it, constrained decoding does not.
    import llguidance
except ImportError:  # pragma: no cover - exercised by the import guard test
    llguidance = None


class IllegalAction(ValueError):
    """Raised when an action is not in the candidate set for a boundary."""

    def __init__(self, action_id: str, candidates: list[str]) -> None:
        self.action_id = action_id
        self.candidates = candidates
        super().__init__(
            f"action {action_id!r} is not a candidate at this boundary; "
            f"candidates: {candidates}"
        )


@dataclass(frozen=True)
class ActionSpace:
    """The legal actions at one decision boundary, plus their grammar."""

    candidates: tuple[str, ...]
    kinds: dict[str, str]
    grammar: str

    @property
    def action_ids(self) -> list[str]:
        return list(self.candidates)

    def contains(self, action_id: str) -> bool:
        return action_id in self.candidates

    def validate(self, action_id: str) -> str:
        """Return the action id, or raise with the candidate list attached."""
        if not self.contains(action_id):
            raise IllegalAction(action_id, self.action_ids)
        return action_id

    def extract(self, text: str) -> str:
        """Pull the action id out of a model response.

        Strict on purpose. A bare id, a fenced block, a JSON object with
        action_id, an "action_id: x" line, and a single-item bullet are all
        accepted. Prose is not: "I pick open" is not silently coerced into
        "answer:open", because a wrong action is far more expensive here than a
        retry. The caller re-prompts with the candidate list instead.
        """
        candidate = text.strip()
        if candidate.startswith("```"):
            lines = [ln for ln in candidate.splitlines() if not ln.strip().startswith("```")]
            candidate = "\n".join(lines).strip()
        if candidate in self.candidates:
            return candidate
        try:
            payload = json.loads(candidate)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            for key in ("action_id", "action", "id"):
                value = payload.get(key)
                if isinstance(value, str) and value in self.candidates:
                    return value
        for line in candidate.splitlines():
            stripped = line.strip()
            if stripped in self.candidates:
                return stripped
            # "action_id: move:E" and friends.
            for sep in (":", "="):
                if sep in stripped:
                    tail = stripped.split(sep, 1)[1].strip().strip("\"'`.,")
                    if tail in self.candidates:
                        return tail
            # A single-item bullet.
            bullet = stripped.lstrip("-*0123456789. \t").strip()
            if bullet in self.candidates:
                return bullet
        raise IllegalAction(candidate[:120], self.action_ids)

    def retry_message(self, rejected: str) -> str:
        """What to send back after a rejection.

        The model gets the exact legal set rather than a paraphrase, because
        the grammar is already the authority and this just re-states it.
        """
        return (
            f"{rejected!r} is not a legal action. "
            f"Reply with exactly one of these ids and nothing else: "
            f"{', '.join(self.candidates)}"
        )


def build_action_space(observation: Observation) -> ActionSpace:
    """Derive the grammar for one boundary from its Observation."""
    candidates = tuple(a.id for a in observation.actions)
    kinds = {a.id: a.kind for a in observation.actions}
    if not candidates:
        raise ValueError("a nonterminal boundary must offer at least one action")
    grammar = _choice_grammar(candidates)
    return ActionSpace(candidates=candidates, kinds=kinds, grammar=grammar)


def _choice_grammar(candidates: Iterable[str]) -> str:
    """A grammar accepting exactly one of the candidate ids.

    Each alternative is already a quoted GBNF literal, so they are joined
    directly rather than wrapped in a second layer of quotes.
    """
    body = " | ".join(_gbnf_literal(c) for c in candidates)
    return f"start: {body}\n"


def _gbnf_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def matcher_for(observation: Observation, tokenizer: Any) -> Any:
    """Build an LLMatcher for true constrained decoding.

    Requires llguidance and a local tokenizer. Raises a clear error otherwise
    rather than silently falling back, because constrained decoding and
    validation are different guarantees and conflating them would be the exact
    false-success shape this project has already been bitten by.
    """
    if llguidance is None:
        raise RuntimeError("llguidance is required for constrained decoding")
    space = build_action_space(observation)
    matcher = llguidance.LLMatcher(tokenizer, space.grammar)
    matcher.validate_grammar(space.grammar)
    return matcher
