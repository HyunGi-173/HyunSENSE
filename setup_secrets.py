"""Generate the values for .streamlit/secrets.toml. Run locally; never commit secrets."""
from getpass import getpass
from cryptography.fernet import Fernet
from security import hash_password

if __name__ == '__main__':
    password = getpass('12자 이상의 앱 접속 비밀번호: ')
    password_hash = hash_password(password)
    key = Fernet.generate_key().decode('ascii')
    print('\n다음 두 줄을 Streamlit Secrets에 복사하세요. 비밀번호는 안전한 곳에 보관하세요.\n')
    print(f'ACCESS_PASSWORD_HASH = "{password_hash}"')
    print(f'DATA_ENCRYPTION_KEY = "{key}"')
    print('\n선택: DATABASE_URL = "PostgreSQL 연결 문자열" # 외부 영구 저장소가 있을 때만\n')
