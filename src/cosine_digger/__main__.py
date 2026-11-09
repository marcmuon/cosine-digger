"""`python -m cosine_digger`: how `cosine-digger start` launches its server
with the same interpreter and environment it was installed with."""
from .cli import app

app(prog_name="cosine-digger")
