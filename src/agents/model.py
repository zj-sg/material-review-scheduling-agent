"""Configured LangChain chat models; tool binding is handled by the integration."""

from langchain_deepseek import ChatDeepSeek
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from src.config import Settings


class DeepSeekChatModel(ChatDeepSeek):
    """Keep DeepSeek reasoning fields when replaying tool-call turns.

    The integration extracts this field but currently omits it on replay.
    Tool schemas, request execution and tool-call parsing stay with LangChain.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        for message, encoded in zip(self._convert_input(input_).to_messages(), payload["messages"], strict=True):
            if isinstance(message, AIMessage) and "reasoning_content" in message.additional_kwargs:
                encoded["reasoning_content"] = message.additional_kwargs["reasoning_content"]
        return payload


class CompatibleChatModel:
    mode = "api"

    def __init__(self, chat_model):
        self.chat_model = chat_model

    def bind_tools(self, tools):
        return self.chat_model.bind_tools(tools, tool_choice="auto", parallel_tool_calls=False)

    def close(self) -> None:
        self.chat_model.root_client.close()


def build_model(settings: Settings) -> CompatibleChatModel:
    missing = [
        name for name, value in (
            ("MATERIAL_AGENT_MODEL", settings.model),
            ("MATERIAL_AGENT_BASE_URL", settings.base_url),
            ("MATERIAL_AGENT_API_KEY", settings.api_key),
        ) if value is None
    ]
    if missing:
        raise ValueError("请在项目 .env 配置：" + "、".join(missing) + "；API Key 只在本地填写。")
    # DeepSeek's integration preserves reasoning_content across tool-call turns.
    adapter = DeepSeekChatModel if settings.model.startswith("deepseek-") or settings.base_url.host == "api.deepseek.com" else ChatOpenAI
    chat_model = adapter(
        model=settings.model, api_key=settings.api_key, base_url=str(settings.base_url),
        timeout=30, max_retries=1,
    )
    return CompatibleChatModel(chat_model)
