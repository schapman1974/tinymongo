# tinymongo
A simple wrapper to make a drop in raplacement for mongodb out of TinyDB

Current Requirements:
   TinyDB  https://github.com/msiemens/tinydb

This module is to attempt to add support for mongodb old and newer versions.  Currently I have tested side by side with  version 3.2.4 of pymongo for this version.  Most likely there are a few bugs in this alpha version so work with me.  I do have it working on a small project I am working on.

Example:
```python
    from tinymongo.tinymongo import *
    
    # you can include a folder name as a parameter if not it will default to "tinydb"
    tinyClient = TinyMongoClient()
    
    # either creates a new database file or accesses an existing one
    tinyDatabase = tinyClient.tinyDatabase
    
    # either creates a new collection or accesses an existing one
    tinyCollection = tinyDatabase.tinyCollection
    
    #insert data adds a new record returns _id
    recordId = tinyCollection.insert({"username":"admin","password":"admin","module":"somemodule"}) 
    userInfo = tinyCollection.find_one({"_id":recordId})  # returns the record inserted
    
    #update data returns True if successful and False if unsuccessful
    upd = table.update({"username":"admin"},{"$set":{"module":"someothermodule"}) 
```

I will also be adding support for gridFS by storing the files somehow and indexing them in a db like mongo currently does

More to come......

# License

MIT License
