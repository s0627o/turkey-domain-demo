from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    execution_mode: str
    bind_host: str
    port: int
    database_path: Path
    cookie_secure: bool
    allowed_emails: frozenset[str]
    public_origin: str | None
    deployment_approval_id: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, base_dir: Path) -> Settings:
        execution_mode = env.get("TDR_EXECUTION_MODE", "dry-run").strip().lower()
        if execution_mode not in {"dry-run", "production"}:
            raise ValueError("TDR_EXECUTION_MODE must be dry-run or production")

        approval_id = env.get("TDR_DEPLOYMENT_APPROVAL_ID", "").strip() or None
        public_origin = env.get("TDR_PUBLIC_ORIGIN", "").strip().rstrip("/") or None
        if execution_mode == "production":
            if approval_id is None:
                raise ValueError("TDR_DEPLOYMENT_APPROVAL_ID is required in production")
            if public_origin is None or urlparse(public_origin).scheme != "https":
                raise ValueError("production TDR_PUBLIC_ORIGIN must use HTTPS")

        raw_emails = env.get("TDR_ALLOWED_EMAILS", "")
        allowed_emails = frozenset(
            email.strip().lower() for email in raw_emails.split(",") if email.strip()
        )
        if execution_mode == "production" and not allowed_emails:
            raise ValueError("TDR_ALLOWED_EMAILS must contain at least one exact email")
        if execution_mode == "production":
            raise ValueError("production runtime composition is not implemented")
        database_path = Path(
            env.get("TDR_DATABASE_PATH", str(base_dir / "var" / "turkey-domain-replacer.db"))
        )
        return cls(
            execution_mode=execution_mode,
            bind_host=env.get("TDR_BIND_HOST", "127.0.0.1"),
            port=int(env.get("TDR_PORT", "20203")),
            database_path=database_path,
            cookie_secure=execution_mode == "production",
            allowed_emails=allowed_emails,
            public_origin=public_origin,
            deployment_approval_id=approval_id,
        )
