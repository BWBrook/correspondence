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


def test_machine_attribution_is_part_of_author_and_obeys_host_redaction(tmp_path):
    path = tmp_path / 'machines.toml'
    path.write_text('project = "p"\nredact = [["\\\\bmacbook\\\\b", "[host]"]]\n')
    rules = load_policy(path)
    msgs = [
        message(1, 'From the laptop.'),
        {**message(2, 'From WSL.'), 'author': 'Codex', 'machine': 'wsl'},
        {**message(3, 'From mini.'), 'machine': 'mini'},
        {**message(4, 'From alien.'), 'author': 'Codex', 'machine': 'alien'},
        message(5, 'Historical message.'),
        {**message(6, 'Human response.'), 'author': 'User', 'machine': 'mini'},
    ]
    msgs[0]['machine'] = 'macbook'
    text, problems = render(THREAD, msgs, rules, user='Barry')
    assert not problems
    assert 'Message 1 · Claude@[host]' in text and 'macbook' not in text
    assert 'Message 2 · Codex@wsl' in text
    assert 'Message 3 · Claude@mini' in text
    assert 'Message 4 · Codex@alien' in text
    assert 'Message 5 · Claude ·' in text
    assert 'Message 6 · Barry ·' in text and 'Barry@' not in text


def test_withheld_machine_header_still_redacts_the_hostname(tmp_path):
    path = tmp_path / 'withheld.toml'
    path.write_text('project = "p"\nredact = [["macbook", "[host]"]]\n[withheld]\n1 = "private"\n')
    text, problems = render(THREAD, [{**message(1, 'private body'), 'machine': 'macbook'}], load_policy(path))
    assert not problems and 'private body' not in text
    assert 'Claude@[host]' in text and 'macbook' not in text
