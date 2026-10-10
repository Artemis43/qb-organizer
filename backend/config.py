# qb-organizer/backend/config.py
"""Central configuration loaded from .env with sensible defaults."""

from pydantic_settings import BaseSettings
from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    # --- Claude API ---
    anthropic_api_key: str = ""
    budget_limit: float = 25.0
    haiku_model: str = "claude-haiku-4-5-20251001"
    sonnet_model: str = "claude-sonnet-4-6-20250610"

    # --- Cost Ceiling (Medical-Grade Answer Pipeline R9) ---
    batch_cost_ceiling_usd: float = 0.50
    daily_cost_ceiling_usd: float = 5.00

    # --- Embedding ---
    embedding_model: str = "pritamdeka/PubMedBERT-mnli-snli-scinli-scitail-mednli-stsb"

    # --- Chunking ---
    chunk_size: int = 500  # tokens
    chunk_overlap: int = 100  # tokens

    # --- Confidence ---
    confidence_high: float = 0.80
    confidence_low: float = 0.50

    # --- Server ---
    backend_port: int = 8000
    frontend_url: str = "http://localhost:3000"

    # --- Answer Generation ---
    default_answer_mode: str = "auto"
    default_answer_preset: str = "SAQ"
    answer_temperature: float = 0.3

    # --- Knowledge Graph ---
    kg_extraction_model: str = "haiku"
    kg_max_concepts_per_batch: int = 50
    kg_enable_relation_extraction: bool = True
    kg_default_limit: int = 150

    # --- Export / Deploy ---
    fs_collection_prefix: str = ""
    imagekit_folder: str = "/qb-organizer"

    # --- Paths ---
    data_dir: Path = DATA_DIR
    chroma_dir: Path = DATA_DIR / "chroma_db"
    textbooks_dir: Path = DATA_DIR / "textbooks"
    qp_dir: Path = DATA_DIR / "question_papers"
    checkpoints_dir: Path = DATA_DIR / "checkpoints"
    exports_dir: Path = DATA_DIR / "exports"
    logs_dir: Path = DATA_DIR / "logs"
    db_path: Path = DATA_DIR / "qb_organizer.sqlite"

    # --- Firebase ---
    firebase_service_account_path: str = str(DATA_DIR / "firebase-service-account.json")

    # --- ImageKit ---
    imagekit_public_key: str = "public_smY90of9evV8XSk1nVYxbEbk18M="
    imagekit_private_key: str = ""  # <-- Added this line to satisfy Pydantic
    imagekit_auth_url: str = "https://imagekit-auth-service.onrender.com/api/imagekit-auth"
    imagekit_url_endpoint: str = "https://ik.imagekit.io/lohithcodes"
    imagekit_upload_folder: str = "/answer_images"

    # --- Rate Limiting ---
    max_retries: int = 5
    retry_base_delay: float = 1.0  # seconds
    retry_max_delay: float = 60.0  # seconds
    circuit_breaker_threshold: int = 5
    circuit_breaker_recovery: int = 300  # seconds

    # --- Batch API ---
    batch_poll_interval: int = 30  # seconds

    class Config:
        env_file = str(BASE_DIR / ".env")
        env_file_encoding = "utf-8"
        # Optional: extra = "ignore" allows unmapped .env variables, but explicitly mapping them is better.


settings = Settings()

# Ensure all data directories exist
for d in [
    settings.data_dir, settings.chroma_dir, settings.textbooks_dir,
    settings.qp_dir, settings.checkpoints_dir, settings.exports_dir,
    settings.logs_dir
]:
    d.mkdir(parents=True, exist_ok=True)