import os
import pytest
import tinymongo as tm

# setup the db
db_name = os.path.abspath('./test_db')
try:
    for f in os.listdir(os.path.join('.', db_name)):
        print('removing file ', f)
        os.remove(os.path.join(db_name, f))
except OSError:
    pass

try:
    if len(os.listdir(db_name)) == 0:
        os.rmdir(db_name)
except OSError:
    pass

tiny_client = tm.TinyMongoClient(db_name)
tiny_database = tiny_client.tinyDatabase
tiny_collection = tiny_database.tinyCollection


@pytest.fixture()
def collection(request):
    # setup the db, clear if necessary
    # todo: the 'drop()' function from pymongo should work in future revisions
    tiny_collection.delete_many({})  # should delete all records in the collection

    # insert 100 integers, strings, floats, booleans, arrays, and objects
    for num in range(100):
        new_obj = {}
        new_obj['count'] = num
        new_obj['countStr'] = str(num)
        new_obj['countFloat'] = float(num) + 0.1
        new_obj['countBool'] = True if num & 1 else False
        new_obj['countArray'] = [num + i for i in range(5)]
        # todo: add object to the db

        tiny_collection.insert_one(new_obj)

    def fin():
        tiny_client.close()

    request.addfinalizer(finalizer=fin)

    return tiny_collection


def test_initialize_db():
    """
    Ensure that the db can be created, use two clients in order to increase coverage

    :return:
    """
    tiny_client = tm.TinyMongoClient(db_name)
    another_client = tm.TinyMongoClient(db_name)
    assert True


def test_access_database_by_subscription():
    assert isinstance(tiny_client['tinyDatabase'], tm.TinyMongoDatabase)


def test_access_collection_by_subscription():
    assert isinstance(tiny_database['tinyCollection'], tm.TinyMongoCollection)


def test_initialize_collection(collection):
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


def test_find_with_filter_named_parameter(collection):
    c = collection.find(filter={})
    assert c.count() == 100


def test_greater_than(collection):
    """
    Testing the greater than operator

    :param collection: pytest fixture that returns the collection
    :return:
    """
    c = collection.find({'count': {'$gte': 50}})

    assert c.count() == 50


def test_sort_wrong_input_type(collection):
    """
    Testing the sort method in the positive direction

    :param collection: pytest fixture that returns the collection
    :return:
    """
    c = collection.find()  # find all
    with pytest.raises(ValueError):
        c.sort('count')


def test_sort_positive(collection):
    """
    Testing the sort method in the positive direction

    :param collection: pytest fixture that returns the collection
    :return:
    """
    c = collection.find()  # find all
    c.sort({'count': 1})
    assert c[0]['count'] == 0
    assert c[1]['count'] == 1


def test_sort_negative(collection):
    """
    Testing the sort method in the positive direction

    :param collection: pytest fixture that returns the collection
    :return:
    """
    c = collection.find()  # find all
    c.sort({'count': -1})
    assert c[0]['count'] == 99
    assert c[1]['count'] == 98


def test_has_next(collection):
    """
    Testing hasNext

    :param collection: pytest fixture that returns the collection
    :return:
    """
    c = collection.find().sort({'count': 1})

    assert c.hasNext() is True
    assert c.next()['count'] == 0


def test_not_has_next(collection):
    """
    Testing hasNext when it should be false

    :param collection: pytest fixture that returns the collection
    :return:
    """
    c = collection.find({'count': {'$gte': 98}}).sort({'count': 1})

    assert c.hasNext() is True
    assert c.next()['count'] == 98
    assert c.hasNext() is True
    assert c.next()['count'] == 99
    assert c.hasNext() is False


def test_empty_find(collection):
    """
    Tests 'find' method when empty
    :param collection:
    :return:
    """
    c = collection.find()
    assert c.count() == 100


def test_find_one(collection):
    """
    Testing the retrieval of an item using the 'find_one' method

    :param collection: pytest fixture that returns the collection
    :return:
    """
    c = collection.find_one({'count': 3})

    assert c['countStr'] == '3'


def test_find_one_with_filter_named_parameter(collection):
    c = collection.find_one(filter={'count': 3})
    assert c['countStr'] == '3'


def test_gte_lt(collection):
    """
    Testing the >= and < queries
    :param collection: pytest fixture that returns the collection
    :return:
    """
    c = collection.find({'count': {'$gte': 50, '$lt': 51}})
    assert c.count() == 1
    assert c[0]['countStr'] == '50'


def test_gt_lte(collection):
    """
    Testing the < and >= queries
    :param collection: pytest fixture that returns the collection
    :return:
    """
    c = collection.find({'count': {'$gt': 50, '$lte': 51}})
    assert c.count() == 1
    assert c[0]['countStr'] == '51'


def test_ne(collection):
    """
    Testing the < and >= queries
    :param collection: pytest fixture that returns the collection
    :return:
    """
    c = collection.find({'count': {'$ne': 50}})
    assert c.count() == 99

    for item in c:
        assert item['countStr'] != '50'


def test_update_one_set(collection):
    """
    Testing the update_one method of the collection

    :param collection: pytest fixture that returns the collection
    :return:
    """
    cu = collection.update_one({'count': 3}, {'$set': {'countStr': 'three'}})
    # cu.raw_result contains the updated ids
    assert len(cu.raw_result) is 1  # only one is updated

    c = collection.find_one({'count': 3})
    assert c['countStr'] == 'three'


def test_delete_one(collection):
    """
    Testing the 'delete_one' method

    :param collection: pytest fixture that returns the collection
    :return:
    """
    collection.delete_one({'count': 3})

    c = collection.find({})

    assert c.count() == 99


def test_delete_all(collection):
    """
    Testing the 'delete_many' method to remove all

    :param collection: pytest fixture that returns the collection
    :return:
    """
    collection.delete_many({})

    c = collection.find({})

    assert c.count() == 0


def test_delete_many(collection):
    """
    Testing the 'delete_many' method to remove some items

    :param collection: pytest fixture that returns the collection
    :return:
    """
    collection.delete_many({'count': {'$gte': 50}})

    c = collection.find({})

    assert c.count() == 50


def test_insert_one(collection):
    """
    Testing the 'insert_one' method
    :param collection: pytest fixture that returns the collection
    :return:
    """
    collection.insert_one({'my_object_name': 'my object value', 'count': 1000})

    c = collection.find({})

    assert c.count() == 101
    assert collection.find({'my_object_name': 'my object value'})['count'] == 1000


def test_insert_many(collection):
    """
    Testing the 'insert_many' method
    :param collection: pytest fixture that returns the collection
    :return:
    """
    items = []
    for i in range(10):
        value = 1000 + i
        items.append({'count': value, 'countStr': str(value)})

    collection.insert_many(items)

    c = collection.find({})

    assert c.count() == 110


def test_insert_many_wrong_input(collection):
    """
    Testing the 'insert_many' method with a non-list input
    :param collection: pytest fixture that returns the collection
    :return:
    """
    item = {'my_key': 'my value'}
    with pytest.raises(ValueError):
        collection.insert_many(item)


def test_insert_one_with_list_input(collection):
    """
    Testing the 'insert_one' method with a list input
    :param collection: pytest fixture that returns the collection
    :return:
    """
    with pytest.raises(ValueError):
        collection.insert_one([{'my_object_name': 'my object value', 'count': 1000}])


def test_and(collection):
    """
    Testing the '$and' query
    :param collection: pytest fixture that returns the collection
    :return:
    """
    c = collection.find({"$and": [{"count": {"$gt": 10}}, {"count": {"$lte": 50}}]})
    assert c.count() == 40


def test_or(collection):
    """
    Testing the '$or' query
    :param collection: pytest fixture that returns the collection
    :return:
    """
    c = collection.find({"$or": [{"count": {"$lt": 10}}, {"count": {"$gte": 90}}]})
    assert c.count() == 20
