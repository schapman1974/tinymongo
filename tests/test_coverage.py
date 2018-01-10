import pytest
import pymongo


mongo_client = pymongo.MongoClient("localhost:27017")
mongo_database = mongo_client["test-mongodb"]
mongo_collection = mongo_database["test-collection"]


@pytest.fixture()
def collection(request):
    # setup the db, clear if necessary
    # todo: the 'drop()' function from pymongo should work in future revisions
    #mongo_collection.delete_many({})

    mongo_collection.insert_one({"Hello": "World"})

    def fin():
        mongo_client.close()

    request.addfinalizer(finalizer=fin)

    return mongo_collection


def test_initialize_collection(collection):
    c = collection.find({})
    assert c.count() == 1
    collection.insert_one({"Hello2": "World2"})


def test_initialize_collection2(collection):
    c = collection.find({})
    assert c.count() == 1


def test_initialize_collection3(collection):
    c = collection.find({})
    assert c.count() == 2
