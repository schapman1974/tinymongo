import tinymongo as tm
import os

db_name = 'demo_db'

# remove the residual database to keep multiple iterations of this demo from continually adding to the same db
try:
    for f in os.listdir(os.path.join('.', db_name)):
        os.remove(os.path.join(db_name, f))
except FileNotFoundError:
    pass

try:
    os.rmdir('demo_db')
except FileNotFoundError:
    pass

# you can include a folder name as a parameter if not it will default to "tinydb"
tinyClient = tm.TinyMongoClient(db_name)

# either creates a new database file or accesses an existing one
tinyDatabase = tinyClient.tinyDatabase

# either creates a new collection or accesses an existing one
tinyCollection = tinyDatabase.tinyCollection

# insert 10 records
for i in range(5):
    recordId = tinyCollection.insert({"username": "user{}".format(i),
                                      "password": "admin{}".format(i),
                                      "module": "somemodule"})

# show me all users, passwords, and 'modules'
cursor = tinyCollection.find({})
for c in cursor:
    print('\t{} {} {}'.format(c['username'], c['password'], c['module']))

# update data returns boolean if successful
if tinyCollection.update({"username": "user0"}, {"$set": {"module": "someothermodule"}}):
    print('db updated!')
else:
    print('db not updated')

# print the updated results
cursor = tinyCollection.find({})
for c in cursor:
    print('\t{} {} {}'.format(c['username'], c['password'], c['module']))

# find one document by user name
user_info = tinyCollection.find_one({'username': 'user2'})
print(user_info)
