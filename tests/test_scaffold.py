import sys

import memd


def test_interpreter_is_313():
    assert sys.version_info[:2] == (3, 13), (
        f"venv must be CPython 3.13, got {sys.version_info[:3]}; "
        "the system python may be 3.14, which breaks sqlite-vec"
    )


def test_sqlite_vec_loads():
    import sqlite3

    import sqlite_vec

    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    (version,) = db.execute("select vec_version()").fetchone()
    assert version.startswith("v")


def test_package_version():
    assert memd.__version__ == "0.1.0"
