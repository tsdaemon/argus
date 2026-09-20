"""The plain-HTML pages served by argus itself: the login form and the break-glass views.

Pages are Jinja templates in `argus/templates/`, all extending `base.html` (the shared head,
favicon, stylesheet, and header). No JavaScript, phone-sized. Autoescaping is on, so request
text and error messages are escaped by the template, not by hand.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

_TEMPLATES = Path(__file__).with_name("templates")
_mark = (_TEMPLATES / "mark.svg").read_text()

_env = Environment(
    loader=FileSystemLoader(_TEMPLATES),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.globals["mark"] = Markup(_mark)  # inline logo, from a file we ship
_env.globals["favicon"] = "data:image/svg+xml," + quote(_mark)  # no extra route to serve or gate


def render(template: str, **context: object) -> str:
    return _env.get_template(template).render(**context)
