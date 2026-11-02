from correspondence.export import load_policy, render

THREAD = {"id": 7, "title": "T", "status": "open"}


def message(id, body):
    return {"id": id, "author": "Claude", "model": None, "kind": "note", "created_at": "t", "parent_id": None, "body": body}


def policy(tmp_path, extra=""):
    path = tmp_path / "publication.toml"
    path.write_text('project = "p"\nflag_terms = ["password"]\nredact = [["\\\\bhost\\\\.example\\\\.org\\\\b", "[host]"]]\n' + extra)
    return load_policy(path)


def test_flagged_message_is_reported_until_reviewed(tmp_path):
    msgs = [message(1, "the password is in the portal")]
    _, problems = render(THREAD, msgs, policy(tmp_path))
    assert problems == ["discussion 7 message 1: password"]
    _, problems = render(THREAD, msgs, policy(tmp_path, "cleared = [1]\n"))
    assert problems == []


def test_withheld_message_is_replaced_and_redactions_apply(tmp_path):
    msgs = [message(1, "password detail"), message(2, "see /Users/alice/x on 10.0.0.1 via host.example.org")]
    text, problems = render(THREAD, msgs, policy(tmp_path, '[withheld]\n1 = "access detail"\n'))
    assert problems == []
    assert "password detail" not in text and "Withheld from the public record: access detail." in text
    assert "~/x on [address] via [host]" in text
