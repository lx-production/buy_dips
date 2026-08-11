from __future__ import annotations

import getpass
import json
import os
import stat
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3


class WalletError(RuntimeError):
    """Raised when a local keystore operation cannot be completed safely."""


def resolve_keystore_password(
    password_env: str = "KEYSTORE_PASSWORD",
    *,
    confirm: bool = False,
    environ: Mapping[str, str] | None = None,
    interactive: bool | None = None,
    prompt: Callable[[str], str] | None = None,
) -> str:
    # Resolve development secrets from the environment first and prompt only on a real terminal.
    source = os.environ if environ is None else environ
    password = source.get(password_env)
    if password:
        return password
    can_prompt = sys.stdin.isatty() if interactive is None else interactive
    if not can_prompt:
        raise WalletError(f"Keystore password is unavailable; set {password_env} or run interactively")
    prompt_fn = getpass.getpass if prompt is None else prompt
    password = prompt_fn("Keystore password: ")
    if not password:
        raise WalletError("Keystore password must not be empty")
    if confirm and prompt_fn("Confirm keystore password: ") != password:
        raise WalletError("Keystore password confirmation does not match")
    return password


def create_encrypted_keystore(path: str | Path, password: str) -> str:
    # Create a new account and durably replace a private temporary file without exposing its key.
    if not password:
        raise WalletError("Keystore password must not be empty")
    target = _absolute_path(path)
    if os.path.lexists(target):
        raise WalletError(f"Keystore already exists: {target}")
    parent = target.parent
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise WalletError(f"Keystore parent must be a real directory: {parent}")
    if stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise WalletError(f"Keystore parent permissions must not allow group or other access: {parent}")

    account = Account.create()
    encrypted = Account.encrypt(account.key, password)
    payload = json.dumps(encrypted, separators=(",", ":"))
    temporary = parent / f".{target.name}.{os.getpid()}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if os.path.lexists(temporary):
            temporary.unlink()
        raise
    return Web3.to_checksum_address(account.address)


def validate_keystore_permissions(path: str | Path) -> Path:
    # Refuse links, non-regular files, and any keystore readable or writable by group/others.
    target = _absolute_path(path)
    try:
        metadata = target.lstat()
    except FileNotFoundError as exc:
        raise WalletError(f"Keystore does not exist: {target}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise WalletError("Keystore must be a regular file and must not be a symlink")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise WalletError("Keystore permissions must be 0600 or stricter")
    return target


def read_keystore_address(path: str | Path) -> str:
    # Read and validate only the public address field from a permission-safe encrypted keystore.
    payload = _read_keystore_payload(path)
    address = payload.get("address")
    if not isinstance(address, str):
        raise WalletError("Keystore is missing its public address")
    candidate = address if address.startswith("0x") else f"0x{address}"
    if not Web3.is_address(candidate):
        raise WalletError("Keystore contains an invalid public address")
    return Web3.to_checksum_address(candidate)


def load_local_account(
    path: str | Path,
    password: str,
    *,
    expected_address: str | None = None,
) -> LocalAccount:
    # Decrypt locally and ensure both the embedded and configured public addresses match the key.
    if not password:
        raise WalletError("Keystore password must not be empty")
    payload = _read_keystore_payload(path)
    embedded_address = payload.get("address")
    if not isinstance(embedded_address, str):
        raise WalletError("Keystore is missing its public address")
    try:
        private_key = Account.decrypt(payload, password)
        account = Account.from_key(private_key)
    except Exception as exc:
        raise WalletError("Keystore decryption failed") from exc
    decrypted_address = Web3.to_checksum_address(account.address)
    embedded_candidate = embedded_address if embedded_address.startswith("0x") else f"0x{embedded_address}"
    if not Web3.is_address(embedded_candidate):
        raise WalletError("Keystore contains an invalid public address")
    if Web3.to_checksum_address(embedded_candidate) != decrypted_address:
        raise WalletError("Keystore public address does not match its encrypted key")
    if expected_address is not None:
        if not Web3.is_address(expected_address):
            raise WalletError("Configured expected wallet address is invalid")
        if Web3.to_checksum_address(expected_address) != decrypted_address:
            raise WalletError("Decrypted wallet does not match wallet.expected_address")
    return account


def _read_keystore_payload(path: str | Path) -> dict[str, Any]:
    # Parse a validated keystore while keeping its encrypted content internal to this module.
    target = validate_keystore_permissions(path)
    try:
        with target.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise WalletError("Keystore is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise WalletError("Keystore JSON must contain an object")
    return payload


def _absolute_path(path: str | Path) -> Path:
    # Make a stable absolute path without resolving a final symlink before lstat can reject it.
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))
