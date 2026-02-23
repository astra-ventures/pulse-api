"""pytest configuration for Pulse API tests.

Provides a test client with isolated PULSE_DATA_DIR so tests
never touch real companion state at /Users/iris/.pulse/companions.
"""
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


TEST_API_KEY = "test-key-abc123"


@pytest.fixture(scope="session", autouse=True)
def isolated_pulse_data_dir(tmp_path_factory):
    """Point PULSE_DATA_DIR at a temp directory for the whole test session."""
    tmp = tmp_path_factory.mktemp("pulse-companions")
    os.environ["PULSE_DATA_DIR"] = str(tmp)
    os.environ["PULSE_API_KEY"] = TEST_API_KEY
    yield tmp
    # Cleanup happens automatically via tmp_path_factory


@pytest.fixture(scope="session")
def client(isolated_pulse_data_dir):
    """Session-scoped FastAPI test client."""
    # Import after env vars are set so module-level constants pick them up
    import importlib
    import main as main_module
    importlib.reload(main_module)
    from main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def auth_headers():
    return {"X-Pulse-Key": TEST_API_KEY}


@pytest.fixture(scope="session")
def test_companion_id():
    return "test-companion-001"
