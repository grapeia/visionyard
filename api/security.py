"""Hash de senha e geração/verificação de tokens.

Regras que valem pra todo segredo daqui:
  * senha → argon2id, com os parâmetros embutidos na string (permite trocar
    de custo/algoritmo sem invalidar login antigo);
  * token (sessão, API, device) → aleatório de 256 bits, e o banco guarda só o
    sha256. Vazamento de dump não vira vazamento de credencial.

Por que sha256 puro no token e argon2 na senha: senha tem entropia baixa e
precisa de KDF caro pra resistir a força bruta offline. Token de 256 bits não
é força-brutável, então o hash rápido basta — e um hash rápido é o que permite
verificar sessão a cada request sem custo.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

# Defaults do argon2-cffi (RFC 9106 low-memory): 64 MiB, t=3, p=4.
_hasher = PasswordHasher()

TOKEN_BYTES = 32          # 256 bits
PREFIX_LEN = 8            # parte visível do token de API, p/ identificar


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Falso quando não há hash (usuário ainda convidado).

    Mesmo sem hash gasta o tempo de uma verificação, pra que 'email não existe'
    e 'senha errada' não sejam distinguíveis pelo tempo de resposta.
    """
    if not stored_hash:
        dummy_verify()
        return False
    try:
        _hasher.verify(stored_hash, password)
        return True
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True quando o hash usa parâmetros antigos — re-hasheia no próximo login."""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


_DUMMY = _hasher.hash("dummy-password-for-timing-equalization")


def dummy_verify() -> None:
    """Queima o mesmo tempo de um verify real (defesa contra enumeração)."""
    try:
        _hasher.verify(_DUMMY, "wrong")
    except VerifyMismatchError:
        pass


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def token_prefix(token: str) -> str:
    return token[:PREFIX_LEN]


def constant_time_eq(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)
