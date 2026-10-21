"""Mã hóa token Kaggle bằng Fernet. Khóa lấy từ biến môi trường SECRET_KEY."""
import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    key = os.environ.get("SECRET_KEY")
    if not key:
        raise RuntimeError(
            "Thiếu SECRET_KEY. Tạo bằng: python -c "
            "\"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode())


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("Không giải mã được token (SECRET_KEY đã đổi?)") from e
