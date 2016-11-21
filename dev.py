import tinymongo as tm
import logging
import os


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)

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

# testing find
tinyCollection.query = tm.Query()
item = tinyCollection.find().sort({'user_number': -1})

for i in item:
    print(i)
