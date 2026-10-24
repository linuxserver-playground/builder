"""?+severity[Title](|text|) shorthand converts to GitHub alerts and mkdocs admonitions like the reference filter."""

from builder.admonition import admonition, convert

PLEX = (
    "Webui can be found at `<your-ip>:32400/web`\n\n"
    "?+note[Updates](|If there is no value set for the VERSION variable, then no updates will take place.|)\n\n"
    "?+info[Plexpass](|YOU CANNOT UPDATE TO A PLEXPASS ONLY (BETA) VERSION.|)\n\n"
    "+ **`docker`**: Let Docker handle the Plex Version."
)


def test_gfm_conversion() -> None:
    out = convert(PLEX, "gfm")
    expected = (
        "> [!NOTE]\n> **Updates**\n>\n"
        "> If there is no value set for the VERSION variable, then no updates will take place."
    )
    assert expected in out
    assert "> [!NOTE]\n> **Plexpass**\n>\n> YOU CANNOT UPDATE" in out
    assert out.startswith("Webui can be found at `<your-ip>:32400/web`\n\n")
    assert out.endswith("+ **`docker`**: Let Docker handle the Plex Version.")
    assert "?+" not in out


def test_mkdocs_conversion() -> None:
    out = convert(PLEX, "mkdocs")
    assert '!!! note "Updates"\n\n    If there is no value set' in out
    assert '!!! info "Plexpass"\n\n    YOU CANNOT UPDATE' in out
    assert "?+" not in out


def test_untitled_and_multiline() -> None:
    assert convert("?+warning(|line one\nline two|)", "gfm") == "> [!WARNING]\n> line one\n> line two"
    assert convert("?+warning(|line one\nline two|)", "mkdocs") == "!!! warning\n\n    line one\n    line two"


def test_severity_mapping_matches_reference() -> None:
    assert admonition("x", "gfm", "tip") == "> [!TIP]\n> x"
    assert admonition("x", "gfm", "danger") == "> [!WARNING]\n> x"
    assert admonition("x", "gfm", "failure") == "> [!CAUTION]\n> x"
    assert admonition("x", "gfm", "bug") == "> [!IMPORTANT]\n> x"
    assert admonition("x", "gfm", "unknown") == "> [!NOTE]\n> x"
    assert admonition("x", "mkdocs", "unknown") == "!!! note\n\n    x"


def test_collapsible() -> None:
    assert (
        admonition("body", "gfm", "tip", "Title", collapse=True)
        == "<details>\n<summary>Title</summary>\n\nbody\n</details>"
    )
    assert admonition("body", "mkdocs", "tip", "Title", collapse=True) == '??? tip "Title"\n\n    body'
    assert admonition("body", "mkdocs", "tip", "Title", collapse="expanded") == '???+ tip "Title"\n\n    body'


def test_deprecation() -> None:
    assert admonition("gone", "gfm", "deprecation").startswith("# DEPRECATION NOTICE")


def test_text_without_shorthand_is_untouched() -> None:
    text = "Plain **Markdown** with a | pipe and (parens) and ?+ but no admonition."
    assert convert(text, "gfm") == text
