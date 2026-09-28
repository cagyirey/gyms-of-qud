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

        Wrappers are normalised; nothing is guessed. A model told to reply with
        an action id will wrap it in whatever the markdown habit suggests -- a
        code fence, a bullet, bold, an ``action_id:`` label -- and those are
        accepted, because rejecting them would reject a compliant answer.

        What is never done is looking for a candidate *inside* a longer string.
        An earlier version did, and because one candidate is a prefix of another
        it silently chose the wrong action::

            **Action: `move:NE`**  ->  move:N
            move:Evil              ->  move:E
            do not wait            ->  wait

        A northeast answer became north, and a refusal became a wait. After the
        wrappers are stripped, the remainder must equal a candidate exactly, and
        a reply that resolves to two different candidates is ambiguous rather
        than first-one-wins.
        """
        body = text.strip()
        if body.startswith("```"):
            body = "\n".join(
                line for line in body.splitlines() if not line.strip().startswith("```")
            ).strip()

        payload = None
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            for key in ("action_id", "action", "id", "choice", "answer"):
                value = payload.get(key)
                if isinstance(value, str):
                    found = self._exact(value)
                    if found:
                        return found
            raise IllegalAction(f"no candidate in JSON response: {body[:120]!r}",
                                self.action_ids)

        # Every line is examined before anything is chosen, so a reply naming two
        # candidates is reported as ambiguous instead of resolved by line order.
        named: list[str] = []
        for line in body.splitlines():
            found = self._from_line(line)
            if found and found not in named:
                named.append(found)
        if len(named) == 1:
            return named[0]
        if len(named) > 1:
            raise IllegalAction(
                f"reply names more than one action: {sorted(named)}", self.action_ids
            )
        raise IllegalAction(body[:120], self.action_ids)

    # Markdown emphasis, code ticks, quotes and trailing sentence punctuation
    # are decoration around the id, not part of it.
    _DECORATION = "*_`\"'.,:;!?()[]{}"

    @classmethod
    def _strip_decoration(cls, token: str) -> str:
        return token.strip().strip(cls._DECORATION).strip()

    @classmethod
    def _strip_list_marker(cls, line: str) -> str:
        """Remove a leading bullet or ordinal: "- x", "* x", "1. x", "2) x"."""
        text = line.strip()
        if text[:1] in ("-", "*", "+"):
            return text[1:].strip()
        i = 0
        while i < len(text) and text[i].isdigit():
            i += 1
        if i and i < len(text) and text[i] in ".)":
            return text[i + 1:].strip()
        return text

    def _exact(self, token: str) -> str | None:
        """A candidate equal to the token, or None. Never a substring test."""
        cleaned = self._strip_decoration(token)
        return cleaned if cleaned in self.candidates else None

    def _from_line(self, line: str) -> str | None:
        """Resolve one line, allowing only the documented wrappers."""
        direct = self._exact(self._strip_list_marker(line))
        if direct:
            return direct
        # "Action: move:E", "action_id = move:E", "**Move** -> move:E". The tail
        # after the first separator must equal a candidate once unwrapped.
        for sep in (":", "=", "->"):
            found_at = line.find(sep)
            if found_at < 0:
                continue
            tail = self._strip_list_marker(line[found_at + len(sep):])
            found = self._exact(tail)
            if found:
                return found
        return None

    def retry_message(self, rejected: str) -> str:
        """Appended to the prompt after a rejection, never sent alone.

        The correction keeps the observation. An earlier version replaced the
        prompt with this text, which stripped the game state and left the model
        with a bare list of ids and no idea what it was being asked -- it
        answered "I'm a coding assistant running in OpenCode". Restating the
        format against the same context is what a retry is for.
        """
        return (
            f"\nYour previous reply was not accepted: {rejected.strip()[:200]!r}\n"
            f"Reply with exactly one action id, copied from this list, and no other text:\n"
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
