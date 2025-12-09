"""Main entry point for Campus Admin Portal Flask application."""

from apps.admin import create_app

app = create_app()
# breakpoint()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
