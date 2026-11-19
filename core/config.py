"""Central settings, read from environment variables and .env."""
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

# Load .env into os.environ too, so LangSmith picks up its LANGSMITH_* variables.
load_dotenv(ROOT / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # --- OpenAI ---
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1"
    openai_mini_model: str = "gpt-4.1-mini"
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = 3072
    llm_temperature: float | None = 0

    # --- Azure OpenAI (optional) ---
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_chat_deployment: str = ""
    azure_openai_mini_deployment: str = ""
    azure_openai_embedding_deployment: str = ""

    # --- Azure AI Search ---
    azure_search_endpoint: str = ""
    azure_search_key: str = ""
    azure_search_index: str = "claimpilot-policies"
    semantic_config: str = "claimpilot-semantic"

    # --- Workflow rules ---
    hitl_payout_threshold: float = 10_000
    fraud_review_threshold: float = 0.7
    eval_threshold: float = 0.8
    max_retries: int = 2
    retrieval_top_k: int = 6
    retrieval_candidates: int = 50
    min_reranker_score: float = 1.5
    context_char_budget: int = 24_000  # ~6k tokens

    # --- MCP ---
    claims_mcp_url: str = "http://localhost:8001/mcp"
    policy_mcp_url: str = "http://localhost:8002/mcp"
    fraud_mcp_url: str = "http://localhost:8003/mcp"

    # --- Storage ---
    data_dir: Path = ROOT / "data"
    db_path: Path = ROOT / "data" / "claimpilot.db"
    checkpoint_path: Path = ROOT / "data" / "checkpoints.db"
    upload_dir: Path = ROOT / "data" / "uploads"
    policy_upload_dir: Path = ROOT / "data" / "policy_uploads"

    # --- Admin (static demo login) ---
    admin_username: str = "Admin"
    admin_password: str = "admin"
    admin_token_secret: str = "change-me"
    admin_token_ttl_minutes: int = 480
    policy_max_files: int = 20
    policy_max_mb: int = 25
    policy_auto_index: bool = True     # index uploads automatically + resync on API start

    # --- Policy extraction (images, scanned pages, .doc) ---
    image_describe: bool = True        # describe figures with the vision model (chat deployment)
    image_min_px: int = 120            # smaller images are icons: skipped
    image_max_per_doc: int = 50        # cost guard for vision calls
    scan_ocr: bool = True              # OCR scanned PDF pages with the vision model
    vision_concurrency: int = 4        # parallel vision calls per document
    libreoffice_path: str = "soffice"  # converter for .doc -> .docx

    # --- Chunking (tokens, one config for all formats – see features/SAVE_IN_VECTOR_DB.md §2.2) ---
    chunk_target_tokens: int = 450     # size of parts when a long clause is split
    chunk_max_tokens: int = 700        # clauses above this are split
    chunk_overlap_tokens: int = 60     # overlap between parts of the same clause only
    chunk_min_tokens: int = 40         # smaller last parts are merged into the previous part
    embed_batch_size: int = 64         # texts per embedding request

    # --- Policy chat (features/POLICY_CHAT.md §7) ---
    chat_eval_threshold: float = 0.8           # minimum faithfulness of an answer
    chat_min_relevance: float = 0.7            # minimum answer relevance
    chat_min_citation_coverage: float = 0.7    # minimum share of statements with a citation
    chat_max_retries: int = 2                  # retry / regenerate attempts after a failed evaluation
    chat_top_k: int = 6                        # chunks in the answer context
    chat_history_turns: int = 6                # previous turns used to rewrite follow-up questions
    chat_rate_limit_per_min: int = 20          # messages per session per minute
    confidential_probe_min_score: float = 2.0  # reranker score: "the answer is in a confidential document"
    confidential_dominance_margin: float = 1.0  # confidential score this much above the best public -> restricted
    confidential_message: str = (
        "Some of the information you asked about is in a confidential document, so I can't share it. "
        "Please contact your insurance advisor or claims handler for details.")

    # --- UI ---
    api_url: str = "http://localhost:8000"

    @field_validator("llm_temperature", mode="before")
    @classmethod
    def _empty_temperature(cls, v):
        return None if v in ("", "none", "None") else v

    @property
    def use_azure_openai(self) -> bool:
        return bool(self.azure_openai_endpoint)


@lru_cache
def get_settings() -> Settings:
    return Settings()
