from __future__ import annotations

import json
import stat

import pytest

from src.trading.wallet import (
    WalletError,
    create_encrypted_keystore,
    load_local_account,
    read_keystore_address,
    resolve_keystore_password,
)


def test_create_keystore_is_0600_and_decrypts_to_returned_address(tmp_path) -> None:
    # Prove the atomic creation path returns only the matching public account identity.
    path = tmp_path / "wallet" / "trader-dev.json"

    address = create_encrypted_keystore(path, "test-password")
    account = load_local_account(path, "test-password", expected_address=address)

    assert account.address == address
    assert read_keystore_address(path) == address
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_create_keystore_refuses_overwrite(tmp_path) -> None:
    # Preserve an existing encrypted wallet rather than silently replacing its private key.
    path = tmp_path / "wallet" / "trader-dev.json"
    create_encrypted_keystore(path, "test-password")

    with pytest.raises(WalletError, match="already exists"):
        create_encrypted_keystore(path, "another-password")


def test_load_keystore_rejects_wrong_password_and_expected_address(tmp_path) -> None:
    # Fail closed for both decryption failure and a wallet pin that does not match the key.
    path = tmp_path / "wallet" / "trader-dev.json"
    create_encrypted_keystore(path, "correct-password")

    with pytest.raises(WalletError, match="decryption failed"):
        load_local_account(path, "wrong-password")
    with pytest.raises(WalletError, match="expected_address"):
        load_local_account(path, "correct-password", expected_address="0x0000000000000000000000000000000000000001")


def test_load_keystore_rejects_tampered_embedded_address(tmp_path) -> None:
    # Detect metadata tampering even when the encrypted key and password remain otherwise valid.
    path = tmp_path / "wallet" / "trader-dev.json"
    create_encrypted_keystore(path, "correct-password")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["address"] = "0000000000000000000000000000000000000001"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(WalletError, match="does not match"):
        load_local_account(path, "correct-password")


def test_load_keystore_rejects_broad_permissions_and_symlink(tmp_path) -> None:
    # Block filesystem aliases and group-readable files before parsing encrypted JSON.
    path = tmp_path / "wallet" / "trader-dev.json"
    create_encrypted_keystore(path, "test-password")
    path.chmod(0o640)
    with pytest.raises(WalletError, match="0600"):
        load_local_account(path, "test-password")
    path.chmod(0o600)
    link = tmp_path / "wallet-link.json"
    link.symlink_to(path)
    with pytest.raises(WalletError, match="symlink"):
        load_local_account(link, "test-password")


def test_password_resolution_prefers_env_and_confirms_prompt() -> None:
    # Verify the documented env-first order and double-entry behavior for interactive creation.
    prompts: list[str] = []

    def prompt(label: str) -> str:
        # Record prompt labels while returning the same non-secret fixture value twice.
        prompts.append(label)
        return "prompt-password"

    assert resolve_keystore_password(environ={"CUSTOM_PASSWORD": "env-password"}, password_env="CUSTOM_PASSWORD") == "env-password"
    assert resolve_keystore_password(environ={}, interactive=True, confirm=True, prompt=prompt) == "prompt-password"
    assert len(prompts) == 2


def test_password_resolution_fails_noninteractive_and_on_mismatch() -> None:
    # Refuse implicit empty credentials and mismatched password confirmation.
    with pytest.raises(WalletError, match="unavailable"):
        resolve_keystore_password(environ={}, interactive=False)
    answers = iter(["first", "second"])
    with pytest.raises(WalletError, match="does not match"):
        resolve_keystore_password(environ={}, interactive=True, confirm=True, prompt=lambda _label: next(answers))


def test_wallet_failures_do_not_print_password(capsys, tmp_path) -> None:
    # Ensure library failures stay silent and never write supplied secrets to either output stream.
    path = tmp_path / "wallet" / "trader-dev.json"
    create_encrypted_keystore(path, "correct-password")
    with pytest.raises(WalletError):
        load_local_account(path, "super-secret-wrong-password")

    captured = capsys.readouterr()
    assert "super-secret-wrong-password" not in captured.out
    assert "super-secret-wrong-password" not in captured.err
