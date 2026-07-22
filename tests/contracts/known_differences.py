"""Strict, temporary compatibility gaps exposed by the contract suite."""

import pytest


KNOWN_DIFFERENCES = {}


def mark_known_difference(request, target_name, contract_name):
    """Mark a known gap strictly so an unexpected fix cannot pass unnoticed."""

    reason = KNOWN_DIFFERENCES.get(contract_name, {}).get(target_name)
    if reason:
        request.node.add_marker(pytest.mark.xfail(reason=reason, strict=True))
