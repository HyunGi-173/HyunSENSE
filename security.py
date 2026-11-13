"""Single-user password hashing and encrypted JSON payloads."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet

ITERATIONS = 600_000


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def hash_password(password: str) -> str:
    if len(password) < 12 or len(password.encode('utf-8')) > 4096:
        raise ValueError('비밀번호는 12자 이상, UTF-8 기준 4096바이트 이하여야 합니다.')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, ITERATIONS)
    return f'pbkdf2_sha256${ITERATIONS}${_b64(salt)}${_b64(digest)}'


def verify_password(password: str, encoded: str) -> bool:
    try:
        if not isinstance(password, str) or len(password.encode('utf-8')) > 4096:
            return False
        algorithm, it, salt, digest = encoded.split('$')
        n = int(it)
        if algorithm != 'pbkdf2_sha256' or not 200_000 <= n <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), _unb64(salt), n)
        return hmac.compare_digest(actual, _unb64(digest))
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        return False


def encrypt_json(raw: bytes, key: str) -> bytes:
    return Fernet(key.encode('utf-8')).encrypt(raw)


def decrypt_json(token: bytes, key: str) -> bytes:
    return Fernet(key.encode('utf-8')).decrypt(token)


# Portable, password-encrypted backup. The local file contains no plain health data.
BACKUP_PREFIX = b"HYUNSENSE-BACKUP-1\n"
BACKUP_ITERATIONS = 480_000


def _backup_fernet(passphrase: str, salt: bytes) -> Fernet:
    if len(passphrase) < 12:
        raise ValueError('백업 암호는 최소 12자 이상이어야 합니다.')
    material = hashlib.pbkdf2_hmac('sha256', passphrase.encode('utf-8'), salt, BACKUP_ITERATIONS)
    return Fernet(base64.urlsafe_b64encode(material))


def encrypt_backup(raw: bytes, passphrase: str) -> bytes:
    salt = secrets.token_bytes(16)
    return BACKUP_PREFIX + base64.urlsafe_b64encode(salt + _backup_fernet(passphrase, salt).encrypt(raw))


def decrypt_backup(blob: bytes, passphrase: str) -> bytes:
    if len(blob) > 3_000_000 or not blob.startswith(BACKUP_PREFIX):
        raise ValueError('HyunSENSE 암호화 백업 파일이 아닙니다.')
    try:
        payload = base64.urlsafe_b64decode(blob[len(BACKUP_PREFIX):])
        return _backup_fernet(passphrase, payload[:16]).decrypt(payload[16:])
    except Exception as exc:
        raise ValueError('백업 암호가 틀렸거나 파일이 손상되었습니다.') from exc
