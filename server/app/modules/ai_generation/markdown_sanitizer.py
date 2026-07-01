"""Markdown content normalization for generated articles."""

from __future__ import annotations


def normalize_markdown_content(markdown: str) -> str:
    """Repair the known writer handoff artifact: JSON-escaped ASCII quotes.

    Some loop writers compose tool arguments as if the markdown body were JSON source,
    so natural straight quotes arrive as the literal two characters ``\"``.  Keep this
    deliberately narrow: do not run generic unicode/string unescaping here.
    """
    if not markdown or '\\"' not in markdown:
        return markdown
    return markdown.replace('\\"', '"')
