"""Explicit offline test model using the same LangChain message/tool interface."""

import json

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableLambda


class OfflineTestModel:
    mode = "offline_test"

    def bind_tools(self, tools):
        return RunnableLambda(self.invoke)

    def invoke(self, messages):
        observations = [message for message in messages if isinstance(message, ToolMessage)]
        request = json.loads(messages[1].content)["request"]
        if not observations:
            return self.call("read_material", len(messages))
        last = observations[-1]
        if last.status == "error":
            return self.stop()
        name, output = last.name, json.loads(last.content)
        if output.get("status") in {"error", "not_found"}:
            return self.stop()
        if name == "read_material":
            return self.call("check_rules", len(messages))
        if name == "supplement_material":
            return self.call("check_rules", len(messages)) if output["status"] == "ok" else self.stop()
        if name == "check_rules":
            review = output["data"]
            if review["status"] == "needs_info":
                if any(item.name == "supplement_material" for item in observations):
                    return self.stop()
                return self.call("supplement_material", len(messages), {"fields": review["missing_fields"]})
            if review["status"] == "passed":
                return self.call("query_slots", len(messages))
            if review["status"] == "needs_review" and request["save_draft"]:
                return self.call("save_draft", len(messages))
            return self.stop()
        if name == "query_slots" and request["save_draft"]:
            return self.call("save_draft", len(messages))
        return self.stop()

    @staticmethod
    def call(name, number, arguments=None):
        arguments = {"reason": "离线测试动作说明，不代表真实模型决策。", **(arguments or {})}
        return AIMessage(content="", tool_calls=[{
            "id": f"offline-{number}", "name": name, "args": arguments, "type": "tool_call",
        }])

    @staticmethod
    def stop():
        return AIMessage(content="离线测试分支结束。")
