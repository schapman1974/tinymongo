import tinymongo as tm

try:
    os.remove("tinydb/pacemain.json")
    os.remove("tinydb/pacelog.json")
except:
    pass
# you can include a folder name as a parameter if not it will default to "tinydb"
tinyClient = tm.TinyMongoClient()

# either creates a new database file or accesses an existing one
tinyDatabase = tinyClient.tinyDatabase

# either creates a new collection or accesses an existing one
tinyCollection = tinyDatabase.tinyCollection

# insert data adds a new record returns _id
recordId = tinyCollection.insert({"username": "admin", "password": "admin", "module": "somemodule"})
userInfo = tinyCollection.find_one({"_id": recordId})  # returns the record inserted

# update data returns boolean if successful
upd = tinyCollection.update({"username": "admin"}, {"$set": {"module": "someothermodule"}})