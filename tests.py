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

    for num in range(100):
        tiny_collection.insert({'count'.format(num): num})

    def fin():
        tiny_client.close()

    request.addfinalizer(finalizer=fin)

    return tiny_collection


def test_initialize_db(collection):
    c = collection.find({})

    count = 0
    for doc in c:
        count += 1

    assert count == 100


