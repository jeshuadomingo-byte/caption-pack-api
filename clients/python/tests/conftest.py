"""Shared fixtures for the captionpack test suite.

The live integration tests start a real copy of the Caption-Pack API on a
local port. They are skipped automatically when the API source is not
available (set CAPTIONPACK_TEST_API_DIR to point at it).
"""

import os
import shutil
import socket
import subprocess
import time

import pytest
import requests

API_DIR = os.environ.get(
    "CAPTIONPACK_TEST_API_DIR",
    os.path.expanduser(
        "~/workspace/goals/build-my-professional-brand-and-network/"
        "files/faucet-setup/caption-pack-api"
    ),
)
TEST_PORT = 8471
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"


def _port_open() -> bool:
    sock = socket.socket()
    sock.settimeout(0.5)
    try:
        sock.connect(("127.0.0.1", TEST_PORT))
        return True
    except OSError:
        return False
    finally:
        sock.close()


@pytest.fixture(scope="session")
def live_server():
    """Run the real API locally; restore credits.db afterwards."""
    if not os.path.exists(os.path.join(API_DIR, "app.py")):
        pytest.skip("API source not available (set CAPTIONPACK_TEST_API_DIR)")
    venv_python = os.path.join(API_DIR, ".venv", "bin", "python")
    if not os.path.exists(venv_python):
        pytest.skip("API venv not available")

    db_path = os.path.join(API_DIR, "credits.db")
    backup_path = db_path + ".testbak"
    if os.path.exists(db_path):
        shutil.copy2(db_path, backup_path)

    proc = subprocess.Popen(
        [venv_python, "-m", "uvicorn", "app:app", "--port", str(TEST_PORT)],
        cwd=API_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 30
        ready = False
        while time.time() < deadline:
            if _port_open():
                try:
                    r = requests.get(
                        f"{BASE_URL}/v1/balance",
                        headers={"Authorization": "Bearer bad-key"},
                        timeout=3,
                    )
                    if r.status_code == 401:
                        ready = True
                        break
                except requests.RequestException:
                    pass
            time.sleep(0.3)
        if not ready:
            pytest.skip("local API server did not start")
        yield BASE_URL
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if os.path.exists(backup_path):
            shutil.move(backup_path, db_path)


@pytest.fixture(scope="session")
def venv_python():
    return os.path.join(API_DIR, ".venv", "bin", "python")


def mint_key(venv_python: str, credits: int) -> str:
    """Mint a test key against the LOCAL api (never production)."""
    out = subprocess.run(
        [venv_python, "make_key.py", str(credits)],
        cwd=API_DIR,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip().splitlines()[0]
