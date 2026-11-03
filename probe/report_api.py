import re
from pathlib import Path

from fastapi import FastAPI, HTTPException

app = FastAPI()

REPORTS_DIR = Path("reports").resolve()
REPORT_NAME = re.compile(r"[A-Za-z0-9_-]+\.txt")


@app.get("/report")
def report(name: str) -> str:
    if not REPORT_NAME.fullmatch(name):
        raise HTTPException(status_code=400)
    path = REPORTS_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404)
    return path.read_text()
