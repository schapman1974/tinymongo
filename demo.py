from tinymongo import tinymongo as tm
import os
import logging


logging.basicConfig(level=logging.INFO)

db_name = './demo_db'
print('db directory: ', db_name)

# remove the residual database to keep multiple iterations of this demo from continually adding to the same db
try:
    print('removing db files: {}'.format(os.listdir(os.path.join('.', db_name))))
    for f in os.listdir(os.path.join('.', db_name)):
        os.remove(os.path.join(db_name, f))
except FileNotFoundError:
    print('no files found to remove')

try:
    print('removing {}'.format(db_name))
    os.rmdir(db_name)
except FileNotFoundError:
    print('no directory found to remove')

# you can include a folder name as a parameter if not it will default to "tinydb"
tinyClient = tm.TinyMongoClient(db_name)

# either creates a new database file or accesses an existing one
tinyDatabase = tinyClient.tinyDatabase

# either creates a new collection or accesses an existing one
tinyCollection = tinyDatabase.tinyCollection

# insert records
print('inserting records...')
for i in range(5):
    recordId = tinyCollection.insert_one({"username": "user{}".format(i),
                                      "user_number": i,
                                      "password": "admin{}".format(i),
                                      "module": "somemodule"})

# show me all users, passwords, and 'modules'
print('finding all documents in the collection')
cursor = tinyCollection.find({})
count = tinyCollection.count()
print("Count:", count)
for c in cursor:
    print('\t{} {} {}'.format(c['username'], c['password'], c['module']))

# removing one element
print('deleting user 1')
tinyCollection.delete_one({'username': 'user1'})

# update data returns boolean if successful
print('updating "module" of user0')
tinyCollection.update_one({"username": "user0"}, {"$set": {"module": "someothermodule"}})

# print the updated results
print('finding all documents in the collection to show the updates')
cursor = tinyCollection.find({})
for c in cursor:
    print('\t{} {} {}'.format(c['username'], c['password'], c['module']))

# print where the number is >= 3
print('finding all documents in the collection where the number >= 3')
cursor = tinyCollection.find({'user_number': {'$gte': 3}})
for c in cursor:
    print('\t{} {} {}'.format(c['username'], c['password'], c['module']))


# find one document by user name
print('find a particular document by username, query = {"username": "user_2"}')
user_info = tinyCollection.find_one({'username': 'user2'})
print('\t{}'.format(user_info))
