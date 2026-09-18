"""Round-21 index guards compared with MongoDB through both client APIs."""

import pytest
from pymongo import IndexModel
from pymongo.errors import DuplicateKeyError, OperationFailure


pytestmark = pytest.mark.contract


@pytest.mark.parametrize("unique", [False, True])
@pytest.mark.parametrize(
    "arrays", [(list, list), (tuple, tuple), (list, tuple), (tuple, list)]
)
def test_parallel_arrays_rejected_before_storage(contract_target, unique, arrays):
    col = contract_target.collection
    keys = [("a", 1), ("b", 1)]
    col.create_index(keys, unique=unique)
    bad = {"_id": 1, "a": arrays[0](["x", "y"]), "b": arrays[1](["p", "q"])}
    with pytest.raises(OperationFailure) as caught:
        col.insert_one(bad)
    assert caught.value.code == 171
    assert col.count_documents({}) == 0

    original = {"_id": 1, "a": ["x", "y"], "b": "p"}
    col.insert_one(original)
    for write in (
        lambda: col.update_one({"_id": 1}, {"$set": {"b": bad["b"]}}),
        lambda: col.replace_one({"_id": 1}, bad),
    ):
        with pytest.raises(OperationFailure) as caught:
            write()
        assert caught.value.code == 171
        assert col.find_one({"_id": 1}) == original

    col.drop_index("a_1_b_1")
    col.replace_one({"_id": 1}, bad)
    with pytest.raises(OperationFailure) as caught:
        col.create_index(keys, unique=unique)
    assert caught.value.code == 171
    assert list(col.index_information()) == ["_id_"]


@pytest.mark.parametrize("compound", [False, True])
def test_one_tuple_array_preserves_unique_enforcement(contract_target, compound):
    col = contract_target.collection
    col.create_index([("a", 1), ("b", 1)] if compound else "a", unique=True)
    col.insert_one({"_id": 1, "a": ("x", "y", "x"), "b": "p"})
    assert col.find_one({"_id": 1})["a"] == ["x", "y", "x"]
    for value in [("y", "z"), ["y", "z"]]:
        with pytest.raises(DuplicateKeyError):
            col.insert_one({"_id": 2, "a": value, "b": "p"})
    col.insert_one({"_id": 3, "a": ("z",), "b": "p"})
    assert col.count_documents({}) == 2


@pytest.mark.parametrize("unique", [False, True])
def test_tuple_arrays_outside_partial_index_are_legal(contract_target, unique):
    col = contract_target.collection
    col.create_index(
        [("a", 1), ("b", 1)],
        unique=unique,
        partialFilterExpression={"active": True},
    )
    col.insert_one({"_id": 1, "a": ("x",), "b": ("y",), "active": False})
    with pytest.raises(OperationFailure) as caught:
        col.update_one({"_id": 1}, {"$set": {"active": True}})
    assert caught.value.code == 171
    assert col.find_one({"_id": 1})["active"] is False


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize(
    "expression, code",
    [
        ({"a": {"$ne": 1}}, 67),
        ({"a": {"$nin": [1]}}, 67),
        ({"a": {"$regex": "x"}}, 67),
        ({"a": {"$exists": False}}, 67),
        ({"a": {"$gt": 1, "unit": "kg"}}, 2),
        ({"a": {"$in": 1}}, 2),
        ({"$where": "this.a > 1"}, 2),
        ({"$unknown": 1}, 2),
        ("not a mapping", 14),
        ([], 14),
        ({"$and": 1}, 2),
        ({"$or": 1}, 2),
        ({"$and": []}, 2),
        ({"$or": []}, 2),
        ({"$and": [1]}, 2),
        ({"$nor": [{"active": True}]}, 67),
    ],
)
def test_partial_index_validation_codes(contract_target, batch, expression, code):
    col = contract_target.collection
    col.insert_one({"_id": 1, "a": 3})
    with pytest.raises(OperationFailure) as caught:
        if batch:
            col.create_indexes([IndexModel("a", partialFilterExpression=expression)])
        else:
            col.create_index("a", partialFilterExpression=expression)
    assert caught.value.code == code
    assert list(col.index_information()) == ["_id_"]
    assert list(col.find({})) == [{"_id": 1, "a": 3}]


@pytest.mark.parametrize("batch", [False, True])
def test_empty_partial_filter_indexes_every_document(contract_target, batch):
    col = contract_target.collection
    if batch:
        col.create_indexes([IndexModel("a", unique=True, partialFilterExpression={})])
    else:
        col.create_index("a", unique=True, partialFilterExpression={})
    col.insert_one({"_id": 1, "a": "x"})
    col.insert_one({"_id": 2})
    for doc in [{"_id": 3, "a": "x"}, {"_id": 4}, {"_id": 5, "a": None}]:
        with pytest.raises(DuplicateKeyError):
            col.insert_one(doc)
    assert col.index_information()["a_1"]["partialFilterExpression"] == {}
    assert col.count_documents({}) == 2
