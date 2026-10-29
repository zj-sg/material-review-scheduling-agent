"""Observable execution records; explanations come from model outputs, not templates."""


def return_summary(name, output):
    if not isinstance(output, dict):
        return str(output)[:300]
    data = output.get("data") or {}
    if name == "read_material":
        return f"status={output.get('status')}; material_id={data.get('material_id')}; authorization_status={data.get('authorization_status')}"
    if name == "check_rules":
        return f"status={data.get('status', output.get('status'))}; missing_fields={data.get('missing_fields', [])}; risks={[risk.get('rule_id') for risk in data.get('risks', [])]}"
    if name == "supplement_material":
        return f"status={output.get('status')}; returned_fields={list(data)}"
    result = output.get("result", output)
    if name == "query_slots":
        return f"scheduling={result.get('scheduling', {}).get('status')}; candidates={[item.get('start_at') for item in (output.get('candidates') or {}).get('candidates', [])]}"
    if name == "save_draft":
        persistence = result.get("persistence", {})
        return f"persistence={persistence.get('status')}; draft_id={persistence.get('draft_id')}"
    return f"status={output.get('status')}; error={(output.get('error') or {}).get('code')}"


def execution_records(model_steps, tools, final_status):
    records = []
    for index, receipt in enumerate(tools, start=1):
        identity = receipt["tool_call_id"]
        selected = next((step for step in model_steps if any(
            call.get("id") == identity for call in step.get("tool_calls", [])
        )), {})
        observed = next((step for step in model_steps if
                         (step.get("observation") or {}).get("tool_call_id") == identity), None)
        calls = observed.get("tool_calls", []) if observed else []
        if observed is not None:
            next_action = ", ".join(call["name"] for call in calls) or (
                "finish" if observed.get("status") == "ok" else "model_error")
            next_reason = "; ".join(call["args"].get("reason", "") for call in calls) if calls else observed.get("content", "")
        else:
            next_action, next_reason = final_status, ""
        records.append({
            "step": index, "model_step": selected.get("step"), "tool": receipt["name"],
            "tool_call_id": identity, "arguments": receipt["arguments"],
            "return_summary": return_summary(receipt["name"], receipt["output"]),
            "decision_reason": receipt["arguments"].get("reason", ""),
            "next_action": next_action, "next_action_source": "model" if observed is not None else "workflow",
            "next_decision_reason": next_reason,
        })
    return records
