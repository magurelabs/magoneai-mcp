"""Text to SQL with the caller's own model, built for one call and discarded.

v1 read one `OPENAI_API_KEY` from the server's environment and spent it on
every caller's questions. Here the key comes with the call that needs it
(`X-Llm-Api-Key`), optionally with an OpenAI-compatible endpoint
(`X-Llm-Base-Url`) and a model (`X-Llm-Model`). The client holding it is built
inside the call and dropped when the call returns; nothing caches it.

The prompting is vanna's own (`vanna.legacy`, pinned at the version v1 ran):
the same system prompt, the same few-shot layout, the same SQL extraction.
Only retrieval is replaced, by the per-call store in `_store.py`.
"""

from __future__ import annotations

from typing import Any

from magone_mcp_core import InvalidURL, SSRFBlocked, validate_url

from . import _store

LLM_CREDENTIAL_HEADERS = ["X-Llm-Api-Key", "X-Llm-Base-Url", "X-Llm-Model"]
DEFAULT_MODEL = "gpt-4o-mini"
LLM_TIMEOUT_S = 60

NOT_ALLOWED_TO_SEE_DATA = "The LLM is not allowed to see the data in your database"


class LlmProblem(ValueError):
    """The request does not carry a usable model configuration."""


def llm_settings(credentials: dict[str, str]) -> dict[str, str | None]:
    api_key = (credentials.get("llm_api_key") or "").strip()
    if not api_key:
        raise LlmProblem(
            "ask_database needs your model's API key in X-Llm-Api-Key; this server holds none"
        )
    base_url = (credentials.get("llm_base_url") or "").strip() or None
    if base_url:
        try:
            # The endpoint is fetched by this server, so it gets the same SSRF
            # check every other server-side fetch in this repository gets.
            validate_url(base_url)
        except (InvalidURL, SSRFBlocked) as exc:
            raise LlmProblem(f"X-Llm-Base-Url refused: {exc}") from None
    model = (credentials.get("llm_model") or "").strip() or DEFAULT_MODEL
    return {"api_key": api_key, "base_url": base_url, "model": model}


def build_vanna(settings: dict[str, str | None], items: list[dict[str, Any]]) -> Any:
    """A vanna instance over this call's training items and this call's model."""
    from openai import OpenAI
    from vanna.legacy.openai import OpenAI_Chat

    class RequestVanna(OpenAI_Chat):
        """vanna's prompting over an in-memory, per-call training set."""

        def __init__(self, client: Any, config: dict[str, Any]) -> None:
            OpenAI_Chat.__init__(self, client=client, config=config)
            self._items = items

        def log(self, message: str, title: str = "Info") -> None:
            # vanna prints the whole prompt, schema and data included, to
            # stdout. A connector's logs must not carry the caller's data.
            return None

        def _of(self, kind: str) -> list[dict[str, Any]]:
            return [i for i in self._items if i["kind"] == kind]

        def get_similar_question_sql(self, question: str, **kwargs: Any) -> list:
            ranked = _store.rank(question, self._of("sql"))
            return [{"question": i["question"], "sql": i["content"]} for i in ranked]

        def get_related_ddl(self, question: str, **kwargs: Any) -> list:
            return [i["content"] for i in _store.rank(question, self._of("ddl"))]

        def get_related_documentation(self, question: str, **kwargs: Any) -> list:
            return [i["content"] for i in _store.rank(question, self._of("documentation"))]

        def generate_embedding(self, data: str, **kwargs: Any) -> list[float]:
            raise NotImplementedError("retrieval here is lexical; nothing is embedded")

        def add_question_sql(self, question: str, sql: str, **kwargs: Any) -> str:
            raise NotImplementedError("training is written to the caller's database")

        def add_ddl(self, ddl: str, **kwargs: Any) -> str:
            raise NotImplementedError("training is written to the caller's database")

        def add_documentation(self, documentation: str, **kwargs: Any) -> str:
            raise NotImplementedError("training is written to the caller's database")

        def get_training_data(self, **kwargs: Any) -> Any:
            return [_store.as_training_data(i) for i in self._items]

        def remove_training_data(self, id: str, **kwargs: Any) -> bool:  # noqa: A002
            raise NotImplementedError("training is removed from the caller's database")

    client = OpenAI(
        api_key=settings["api_key"],
        base_url=settings["base_url"],
        timeout=LLM_TIMEOUT_S,
        max_retries=1,
    )
    return RequestVanna(client=client, config={"model": settings["model"]})
