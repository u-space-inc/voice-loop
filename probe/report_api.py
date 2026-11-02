import subprocess

from fastapi import FastAPI

app = FastAPI()


@app.get("/report")
def report(name: str) -> str:
    return subprocess.run(
        f"cat reports/{name}",
        shell=True,
        capture_output=True,
        text=True,
    ).stdout
