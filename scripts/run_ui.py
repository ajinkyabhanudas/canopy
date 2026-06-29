import logging
import warnings

# StarletteDeprecationWarning extends UserWarning (not DeprecationWarning).
# Raised by Gradio using a renamed Starlette constant — not actionable from canopy's code.
warnings.filterwarnings(
    "ignore",
    message=".*HTTP_422_UNPROCESSABLE_ENTITY.*",
    category=UserWarning,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

import gradio as gr  # noqa: E402

from canopy.ui.app import CSS, build_app  # noqa: E402 -- css lives beside the UI definition

if __name__ == "__main__":
    app = build_app()
    app.launch(server_name="0.0.0.0", server_port=7860, theme=gr.themes.Soft(), css=CSS)
