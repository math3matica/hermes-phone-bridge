#!/usr/bin/env python3
"""Deterministic turn selection for the physical call acceptance test.

This module is deliberately outside the production audio worker. It consumes
completed per-turn evidence and selects the authorized test utterance without
changing capture, VAD, STT, model, or document behavior.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Callable


class GateStatus(str, Enum):
    WAITING = "waiting"
    PASSED = "passed"
    TARGET_UTTERANCE_NOT_OBSERVED = "target-utterance-not-observed"


@dataclass(frozen=True)
class TurnEvidence:
    turn: int
    transcript: str
    model_facing_request: str
    evidence_ref: str | None
    classification: str


@dataclass(frozen=True)
class GateResult:
    status: GateStatus
    classification: str
    target_turn: int | None = None


def _tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.findall(r"[a-z0-9]+", normalized)


def _normalized(value: str) -> str:
    """Return normalized text while retaining lexical word boundaries."""
    return " ".join(_tokens(value))


def _contains_sequence(tokens: list[str], sequence: list[str]) -> int | None:
    width = len(sequence)
    for start in range(len(tokens) - width + 1):
        if tokens[start : start + width] == sequence:
            return start
    return None


def _blocked_command_context(tokens: list[str], command_start: int, command_end: int) -> bool:
    prefix = tokens[max(0, command_start - 5) : command_start]
    suffix = tokens[command_end : command_end + 4]

    blocked_prefixes = (
        ("do", "not"),
        ("don", "t"),
        ("should", "not"),
        ("will", "not"),
        ("never",),
        ("no",),
        ("not",),
        ("said",),
        ("say",),
        ("asked",),
        ("asking",),
        ("mentioned",),
        ("mention",),
    )
    if any(
        len(marker) <= len(prefix) and tuple(prefix[-len(marker) :]) == marker
        for marker in blocked_prefixes
    ):
        return True

    return (
        tuple(suffix[:2]) == ("as", "an")
        and len(suffix) >= 3
        and suffix[2] == "example"
    ) or suffix[:1] in (["yesterday"], ["earlier"], ["before"])


def _structured_command_match(transcript_tokens: list[str], target_tokens: list[str]) -> bool:
    """Recognize the narrow create-document intent for this acceptance target."""
    if not transcript_tokens or not target_tokens:
        return False

    identifier = target_tokens[-3:]
    identifier_start = _contains_sequence(transcript_tokens, identifier)
    if identifier_start is None:
        return False

    for verb in ("create", "make"):
        for command_start, token in enumerate(transcript_tokens):
            if token != verb or command_start >= identifier_start:
                continue
            command_end = identifier_start + len(identifier)
            if _blocked_command_context(transcript_tokens, command_start, command_end):
                continue

            middle = transcript_tokens[command_start + 1 : identifier_start]
            if not any(token in {"document", "file"} for token in middle):
                continue
            if not any(token in {"called", "named"} for token in middle):
                continue
            return True
    return False


def materially_matches(transcript: str, target_phrase: str) -> bool:
    """Match a narrow command intent, allowing conversational framing."""
    actual = _tokens(transcript)
    target = _tokens(target_phrase)
    if not actual or not target:
        return False

    normalized_actual = _normalized(transcript)
    normalized_target = _normalized(target_phrase)
    target_start = _contains_sequence(actual, target)
    if (
        target_start is not None
        and f" {normalized_target} " in f" {normalized_actual} "
    ):
        target_end = target_start + len(target)
        if not _blocked_command_context(actual, target_start, target_end):
            return True

    if _structured_command_match(actual, target):
        return True

    # Keep similarity as a bounded fallback for small STT substitutions, but
    # never let whole-utterance prefixes or suffixes lower the score.
    for width in range(max(1, len(target) - 1), len(target) + 2):
        for start in range(len(actual) - width + 1):
            window = actual[start : start + width]
            command_positions = [
                start + offset
                for offset, token in enumerate(window)
                if token in {"create", "make"}
            ]
            if not command_positions:
                continue
            command_start = command_positions[0]
            if _blocked_command_context(
                actual, command_start, min(len(actual), command_start + len(target))
            ):
                continue
            if not any(token in {"document", "file"} for token in window):
                continue
            if not any(token in {"called", "named"} for token in window):
                continue
            if SequenceMatcher(None, window, target).ratio() >= 0.80:
                return True
    return False


class PhysicalAcceptanceTurnGate:
    """Select one authorized target turn from bounded call conversation evidence."""

    def __init__(
        self,
        *,
        target_phrase: str,
        max_turns: int,
        timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        hangup: Callable[[], Any],
        log: Callable[[str], Any] = lambda _message: None,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.target_phrase = target_phrase
        self.max_turns = max_turns
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self.hangup = hangup
        self.log = log
        self.turns: list[TurnEvidence] = []
        self.status = GateStatus.WAITING
        self.target_turn: int | None = None
        self._deadline: float | None = None
        self._hangup_done = False
        self._terminal_result: GateResult | None = None

    def start_audio_active(self) -> None:
        if self._deadline is not None:
            raise RuntimeError("acceptance turn gate already started")
        self._deadline = self.clock() + self.timeout_seconds
        self.log(
            f"ACCEPTANCE_TARGET_WAIT_STARTED max_turns={self.max_turns} "
            f"timeout_seconds={self.timeout_seconds:g}"
        )

    def _timeout(self) -> GateResult:
        if self.status is GateStatus.TARGET_UTTERANCE_NOT_OBSERVED:
            assert self._terminal_result is not None
            return self._terminal_result
        if self.status is GateStatus.PASSED:
            assert self._terminal_result is not None
            return self._terminal_result
        self.status = GateStatus.TARGET_UTTERANCE_NOT_OBSERVED
        self.log("ACCEPTANCE_TARGET_UTTERANCE_NOT_OBSERVED")
        if not self._hangup_done:
            self._hangup_done = True
            self.hangup()
        self._terminal_result = GateResult(self.status, self.status.value)
        return self._terminal_result

    def check_timeout(self) -> GateResult:
        if self._deadline is None:
            raise RuntimeError("acceptance turn gate has not started")
        if self.status is not GateStatus.WAITING:
            assert self._terminal_result is not None
            return self._terminal_result
        if self.clock() >= self._deadline:
            return self._timeout()
        return GateResult(self.status, "waiting")

    def observe_turn(
        self,
        *,
        transcript: str,
        model_facing_request: str,
        evidence_ref: str | None = None,
    ) -> GateResult:
        if self._deadline is None:
            raise RuntimeError("acceptance turn gate has not started")
        if self.status is not GateStatus.WAITING:
            return self.check_timeout()
        if self.clock() >= self._deadline:
            return self._timeout()

        turn_number = len(self.turns) + 1
        classification = "target" if materially_matches(transcript, self.target_phrase) else "preamble"
        self.turns.append(
            TurnEvidence(
                turn=turn_number,
                transcript=transcript,
                model_facing_request=model_facing_request,
                evidence_ref=evidence_ref,
                classification=classification,
            )
        )
        self.log(f"ACCEPTANCE_TURN_{classification.upper()} turn={turn_number}")

        if classification == "target":
            self.status = GateStatus.PASSED
            self.target_turn = turn_number
            self.log(
                f"ACCEPTANCE_AUDIO_STT_PASS target_turn={turn_number} "
                "document_workflow_authorized=false"
            )
            self._terminal_result = GateResult(self.status, classification, turn_number)
            return self._terminal_result
        if turn_number >= self.max_turns:
            return self._timeout()
        return GateResult(self.status, classification)
