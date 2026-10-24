"""Development entry point: ``flask --app run.py run`` or ``python run.py``."""
import os

from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_ENV", "development") == "development")
