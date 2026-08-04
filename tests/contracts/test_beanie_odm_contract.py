from collections.abc import Mapping

import pytest


pytestmark = pytest.mark.contract


def _assert_update_reply(result, *, matched, modified, existing, upserted=None):
    assert isinstance(result.raw_result, Mapping)
    assert result.raw_result["n"] == (1 if upserted is not None else matched)
    assert result.raw_result["nModified"] == modified
    assert result.raw_result["updatedExisting"] is existing
    assert result.raw_result["ok"] == 1.0
    if upserted is None:
        assert "upserted" not in result.raw_result
    else:
        assert result.raw_result["upserted"] == upserted
    assert result.matched_count == matched
    assert result.modified_count == modified
    assert result.upserted_id == upserted
    assert result.did_upsert is (upserted is not None)


def _assert_delete_reply(result, deleted):
    assert isinstance(result.raw_result, Mapping)
    assert result.raw_result["n"] == deleted
    assert result.raw_result["ok"] == 1.0
    assert result.deleted_count == deleted


def test_beanie_write_result_contract(contract_target):
    collection = contract_target.collection
    collection.insert_many(
        [
            {"_id": 1, "value": 0},
            {"_id": 2, "value": 0},
        ]
    )

    changed = collection.update_one({"_id": 1}, {"$set": {"value": 1}})
    _assert_update_reply(changed, matched=1, modified=1, existing=True)

    unchanged = collection.replace_one({"_id": 1}, {"value": 1})
    _assert_update_reply(unchanged, matched=1, modified=0, existing=True)

    missing = collection.update_one({"_id": 99}, {"$set": {"value": 1}})
    _assert_update_reply(missing, matched=0, modified=0, existing=False)

    collection.update_one({"_id": 1}, {"$set": {"seen": True}})
    many = collection.update_many({}, {"$set": {"seen": True}})
    _assert_update_reply(many, matched=2, modified=1, existing=True)

    upserted = collection.replace_one(
        {"_id": 3},
        {"_id": 3, "value": 3},
        upsert=True,
    )
    _assert_update_reply(
        upserted,
        matched=0,
        modified=0,
        existing=False,
        upserted=3,
    )

    _assert_delete_reply(collection.delete_one({"_id": 2}), 1)
    _assert_delete_reply(collection.delete_many({"_id": 99}), 0)
    _assert_delete_reply(collection.delete_many({"seen": True}), 1)
