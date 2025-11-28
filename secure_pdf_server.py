import time
import hashlib
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

app = FastAPI()

# Место, где PDF реально лежит на сервере (локальный путь на VPS)
PDF_PATH = "/home/dmitry/calmwaybot_test/protected/guide.pdf"

# Соль для токена (любой рандом, не меняй после создания)
SECRET = "ajd82jhAHD828hd82hds9"

# Токены живут 10 минут
TOKEN_TTL = 600


def generate_token(user_id: int) -> str:
    expires = int(time.time()) + TOKEN_TTL
    raw = f"{user_id}:{expires}:{SECRET}"
    token = hashlib.sha256(raw.encode()).hexdigest()
    return f"{token}:{expires}"


def verify_token(token: str) -> bool:
    try:
        hash_part, expires, user_id = token.split(":")
        expires = int(expires)
        user_id = int(user_id)

        if expires < time.time():
            return False

        raw = f"{user_id}:{expires}:{SECRET}".encode()
        expected = hashlib.sha256(raw).hexdigest()

        return expected == hash_part

    except:
        return False



@app.get("/secure-pdf")
def get_secure_pdf(token: str):
    if not verify_token(token):
        raise HTTPException(status_code=403, detail="Invalid or expired token")

    return FileResponse(PDF_PATH, media_type="application/pdf")
