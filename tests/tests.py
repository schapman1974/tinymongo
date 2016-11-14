import os
import pytest
import tinymongo as tm

# setup the db
db_name = os.path.abspath('./test_db')
try:
    for f in os.listdir(os.path.join('.', db_name)):
        print('removing file ', f)
        os.remove(os.path.join(db_name, f))
except FileNotFoundError:
    pass

try:
    if len(os.listdir(db_name)) == 0:
        os.rmdir(db_name)
except FileNotFoundError:
    pass

tiny_client = tm.TinyMongoClient(db_name)
tiny_database = tiny_client.tinyDatabase
tiny_collection = tiny_database.tinyCollection


@pytest.fixture()
def collection(request):
    # setup the db, clear if necessary
    # todo: the 'delete_many()' and 'drop()' function from pymongo should work in future revisions
    #tiny_collection.delete_one({})
    #tiny_collection.delete_many({})    # should delete all records in the collection

    # insert 100 integers, strings, floats, booleans, arrays, and objects
    for num in range(100):
        new_obj = {}
        new_obj['count'] = num
        new_obj['countStr'] = str(num)
        new_obj['countFloat'] = float(num) + 0.1
        new_obj['countBool'] = True if num & 1 else False
        new_obj['countArray'] = [num + i for i in range(5)]
        # todo: add object to the db

        tiny_collection.insert(new_obj)

    def fin():
        tiny_client.close()

    request.addfinalizer(finalizer=fin)

    return tiny_collection


def test_initialize_db(collection):
    """
    Ensure that the initial db is of the correct size

    :param collection: pytest fixture that returns the collection
    :return:
    """
    c = collection.find({})

    count = 0
    for doc in c:
        count += 1

    assert count == 100
    assert c.count() == 100


def test_greater_than(collection):
    """
    Testing the greater than operator

    :param collection: pytest fixture that returns the collection
    :return:
    """
    c = collection.find({'count': {'$gt': 50}})

    assert c.count() == 50


def test_find_one(collection):
    """
    Testing the retrieval of an item using the 'find_one' method

    :param collection: pytest fixture that returns the collection
    :return:
    """
    c = collection.find_one({'count': 3})

    assert c['countStr'] == '3'


def test_delete_one(collection):
    """
    Testing the 'delete_one' method

    :param collection: pytest fixture that returns the collection
    :return:
    """
    collection.delete_one({'count': 3})

    c = collection.find({})

    count = 0
    for document in c:
        count += 1

    assert count == 99
