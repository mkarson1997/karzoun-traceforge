from __future__ import annotations

from traceforge.adapters import build_export_request, normalize_records


def test_codex_adapter_keeps_metadata_without_command_or_agent_text():
    marker = "customer-secret-codex"
    records = [
        {
            "type": "thread.started",
            "thread_id": "thread-1",
            "timestamp": 1_700_000_000_000,
        },
        {
            "type": "item.completed",
            "timestamp": 1_700_000_000_100,
            "item": {
                "id": "item-1",
                "type": "command_execution",
                "status": "completed",
                "command": f"echo {marker}",
            },
        },
        {
            "type": "item.completed",
            "timestamp": 1_700_000_000_200,
            "item": {
                "id": "item-2",
                "type": "agent_message",
                "text": f"private answer {marker}",
            },
        },
    ]

    events = normalize_records("codex", records)
    assert len(events) == 3
    assert events[1].session_id == "thread-1"
    assert events[1].attributes["adapter.item.type"] == "command_execution"
    serialized = build_export_request(events).SerializeToString()
    assert marker.encode() not in serialized
    assert b"private answer" not in serialized


def test_claude_adapter_records_tool_names_without_tool_input_or_result_text():
    marker = "customer-secret-claude"
    records = [
        {
            "type": "system",
            "subtype": "init",
            "session_id": "claude-session",
            "model": "claude-test",
            "timestamp": "2026-09-18T10:00:00Z",
        },
        {
            "type": "assistant",
            "session_id": "claude-session",
            "timestamp": "2026-09-18T10:00:01Z",
            "message": {
                "model": "claude-test",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": f"echo {marker}"},
                    },
                    {"type": "text", "text": f"sensitive response {marker}"},
                ],
                "usage": {"input_tokens": 12, "output_tokens": 8},
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "session_id": "claude-session",
            "is_error": False,
            "result": f"final secret {marker}",
            "num_turns": 2,
        },
    ]

    events = normalize_records("claude", records)
    assert events[1].attributes["tool.names"] == "Bash"
    assert events[2].status == "completed"
    serialized = build_export_request(events).SerializeToString()
    assert marker.encode() not in serialized
    assert b"sensitive response" not in serialized
    assert b"final secret" not in serialized


def test_copilot_adapter_ignores_prompt_cwd_and_tool_args():
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "copilot-session",
        "timestamp": "2026-09-18T10:00:00Z",
        "cwd": "/home/private/repo",
        "tool_name": "bash",
        "tool_input": {"command": "echo customer-secret"},
        "initial_prompt": "private prompt",
    }

    events = normalize_records("copilot", [payload])
    assert events[0].name == "copilot.PostToolUse"
    assert events[0].attributes["tool.name"] == "bash"
    serialized = build_export_request(events).SerializeToString()
    assert b"customer-secret" not in serialized
    assert b"private prompt" not in serialized
    assert b"/home/private/repo" not in serialized


def test_generic_adapter_scrubs_sensitive_attributes_before_otlp():
    records = [
        {
            "type": "tool.completed",
            "session_id": "generic-session",
            "task_id": "generic-task",
            "timestamp": 1_700_000_000,
            "status": "completed",
            "attributes": {
                "tool.name": "shell",
                "authorization": "sensitive-value",
                "safe.count": 4,
            },
        }
    ]

    events = normalize_records("generic", records)
    request = build_export_request(events)
    serialized = request.SerializeToString()
    assert b"sensitive-value" not in serialized
    assert b"safe.count" in serialized

    span = request.resource_spans[0].scope_spans[0].spans[0]
    keys = {item.key for item in span.attributes}
    assert "traceforge.session.id" in keys
    assert "traceforge.task.id" in keys
