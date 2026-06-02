import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--mysql", action="store_true", default=False, help="deprecated; tests use MySQL only"
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "mysql: mark test as requiring the disposable MySQL test database"
    )


def pytest_collection_modifyitems(config, items):
    import os

    if os.environ.get("GEO_TEST_DATABASE_URL"):
        return
    skip_mysql = pytest.mark.skip(reason="Set GEO_TEST_DATABASE_URL to run MySQL database tests")
    for item in items:
        if "mysql" in item.keywords:
            item.add_marker(skip_mysql)


@pytest.fixture(scope="session")
def mysql_db(request):
    import os

    if not os.environ.get("GEO_TEST_DATABASE_URL"):
        pytest.skip("Set GEO_TEST_DATABASE_URL to run MySQL database tests")
    return None
