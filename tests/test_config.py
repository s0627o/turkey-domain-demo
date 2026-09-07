from __future__ import annotations

import pytest

from turkey_domain_replacer.config import Settings


def test_defaults_are_local_dry_run_and_use_independent_port(tmp_path):
    settings = Settings.from_env({}, base_dir=tmp_path)

    assert settings.execution_mode == "dry-run"
    assert settings.bind_host == "127.0.0.1"
    assert settings.port == 20203
    assert settings.database_path == tmp_path / "var" / "turkey-domain-replacer.db"
    assert settings.cookie_secure is False


def test_production_mode_requires_explicit_approval_identifier(tmp_path):
    with pytest.raises(ValueError, match="DEPLOYMENT_APPROVAL_ID"):
        Settings.from_env(
            {
                "TDR_EXECUTION_MODE": "production",
                "TDR_PUBLIC_ORIGIN": "https://domain-switch.example.com",
            },
            base_dir=tmp_path,
        )


def test_production_mode_requires_https_origin(tmp_path):
    with pytest.raises(ValueError, match="HTTPS"):
        Settings.from_env(
            {
                "TDR_EXECUTION_MODE": "production",
                "TDR_DEPLOYMENT_APPROVAL_ID": "approved-change-123",
                "TDR_PUBLIC_ORIGIN": "http://domain-switch.example.com",
            },
            base_dir=tmp_path,
        )


def test_allowlist_is_normalized_but_still_exact(tmp_path):
    settings = Settings.from_env(
        {"TDR_ALLOWED_EMAILS": " Alice@Example.com,agent@example.net "},
        base_dir=tmp_path,
    )

    assert settings.allowed_emails == frozenset({"alice@example.com", "agent@example.net"})
    assert "example.com" not in settings.allowed_emails


def test_production_mode_rejects_an_empty_email_allowlist(tmp_path):
    with pytest.raises(ValueError, match="TDR_ALLOWED_EMAILS"):
        Settings.from_env(
            {
                "TDR_EXECUTION_MODE": "production",
                "TDR_DEPLOYMENT_APPROVAL_ID": "approved-change-123",
                "TDR_PUBLIC_ORIGIN": "https://domain-switch.example.com",
            },
            base_dir=tmp_path,
        )


def test_production_mode_is_unavailable_until_runtime_composition_is_approved(tmp_path):
    with pytest.raises(ValueError, match="production runtime composition is not implemented"):
        Settings.from_env(
            {
                "TDR_EXECUTION_MODE": "production",
                "TDR_DEPLOYMENT_APPROVAL_ID": "approved-change-123",
                "TDR_PUBLIC_ORIGIN": "https://domain-switch.example.com",
                "TDR_ALLOWED_EMAILS": "agent@example.com",
            },
            base_dir=tmp_path,
        )
