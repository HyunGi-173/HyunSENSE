import os
import tempfile

from cryptography.fernet import Fernet
from domain import sample_data
from security import hash_password, verify_password
from store import SecureStore


def test_hash_verify_roundtrip():
    encoded = hash_password("this-is-a-strong-passphrase")
    assert verify_password("this-is-a-strong-passphrase", encoded)
    assert not verify_password("this-is-the-wrong-passphrase", encoded)
    assert not verify_password("x", "invalidhash")


def test_encrypted_sqlite_roundtrip():
    with tempfile.TemporaryDirectory() as directory:
        key = Fernet.generate_key().decode()
        path = os.path.join(directory, "records.sqlite3")
        s = SecureStore(key, sqlite_path=path)
        assert s.load() is None
        d = sample_data()
        d["profile"]["partner_name"] = "암호화 검사 일상"
        s.save(d)
        assert s.load() == d
        contents = open(path, "rb").read()
        assert "암호화 검사 일상".encode() not in contents
        s.delete()
        assert s.load() is None
