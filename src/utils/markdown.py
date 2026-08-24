import mistune
import nh3
from markupsafe import Markup

ALLOWED_HTML_TAGS = {
    "a",
    "p",
    "br",
    "b",
    "code",
    "i",
    "em",
    "pre",
    "strong",
    "ul",
    "ol",
    "li",
    "blockquote",
    "span",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}

ALLOWED_HTML_ATTRIBUTES = {
    "a": {"href", "title"},
}


def sanitize_html(value):
    if not value:
        return Markup("")
    cleaned = nh3.clean(
        str(value),
        tags=ALLOWED_HTML_TAGS,
        attributes=ALLOWED_HTML_ATTRIBUTES,
    )
    return Markup(cleaned)


def render_markdown_html(value):
    if not value:
        return ""
    html = mistune.html(value)
    return nh3.clean(
        html,
        tags=ALLOWED_HTML_TAGS,
        attributes=ALLOWED_HTML_ATTRIBUTES,
    )


def render_markdown_markup(value):
    return Markup(render_markdown_html(value))
