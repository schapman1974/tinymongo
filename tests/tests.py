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
    cursor = tiny_collection.find({})
    for item in cursor:
        item.remove()

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
    
