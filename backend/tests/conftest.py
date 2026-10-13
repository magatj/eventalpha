import json
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
os.environ.setdefault("SEC_USER_AGENT", "EventAlpha-tests tests@example.com")


def load_fixture(name: str):
    p = FIXTURES / name
    return json.loads(p.read_text()) if p.suffix == ".json" else p.read_text()


@pytest.fixture
def fixture():
    return load_fixture
