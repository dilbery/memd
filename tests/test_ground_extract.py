from memd.ground import extract_commands


def test_extracts_backticked_command_names():
    body = "the tool is `pkgaudit` at ~/.local/bin/pkgaudit; there is NO `parux`"
    cmds = extract_commands(body)
    assert "pkgaudit" in cmds
    assert "parux" in cmds


def test_ignores_non_command_words_and_paths():
    body = "see http://10.10.1.11:9002 and the file /data/state.db"
    cmds = extract_commands(body)
    # URLs and absolute paths are not bare command names
    assert "http" not in cmds
    assert "10.10.1.11" not in cmds


def test_extracts_first_token_of_fenced_shell_line():
    body = "```bash\ndocker compose ps\ngit status\n```"
    cmds = extract_commands(body)
    assert "docker" in cmds
    assert "git" in cmds


def test_dedups_commands():
    body = "`git` and `git` again"
    cmds = extract_commands(body)
    assert cmds.count("git") == 1
