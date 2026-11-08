from importlib.metadata import version

import cryptolab


def test_source_and_distribution_versions_are_v1() -> None:
    assert cryptolab.__version__ == "1.0.0"
    assert version("cryptolab") == "1.0.0"
