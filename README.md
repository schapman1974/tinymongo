# tinymongo
A quick and dirty wrapper to make a drop in raplacement for mongodb out of tinyDB

This module is to attempt to add support for mongodb old and newer versions.  Currently I have tested against version 3.2.4 of pymongo for this version.  Most likely there are plenty of bugs in this alpha version so work with me.

Example:

    tinyClient = TinyMongoClient()  # you can include a folder name as a parameter if not it will default to "tinydb"
    tinyDatabase = tinyClient.tinyDatabase        # either creates a new database file or accesses an existing one
    tinyCollection = tinyDatabase.tinyCollection  # either creates a new collection or access an existing one
    #insert data
    # adds a new record returns _id
    recordId = tinyCollection.insert({"username":"admin","password":"admin","module":"somemodule"}) 
    userInfo = tinyCollection.find_one({"_id":recordId})  # returns the record inserted
    print(userInfo)
    #update data
    upd = table.update({"username":"admin"},{"$set":{"module":"someothermodule"}) #returns boolean if successful

    
I plan on also adding gridFS file support very shortly as well
