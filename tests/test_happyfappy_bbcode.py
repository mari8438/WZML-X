import ast
import re
from pathlib import Path
import pytest

_REPO_ROOT = Path(__file__).parents[1]
_CREATE_TORRENT_FILE = _REPO_ROOT / "bot" / "modules" / "create_torrent.py"


def _extract_bbcode_templates():
    source = _CREATE_TORRENT_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "BBCODE_TEMPLATES":
                    return ast.literal_eval(node.value)
    raise RuntimeError("BBCODE_TEMPLATES not found in create_torrent.py")


def test_happyfappy_template_structure():
    templates = _extract_bbcode_templates()
    template = templates.get("happyfappy")
    assert template is not None, "happyfappy template should be defined"
    
    # Check official presentation tags
    for tag in ["[plot]", "[details]", "[info]", "[screens]"]:
        assert tag in template, f"Template must contain Gazelle badge {tag}"
        
    # Check structure tags
    assert "[table=" in template
    assert "[tr]" in template
    assert "[/tr]" in template
    assert "[/table]" in template
    assert "[bg=" in template
    assert "[/bg]" in template
    assert "[code]{media_info}[/code]" in template
    assert "{title}" in template
    assert "{description}" in template
    assert "{genres}" in template
    assert "{thumbnail_link}" in template
    assert "{thumbnail_page}" in template
    assert "{contact_sheet_link}" in template
    assert "{contact_sheet_page}" in template


def test_happyfappy_tag_balance():
    templates = _extract_bbcode_templates()
    template = templates["happyfappy"]
    
    # Check paired tags have equal open and close counts
    for tag_name in ["font", "color", "center", "size", "b", "table", "tr", "td", "code", "url", "img"]:
        open_count = len(re.findall(rf"\[{tag_name}(?:=[^\]]*)?\]", template, re.IGNORECASE))
        close_count = len(re.findall(rf"\[/{tag_name}\]", template, re.IGNORECASE))
        assert open_count == close_count, f"Tag mismatch for [{tag_name}]: {open_count} open vs {close_count} close"
        
    # Check bg tag balance specifically
    bg_open = len(re.findall(r"\[bg(?:=[^\]]*)?\]", template, re.IGNORECASE))
    bg_close = len(re.findall(r"\[/bg\]", template, re.IGNORECASE))
    assert bg_open == bg_close, f"Tag mismatch for [bg]: {bg_open} open vs {bg_close} close"


def test_happyfappy_rendering():
    templates = _extract_bbcode_templates()
    template = templates["happyfappy"]
    values = {
        "title": "Kanojo wa Dare to demo Sex Suru - 1",
        "thumbnail_link": "https://t3.pixhost.cc/thumbs/1/cover.jpg",
        "thumbnail_page": "https://pixhost.to/show/1/cover.jpg",
        "description": "An adventurous young woman discovers romance and excitement.",
        "genres": "Vanilla, Creampie, 1080p",
        "media_info": "File Size: 500MB\nResolution: 1920x1080",
        "contact_sheet_link": "https://t3.pixhost.cc/thumbs/1/screens.jpg",
        "contact_sheet_page": "https://pixhost.to/show/1/screens.jpg",
    }
    rendered = template
    for k, v in values.items():
        rendered = rendered.replace("{" + k + "}", v)
        
    # Check no unresolved placeholders remain
    unresolved = re.findall(r"\{[a-z0-9_]+\}", rendered)
    assert not unresolved, f"Unresolved template variables found: {unresolved}"
    
    # Verify rendered content
    assert "Kanojo wa Dare to demo Sex Suru - 1" in rendered
    assert "https://pixhost.to/show/1/cover.jpg" in rendered
    assert "https://t3.pixhost.cc/thumbs/1/screens.jpg" in rendered
    assert "Vanilla, Creampie, 1080p" in rendered
