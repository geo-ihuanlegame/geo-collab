from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from server.app.core.paths import ensure_data_dirs, get_database_url
from server.app.db.base import Base
import server.app.modules.system.models        # noqa: F401
import server.app.modules.accounts.models      # noqa: F401
import server.app.modules.articles.models      # noqa: F401
import server.app.modules.tasks.models         # noqa: F401
import server.app.modules.ai_generation.models # noqa: F401
import server.app.modules.image_library.models # noqa: F401
import server.app.modules.skills.models        # noqa: F401
import server.app.modules.prompt_templates.models  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", get_database_url())

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    ensure_data_dirs()
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    ensure_data_dirs()
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

