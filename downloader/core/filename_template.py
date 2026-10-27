"""Custom filename templates.

A template is a format string such as "{date} - {title}_{index}{ext}".
Values are sanitized before they are inserted, and "/" in the template
itself creates subfolders below the usual media folder.
"""

import re
import string

TEMPLATE_TOKENS = (
    ("name", "original file name, without extension"),
    ("ext", "file extension, with the dot (.jpg)"),
    ("index", "attachment number within the post (supports {index:03})"),
    ("title", "post or album title"),
    ("post_id", "post ID"),
    ("published", "post date and time as given by the site"),
    ("date", "post date as YYYY-MM-DD"),
    ("user", "model / user name or ID"),
    ("site", "site the file comes from"),
    ("type", "images, videos, documents, compressed or other"),
    ("url_hash", "short hash of the file URL"),
)

DEFAULT_TEMPLATE = "{date} - {title}_{index}{ext}"

SAMPLE_FIELDS = {
    "name": "a1b2c3d4",
    "ext": ".jpg",
    "index": 1,
    "title": "Beach day",
    "post_id": "123456",
    "published": "2024-05-01T12:30:00",
    "date": "2024-05-01",
    "user": "model_name",
    "site": "coomer",
    "type": "images",
    "url_hash": "9f3a",
}

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_MAX_COMPONENT = 150


class TemplateError(ValueError):
    pass


def sanitize_component(value):
    return _INVALID_CHARS.sub("_", str(value)).strip()


def date_from_published(published):
    match = _DATE_RE.search(str(published or ""))
    return "-".join(match.groups()) if match else ""


def validate_template(template):
    """Raise TemplateError if the template cannot be rendered."""
    if not (template or "").strip():
        raise TemplateError("The template is empty.")
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(template) if f is not None]
    except ValueError as e:
        raise TemplateError(f"Invalid template: {e}") from None
    known = {name for name, _ in TEMPLATE_TOKENS}
    for field in fields:
        if field not in known:
            raise TemplateError(f"Unknown token {{{field}}}.")
    render_template(template, SAMPLE_FIELDS)


def render_template(template, fields):
    """Render a template into a relative path (may contain subfolders)."""
    ext = fields.get("ext") or ""
    values = {
        key: (value if isinstance(value, int) else sanitize_component(value or ""))
        for key, value in fields.items()
    }
    try:
        rendered = template.format_map(values)
    except (KeyError, IndexError) as e:
        raise TemplateError(f"Unknown token {e}.") from None
    except ValueError as e:
        raise TemplateError(f"Invalid template: {e}") from None

    parts = []
    for raw in re.split(r"[\\/]+", rendered):
        part = sanitize_component(raw).strip(" .")
        if part and part not in (".", ".."):
            parts.append(part)

    if not parts:
        parts = [(values.get("name") or "file") + ext]

    stem = parts[-1]
    if ext and stem.lower().endswith(ext.lower()):
        stem = stem[: -len(ext)]
    # Empty tokens leave separators behind, such as " - _1".
    stem = stem.strip(" -_.") or values.get("name") or "file"
    parts[-1] = stem[:_MAX_COMPONENT] + ext
    parts[:-1] = [p[:_MAX_COMPONENT] for p in parts[:-1]]
    return "/".join(parts)
