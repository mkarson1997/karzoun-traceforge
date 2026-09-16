import json
import logging

import pytest

from traceforge.audit import emit_audit_event, stable_ref


def test_stable_ref_is_deterministic_and_does_not_expose_original_value() -> None:
    original = "user@example.com"

    first = stable_ref(original)
    second = stable_ref(original)

    assert first == second
    assert first is not None
    assert original not in first
    assert first.startswith("sha256:")


def test_audit_event_is_structured_json_without_raw_actor_identifier(caplog) -> None:
    raw_actor = "00000000-1111-2222-3333-444444444444"
    actor_ref = stable_ref(raw_actor)

    with caplog.at_level(logging.INFO, logger="traceforge.audit"):
        emit_audit_event(
            "viewer.dataset_export",
            "success",
            component="viewer",
            actor_ref=actor_ref,
            rows=7,
            filtered=True,
        )

    payload = json.loads(caplog.records[-1].message)
    assert payload["schema"] == "traceforge.audit.v1"
    assert payload["event"] == "viewer.dataset_export"
    assert payload["outcome"] == "success"
    assert payload["actor_ref"] == actor_ref
    assert payload["rows"] == 7
    assert raw_actor not in caplog.records[-1].message


def test_audit_event_rejects_non_scalar_fields() -> None:
    with pytest.raises(TypeError):
        emit_audit_event(
            "bad",
            "denied",
            component="test",
            unsafe={"secret": "value"},
        )
