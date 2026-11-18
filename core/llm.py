"""LLM and embedding clients. OpenAI by default, Azure OpenAI when AZURE_OPENAI_ENDPOINT is set."""
from functools import lru_cache

from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings, ChatOpenAI, OpenAIEmbeddings

from core.config import get_settings


@lru_cache
def chat(mini: bool = False):
    """Main model for agents; mini model for guardrails, query rewrite and the judge."""
    s = get_settings()
    extra = {} if s.llm_temperature is None else {"temperature": s.llm_temperature}
    if s.use_azure_openai:
        return AzureChatOpenAI(
            azure_endpoint=s.azure_openai_endpoint,
            api_key=s.azure_openai_api_key,
            api_version=s.azure_openai_api_version,
            azure_deployment=s.azure_openai_mini_deployment if mini else s.azure_openai_chat_deployment,
            **extra,
        )
    return ChatOpenAI(
        model=s.openai_mini_model if mini else s.openai_model,
        api_key=s.openai_api_key or None,
        **extra,
    )


def structured(schema, mini: bool = False):
    """Chat model that returns a validated Pydantic object."""
    return chat(mini).with_structured_output(schema)


@lru_cache
def embedder():
    s = get_settings()
    if s.use_azure_openai:
        return AzureOpenAIEmbeddings(
            azure_endpoint=s.azure_openai_endpoint,
            api_key=s.azure_openai_api_key,
            api_version=s.azure_openai_api_version,
            azure_deployment=s.azure_openai_embedding_deployment,
            dimensions=s.embedding_dimensions,
        )
    return OpenAIEmbeddings(
        model=s.embedding_model,
        dimensions=s.embedding_dimensions,
        api_key=s.openai_api_key or None,
    )
