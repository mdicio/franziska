"""A simulated week (evals/week.py) on the real local model: every message, job and tap judged on state.

Opt-in, ~10-15 min, holds the GPU: uv run --env-file .env pytest -m week -s
"""

import pytest

from evals import week


@pytest.mark.week
def test_week():
    rows = week.run()
    unexpected = [r for r in rows if not r["ok"] and not r["known"]]
    assert not unexpected, "\n" + week.failures(unexpected)
