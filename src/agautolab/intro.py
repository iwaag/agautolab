"""`python -m agautolab.intro`: post `params/intro.md` to `#agents` as this instance.

The topic `intro-<instance>` is append-only history: the newest post is the
current contract, the older ones stay readable. Run it whenever the
introduction or the behavior it describes changes. The mechanics are
`agag.agent.intro_main`.
"""

from agag.agent import intro_main

from .instance import SPEC

if __name__ == "__main__":
    intro_main(SPEC)
