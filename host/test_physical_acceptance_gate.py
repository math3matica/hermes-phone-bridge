from physical_acceptance_gate import (
    GateStatus,
    PhysicalAcceptanceTurnGate,
    materially_matches,
)


TARGET = "Create a document called phone save test."


def test_target_matcher_accepts_conversational_prefixes_and_suffixes():
    positive_cases = [
        "Create a document called phone save test.",
        "Okay, create a document called phone save test.",
        "Okay, I want you to create a document called phone save test.",
        "Rex, please create a document called phone save test.",
        "Can you create a document called phone save test for me?",
        "CREATE A DOCUMENT CALLED PHONE SAVE TEST!",
    ]

    assert all(materially_matches(case, TARGET) for case in positive_cases)


def test_target_matcher_rejects_negated_referential_and_unrelated_speech():
    negative_cases = [
        "Let's test the phone document system.",
        "Don't create a document called phone save test.",
        "Can you read phone save test?",
        "I created a document called phone save test yesterday.",
        "I said, create a document called phone save test, as an example.",
        "The phone save test document is unrelated to what we are doing.",
    ]

    assert not any(materially_matches(case, TARGET) for case in negative_cases)


def test_unrelated_preamble_is_retained_and_does_not_fail_gate():
    events = []
    gate = PhysicalAcceptanceTurnGate(
        target_phrase=TARGET,
        max_turns=4,
        timeout_seconds=60,
        clock=lambda: 10.0,
        hangup=lambda: events.append("hangup"),
        log=events.append,
    )
    gate.start_audio_active()

    result = gate.observe_turn(
        transcript="You hear me?",
        model_facing_request="You hear me?",
        evidence_ref="turn-0.wav",
    )

    assert result.status is GateStatus.WAITING
    assert result.classification == "preamble"
    assert gate.turns[0].transcript == "You hear me?"
    assert gate.turns[0].evidence_ref == "turn-0.wav"
    assert events == [
        "ACCEPTANCE_TARGET_WAIT_STARTED max_turns=4 timeout_seconds=60",
        "ACCEPTANCE_TURN_PREAMBLE turn=1",
    ]


def test_target_on_later_turn_passes_and_preserves_prior_evidence():
    gate = PhysicalAcceptanceTurnGate(
        target_phrase=TARGET,
        max_turns=4,
        timeout_seconds=60,
        clock=lambda: 10.0,
        hangup=lambda: (_ for _ in ()).throw(AssertionError("must not hang up on pass")),
    )
    gate.start_audio_active()
    gate.observe_turn(
        transcript="All right, let's get this file thing tested, okay?",
        model_facing_request="All right, let's get this file thing tested, okay?",
        evidence_ref="turn-0.wav",
    )

    result = gate.observe_turn(
        transcript="Create a document called phone save test.",
        model_facing_request="Create a document called phone save test.",
        evidence_ref="turn-1.wav",
    )

    assert result.status is GateStatus.PASSED
    assert result.classification == "target"
    assert result.target_turn == 2
    assert [turn.classification for turn in gate.turns] == ["preamble", "target"]
    assert [turn.evidence_ref for turn in gate.turns] == ["turn-0.wav", "turn-1.wav"]


def test_timeout_classifies_target_not_observed_and_hangs_up_once():
    now = [100.0]
    hangups = []
    gate = PhysicalAcceptanceTurnGate(
        target_phrase=TARGET,
        max_turns=4,
        timeout_seconds=5,
        clock=lambda: now[0],
        hangup=lambda: hangups.append("hangup"),
    )
    gate.start_audio_active()
    gate.observe_turn(
        transcript="You hear me?",
        model_facing_request="You hear me?",
        evidence_ref="turn-0.wav",
    )
    now[0] = 106.0

    result = gate.check_timeout()

    assert result.status is GateStatus.TARGET_UTTERANCE_NOT_OBSERVED
    assert result.classification == "target-utterance-not-observed"
    assert hangups == ["hangup"]
    assert gate.check_timeout() is result
    assert hangups == ["hangup"]


def test_turn_bound_is_bounded_and_never_redials():
    actions = []
    gate = PhysicalAcceptanceTurnGate(
        target_phrase=TARGET,
        max_turns=2,
        timeout_seconds=60,
        clock=lambda: 10.0,
        hangup=lambda: actions.append("hangup"),
    )
    gate.start_audio_active()
    gate.observe_turn(
        transcript="You hear me?",
        model_facing_request="You hear me?",
        evidence_ref="turn-0.wav",
    )

    result = gate.observe_turn(
        transcript="All right, let's get this file thing tested, okay?",
        model_facing_request="All right, let's get this file thing tested, okay?",
        evidence_ref="turn-1.wav",
    )

    assert result.status is GateStatus.TARGET_UTTERANCE_NOT_OBSERVED
    assert result.classification == "target-utterance-not-observed"
    assert actions == ["hangup"]
    assert not hasattr(gate, "redial")
