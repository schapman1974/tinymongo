import tinymongo as tm
import os

db_name = 'demo_db'
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

# insert data adds a new record returns _id
recordId = tinyCollection.insert({"username": "admin", "password": "admin", "module": "somemodule"})
userInfo = tinyCollection.find_one({"_id": recordId})  # returns the record inserted
print('find one result: '.format())

#cursor = tinyCollection.find({})
#for c in cursor:
#    print(c['username'])

# update data returns boolean if successful
upd = tinyCollection.update({"username": "admin"}, {"$set": {"module": "someothermodule"}})

