import json
import re
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "integration-registration.md"


def _fenced_json_blocks(text):
    return re.findall(r"```json\n(.*?)```", text, re.DOTALL)


def test_doc_exists_and_has_valid_json_blocks():
    text = DOC.read_text(encoding="utf-8")
    blocks = _fenced_json_blocks(text)
    assert blocks, "no ```json blocks found"
    for b in blocks:
        json.loads(b)  # must be valid JSON


def test_doc_registers_mcp_under_home_svcuser_project():
    text = DOC.read_text(encoding="utf-8")
    assert '"/home/svcuser"' in text
    assert '"mcpServers"' in text
    assert "mem-mcp" in text


def test_doc_wires_userpromptsubmit_hook():
    text = DOC.read_text(encoding="utf-8")
    assert "UserPromptSubmit" in text
    assert "memd.hooks.auto_recall" in text


def test_doc_points_at_firewalld_script():
    text = DOC.read_text(encoding="utf-8")
    assert "firewalld-memd.sh" in text
