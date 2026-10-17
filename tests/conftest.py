import pytest

# A fixed pseudonym key, so study IDs are the same on every test run and no key file is
# written under data/secrets by tests (D30).
TEST_KEY = "00" * 31 + "01"


@pytest.fixture(autouse=True)
def pseudonym_key(monkeypatch: pytest.MonkeyPatch) -> bytes:
    monkeypatch.setenv("PPBP_PSEUDONYM_KEY", TEST_KEY)
    return bytes.fromhex(TEST_KEY)
