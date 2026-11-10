"""Run: python3 test_lazy_resume.py"""
import json
import os
import tempfile

from lazy_resume import build_command, claim_boot, line_hash, load_config, sync

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"


def pane(pid, agent=None, sid=None, term=None):
    p = {"pane_id": pid, "agent": agent, "terminal_id": term or "t-" + pid}
    if sid:
        p["agent_session"] = {"agent": "claude", "value": sid}
    return p


def test():
    s = {}
    # boot 1: plugin first sees panes; nothing pending yet
    live = [pane("w1:p1", "claude", A), pane("w1:p2", "claude", B), pane("w1:p3")]
    assert claim_boot(s, "b1", live) and s["pending"] == {}
    sync(s, live)
    assert s["panes"] == {"w1:p1": A, "w1:p2": B}

    # user quits claude in p2 (herdr: agent_detected released=true)
    sync(s, [pane("w1:p1", "claude", A), pane("w1:p2"), pane("w1:p3")], gone=["w1:p2"])
    assert s["panes"] == {"w1:p1": A}

    # a list without the agent but no release event (e.g. read during shutdown) keeps the record
    sync(s, [pane("w1:p1"), pane("w1:p2"), pane("w1:p3")])
    assert s["panes"] == {"w1:p1": A}

    # session id changes (/clear) are picked up; a missing report keeps the old id
    sync(s, [pane("w1:p1", "claude", B), pane("w1:p2"), pane("w1:p3")])
    assert s["panes"]["w1:p1"] == B
    sync(s, [pane("w1:p1", "claude"), pane("w1:p2"), pane("w1:p3")])
    assert s["panes"]["w1:p1"] == B

    # boot 2 (new terminal ids): p1 comes back as a shell -> pending; p2 (quit by user) is not
    live = [pane("w1:p1", term="n1"), pane("w1:p2", term="n2"), pane("w1:p3", term="n3")]
    assert claim_boot(s, "b2", live) and s["pending"] == {"w1:p1": B}
    assert not claim_boot(s, "b2", live)  # once per boot
    sync(s, live)
    assert s["pending"] == {"w1:p1": B} and s["panes"] == {"w1:p1": B}

    # unvisited pending pane carries over to the next restart
    assert claim_boot(s, "b3", live) and s["pending"] == {"w1:p1": B}

    # moved to another workspace: new id, same terminal -> record and pending follow
    sync(s, [pane("w2:p1", term="n1"), pane("w1:p2", term="n2"), pane("w1:p3", term="n3")])
    assert s["pending"] == {"w2:p1": B} and s["panes"] == {"w2:p1": B}

    # another agent in the pane cancels it
    sync(s, [pane("w2:p1", "codex", term="n1"), pane("w1:p2", term="n2"), pane("w1:p3", term="n3")])
    assert s["pending"] == {} and s["panes"] == {}

    # closed panes are forgotten (event), and pruned at the next boot otherwise
    s = {"boot": "b1", "panes": {"w1:p9": A, "w1:p8": B}, "pending": {"w1:p9": A}}
    sync(s, [pane("w1:p1")], gone=["w1:p9"])
    assert s["panes"] == {"w1:p8": B} and s["pending"] == {}
    claim_boot(s, "b2", [pane("w1:p1")])
    assert s["panes"] == {} and s["pending"] == {}

    # a closed pane's terminal id showing up again must not carry its entry over
    s = {"panes": {"w1:p9": A}, "pending": {"w1:p9": A}, "terms": {"w1:p9": "t9"}}
    sync(s, [pane("w1:p1", term="t9")], gone=["w1:p9"])
    assert s["panes"] == {} and s["pending"] == {}

    # prompt reference ignores re-padding (right prompt on resize) but not typed text
    assert line_hash("~/proj ❯        aws") == line_hash("~/proj ❯  aws")
    assert line_hash("~/proj ❯ ls") != line_hash("~/proj ❯")

    # command building only accepts UUIDs
    assert build_command("claude --resume {id}", A) == "claude --resume " + A
    assert build_command("claude --resume {id}", A + "\n") is None
    assert build_command("claude --resume {id}", "x; rm -rf ~") is None
    assert build_command("claude --resume {id}", None) is None

    # config: valid overrides apply, invalid values fall back to defaults
    default = {"min_interval_seconds": 5, "command": "claude --resume {id}"}
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.json")
        assert load_config(path) == default
        for text, want in [('{"min_interval_seconds": 2, "command": "echo {id}"}', {"min_interval_seconds": 2, "command": "echo {id}"}),
                           ('{"min_interval_seconds": -1, "command": "no placeholder"}', default),
                           ('{"min_interval_seconds": Infinity}', default),
                           ("[]", default)]:
            with open(path, "w") as f:
                f.write(text)
            assert load_config(path) == want, text


if __name__ == "__main__":
    test()
    print("ok")
