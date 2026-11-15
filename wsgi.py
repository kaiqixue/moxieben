"""Production entry point; requires explicit environment and secret files."""
from production import create_production_app

app = create_production_app()
