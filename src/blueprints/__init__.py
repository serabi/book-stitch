"""Flask Blueprints for Book Stitch web server."""


def register_blueprints(app):
    """Register all application blueprints with the Flask app."""
    from src.blueprints.abs_bp import abs_bp
    from src.blueprints.api import api_bp
    from src.blueprints.books import books_bp
    from src.blueprints.covers import covers_bp
    from src.blueprints.dashboard import dashboard_bp
    from src.blueprints.logs import logs_bp
    from src.blueprints.settings_bp import settings_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(books_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(covers_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(abs_bp)
    app.register_blueprint(logs_bp)
