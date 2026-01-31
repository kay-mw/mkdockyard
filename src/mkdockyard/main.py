from mkdocs.config import base
from mkdocs.config import config_options as c
from mkdocs.plugins import BasePlugin


class MkdockyardConfig(base.Config):
    foo = c.Type(dict, default={"a": 1})


class MkdockyardPlugin(BasePlugin[MkdockyardConfig]):
    def on_serve(self, server, config, builder):
        print("It's working!")
