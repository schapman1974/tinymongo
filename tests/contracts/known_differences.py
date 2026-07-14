"""Strict, temporary compatibility gaps exposed by the contract suite."""

import pytest


KNOWN_DIFFERENCES = {
    "array_in_query": {
        "sqlite": "#77: table-native $in does not yet match values inside arrays",
        "duckdb": "#77: table-native $in does not yet match values inside arrays",
        "parquet": "#77: table-native $in does not yet match values inside arrays",
    },
    "cursor_skip_limit": {
        "json": "#73: cursor pagination does not yet retain a PyMongo-style query spec",
        "sqlite": "#73: cursor pagination does not yet retain a PyMongo-style query spec",
        "duckdb": "#73: cursor pagination does not yet retain a PyMongo-style query spec",
        "parquet": "#73: cursor pagination does not yet retain a PyMongo-style query spec",
    },
}


def mark_known_difference(request, target_name, contract_name):
    """Mark a known gap strictly so an unexpected fix cannot pass unnoticed."""

    reason = KNOWN_DIFFERENCES.get(contract_name, {}).get(target_name)
    if reason:
        request.node.add_marker(pytest.mark.xfail(reason=reason, strict=True))
