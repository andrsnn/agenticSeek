import os
import importlib

import pytest


@pytest.mark.asyncio
async def test_query_does_not_exit_on_exception(monkeypatch):
    """
    Regression: provider/agent exceptions must not call sys.exit() and kill the server.
    """
    monkeypatch.setenv("AGENTICSEEK_SKIP_INIT", "1")

    api_mod = importlib.import_module("api")
    importlib.reload(api_mod)

    class DummyInteraction:
        def __init__(self):
            self.is_active = True
            self.current_agent = None
            self.last_query = None
            self.last_answer = ""
            self.last_reasoning = ""
            self.last_success = False

        async def think(self):
            raise RuntimeError("boom")

        def speak_answer(self):
            return None

        def save_session(self):
            return None

    api_mod.interaction = DummyInteraction()

    class DummyReq:
        def __init__(self, query: str):
            self.query = query

    # If process_query still called sys.exit, pytest would error out.
    resp = await api_mod.process_query(DummyReq("hello"))
    assert resp.status_code in (400, 500)
