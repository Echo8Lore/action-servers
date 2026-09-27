#!/usr/bin/env python3
"""Static checks on the actions every workflow uses (OPS-42; stdlib).

Run from the repo root:  python3 -m unittest discover -s deploy -v

What they pin:
  1. No workflow uses a release known to target Node.js 20. GitHub forces those onto
     Node 24 with a deprecation warning today and will drop the fallback. The floor per
     action is the first major whose action.yml says `using: node24`.
  2. Every action outside the actions/ org is pinned by full commit SHA (a tag can be
     moved), with the release named in a trailing comment. webfactory/ssh-agent holds
     the deploy key, so its pin is also checked against the reviewed commit.
Nothing here touches the network.
"""

import pathlib
import re
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
_WF = ROOT / ".github" / "workflows"
# GitHub loads both extensions; a .yaml workflow must not slip past these checks.
WORKFLOWS = sorted([*_WF.glob("*.yml"), *_WF.glob("*.yaml")])

# First Node 24 major of each first-party action (checked against each release's
# action.yml, 2026-09-27). actions/cache/restore and /save share actions/cache's tag.
NODE24_FLOOR = {
    "actions/checkout": 5,
    "actions/cache": 5,
    "actions/upload-artifact": 6,
    "actions/download-artifact": 7,
}

# webfactory/ssh-agent v0.10.0: v0.9.1 plus runs.using node20 -> node24 (PR #243).
SSH_AGENT_SHA = "e83874834305fe9a4a2997156cb26c5de65a8555"

USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$")


def uses_lines():
    """(file:line, action ref, rest of line) for every `uses:` in every workflow."""
    out = []
    for wf in WORKFLOWS:
        for n, line in enumerate(wf.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            m = USES.match(line)
            if m:
                out.append((f"{wf.name}:{n}", m.group(1), m.group(2)))
    return out


def split_ref(ref):
    """('owner/repo', 'ref') for an action; subpaths (actions/cache/restore) drop."""
    path, _, version = ref.partition("@")
    return "/".join(path.split("/")[:2]), version


class ActionPins(unittest.TestCase):
    def setUp(self):
        self.uses = uses_lines()
        self.assertTrue(self.uses, "no uses: lines found; the parser is broken")

    def test_no_node20_releases(self):
        bad = []
        for where, ref, _ in self.uses:
            if ref.startswith("./"):
                continue
            repo, version = split_ref(ref)
            if repo not in NODE24_FLOOR:
                continue
            m = re.fullmatch(r"v(\d+)(?:\.\d+)*", version)
            if not m or int(m.group(1)) < NODE24_FLOOR[repo]:
                bad.append(f"{where}: {ref} (need v{NODE24_FLOOR[repo]}+)")
        self.assertEqual(bad, [], "action release older than its first Node 24 major")

    def test_every_action_is_known(self):
        # A new first-party action has to get a Node 24 floor above; a new third-party
        # one gets a SHA pin (next test). Either way, nothing slips in unchecked.
        unknown = []
        for where, ref, _ in self.uses:
            if ref.startswith("./"):
                continue
            repo, _ = split_ref(ref)
            if repo.startswith("actions/") and repo not in NODE24_FLOOR:
                unknown.append(f"{where}: {ref}")
        self.assertEqual(unknown, [], "first-party action with no Node 24 floor in NODE24_FLOOR")

    def test_third_party_actions_pinned_by_sha(self):
        bad = []
        for where, ref, rest in self.uses:
            if ref.startswith("./") or ref.startswith("actions/"):
                continue
            _, version = split_ref(ref)
            if not re.fullmatch(r"[0-9a-f]{40}", version) or not re.search(r"#\s*v\d", rest):
                bad.append(f"{where}: {ref}{rest}")
        self.assertEqual(bad, [], "third-party action not pinned by full SHA with a '# vX' comment")

    def test_ssh_agent_is_the_reviewed_node24_commit(self):
        refs = [(w, ref, rest) for w, ref, rest in self.uses
                if split_ref(ref)[0] == "webfactory/ssh-agent"]
        self.assertEqual(len(refs), 1, refs)
        where, ref, rest = refs[0]
        self.assertEqual(split_ref(ref)[1], SSH_AGENT_SHA, where)
        self.assertIn("# v0.10.0", rest, where)


if __name__ == "__main__":
    unittest.main()
