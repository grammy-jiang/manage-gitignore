#!/usr/bin/env python3
"""Ask gitignore.io whether it still answers the way this skill expects.

Every test in the suite stubs `curl`, which is what makes them fast, offline and
deterministic. The cost is that the contract with the outside world is asserted
only against fixtures written here. If Toptal changed the `# Created by` marker,
the URL, or what follows `# End of`, the skill would break for every user and
the whole suite would stay green.

This is the one thing that talks to the real API, and it runs on a schedule
rather than in the gates -- a pull request must never fail because somebody
else's service was down.

It reuses `templates.fetch_text` and `templates.check_api_block` rather than
describing the contract a second time. Two consequences worth stating: the
check cannot drift from what the skill actually requires, and this is the only
place the real fetch path -- curl, the byte cap, the streaming read -- is ever
run against the real service.

Exit codes, which the workflow distinguishes:

    0   the contract holds
    1   the contract changed -- somebody has to look at templates.py
    2   the API could not be reached, which is not the same event
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

# The scripts import each other by plain module name, resolved from their own
# directory the way `python3 <dir>/foo.py` resolves anything. Doing the same
# here is what makes the shebang honest: `./tests/check_api_contract.py` works
# from a bare checkout, with no PYTHONPATH to remember.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/manage_gitignore/skill/scripts"))

import templates

# Two templates, not one: the header echoes a comma-separated list, and a
# single name would not exercise the separator the parser splits on.
WANTED = ["node", "python"]

REACHABLE_TIMEOUT = 20


def reachable() -> str | None:
    """None if the service answered at all, else why it did not.

    Asked separately from the real fetch so that "the service is down" and "the
    service changed" are different outcomes: `fetch_text` dies the same way for
    both, and a watcher that cried wolf every time a network blipped would be
    switched off within a month.

    A 4xx is deliberately *not* unreachable. If the endpoint moved, the API
    answers 404 and that is precisely the change this exists to notice -- so it
    falls through to the contract check and fails there, loudly. Only a
    transport failure or a 5xx counts as the service being unavailable.
    """
    request = urllib.request.Request(  # noqa: S310 - the URL is this module's constant
        f"{templates.API}/{','.join(WANTED)}",
        headers={"User-Agent": "manage-gitignore contract check"},
    )
    try:
        with urllib.request.urlopen(request, timeout=REACHABLE_TIMEOUT) as response:  # noqa: S310
            if response.status >= 500:
                return f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        if exc.code < 500:
            return None  # it answered, and the answer is the contract check's business
        return f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return str(exc)
    return None


def main() -> int:
    why = reachable()
    if why is not None:
        print(f"could not reach {templates.API}: {why}", file=sys.stderr)
        return 2

    # From here, the skill's own code decides. `check_api_block` calls `die` on
    # anything it does not accept, which is a SystemExit carrying the message a
    # user would have seen.
    try:
        text = templates.fetch_text(",".join(WANTED))
        templates.check_api_block(text, WANTED)
    except SystemExit as exc:
        # The probe and the fetch are two separate requests, so the service can
        # go away between them -- and `fetch_text` dies the same way whether
        # curl failed or the response was wrong. Asking again is what tells
        # those apart; without it a transient outage is reported as a changed
        # contract, which is the one thing this must not cry wolf about.
        gone = reachable()
        if gone is not None:
            print(f"lost {templates.API} mid-check: {gone}", file=sys.stderr)
            return 2
        print(f"the API contract this skill depends on has changed: {exc}", file=sys.stderr)
        return 1

    sections = templates.api_pattern_sections(text)
    if not sections:
        print("no '### Name ###' sections in the response", file=sys.stderr)
        return 1

    print(f"contract holds: {len(text.splitlines())} lines, {len(sections)} patterns attributed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
