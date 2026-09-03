"""WSGI development entrypoint for LegiView."""

import logging

from olis_archive import create_app


app = create_app()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    runtime = app.extensions["legiview"]["runtime"]
    app.run(
        host=runtime.config.host,
        port=runtime.config.port,
        debug=runtime.config.debug,
        use_reloader=False,
    )
