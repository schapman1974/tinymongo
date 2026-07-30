"""Fault-injection coverage for destructive CLI collection replacement."""

import copy

import pytest

from tinymongo import cli


class _Database:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, _name):
        return self.collection


class _FaultingCollection:
    def __init__(self, documents, fail_delete=False, fail_insert=False):
        self.documents = copy.deepcopy(documents)
        self.fail_delete = fail_delete
        self.fail_insert = fail_insert

    def find(self, _filter):
        return copy.deepcopy(self.documents)

    def list_indexes(self):
        return [{"name": "_id_", "key": [("_id", 1)]}]

    def delete_many(self, _filter):
        if self.fail_delete:
            self.fail_delete = False
            self.documents = self.documents[1:]
            raise RuntimeError("delete interrupted")
        self.documents = []

    def insert_many(self, documents):
        if self.fail_insert:
            self.fail_insert = False
            self.documents.extend(copy.deepcopy(documents[:1]))
            raise RuntimeError("insert interrupted")
        self.documents.extend(copy.deepcopy(documents))


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("fail_delete", "delete interrupted"),
        ("fail_insert", "insert interrupted"),
    ],
)
def test_replace_collection_restores_previous_data_after_destructive_failure(
    failure, message
):
    previous = [{"_id": "old-1"}, {"_id": "old-2"}]
    replacement = [{"_id": "new-1"}, {"_id": "new-2"}]
    collection = _FaultingCollection(previous, **{failure: True})

    with pytest.raises(RuntimeError, match=message):
        cli._replace_collection(_Database(collection), "items", replacement)

    assert collection.documents == previous
