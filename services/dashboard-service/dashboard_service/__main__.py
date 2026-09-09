"""Run one origin per process: python -m dashboard_service serve-control|serve-content."""
import argparse
import os


def main() -> int:
    parser = argparse.ArgumentParser(prog="dashboard-service")
    parser.add_argument("role", choices=["serve-control", "serve-content"])
    parser.add_argument("--host", default=os.getenv("DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    import uvicorn
    from .config import Config
    from .database import Database, create_db_engine
    from .authn import Authenticator, HttpW3Verifier
    from .content_app import create_content_app
    from .routers import build_service
    from .app import create_control_app

    config = Config.from_env()
    database = Database(create_db_engine(config.database_url))
    database.seed_guard()
    database.verify_schema()
    if args.role == "serve-control":
        app = create_control_app(config, database=database)
        port = args.port or int(os.getenv("DASHBOARD_CONTROL_PORT", "8080"))
    else:
        verifier = HttpW3Verifier.from_config(config)
        service = build_service(config, database, Authenticator(config, database, verifier))
        app = create_content_app(service)
        port = args.port or int(os.getenv("DASHBOARD_CONTENT_PORT", "8081"))
    uvicorn.run(app, host=args.host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
