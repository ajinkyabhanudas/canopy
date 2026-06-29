import logging
import warnings

# Gradio uses a renamed Starlette constant that triggers a DeprecationWarning
# from starlette itself. Not actionable from canopy's code.
warnings.filterwarnings(
    "ignore",
    message=".*HTTP_422_UNPROCESSABLE_ENTITY.*",
    category=DeprecationWarning,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

from canopy.ui.app import build_app  # noqa: E402

if __name__ == "__main__":
    app = build_app()
    import gradio as gr
    app.launch(server_name="0.0.0.0", server_port=7860, theme=gr.themes.Soft())
