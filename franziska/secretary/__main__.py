"""CLI: `python -m franziska.secretary "remind me to call the dentist tomorrow at 10"`.

`python -m franziska.secretary --brief` prints the morning brief (no LLM).
"""

import sys

from franziska import guards
from franziska.config import Settings
from franziska.llm import USAGE_LIMITS, build_model
from franziska.secretary.agent import agent
from franziska.secretary.service import Life


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    settings = Settings.from_env()
    life = Life.from_settings(settings)
    if argv == ["--brief"]:
        print(life.brief())
        return 0
    print(guards.run_sync(agent, " ".join(argv), "life", life.now(), model=build_model(settings), deps=life,
                          usage_limits=USAGE_LIMITS))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
