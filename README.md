
* Master Branch [![Build Status](https://travis-ci.org/jjonesAtMoog/tinymongo.svg?branch=master)](https://travis-ci.org/jjonesAtMoog/tinymongo)
* Develop Branch [![Build Status](https://travis-ci.org/jjonesAtMoog/tinymongo.svg?branch=develop)](https://travis-ci.org/jjonesAtMoog/tinymongo)

# Purpose

A simple wrapper to make a drop in replacement for mongodb out of
[tinydb](http://tinydb.readthedocs.io/en/latest/).  This module is an
attempt to add an interface familiar to those currently using pymongo.

# Status

Unit testing is currently being worked on and functionality is being
added to the library.  Current coverage is 79%.  Current builds tested
on versions 3.3+.

# Installation

The latest release can be installed via `pip install tinymongo`.  The
library is currently under rapid development and a more recent version
may be desired.  In this case, simply clone this repository, navigate
to the root project directory, and `python setup.py install`.  This
is a pure python distribution and - thus - should require no external
compilers or tools besides those contained within Python itself.

# Examples

The quick start is shown below.  For a more detailed look at tinymongo,
take a look at demo.py within the repository.

    from tinymongo import *
    
    # you can include a folder name as a parameter if not it will default to "tinydb"
    tinyClient = TinyMongoClient()
    
    # either creates a new database file or accesses an existing one
    tinyDatabase = tinyClient.tinyDatabase
    
    # either creates a new collection or accesses an existing one
    tinyCollection = tinyDatabase.tinyCollection
    
    #insert data adds a new record returns _id
    recordId = tinyCollection.insert_one({"username":"admin","password":"admin","module":"somemodule"})
    userInfo = tinyCollection.find_one({"_id":recordId})  # returns the record inserted
    
    #update data returns True if successful and False if unsuccessful
    upd = table.update_one({"username":"admin"},{"$set":{"module":"someothermodule"}) 

# Contributions

Contributions are welcome!  Currently, the most valuable contributions
would be:

 * adding test cases
 * adding functionality consistent with pymongo
 * documentation
 * identifying bugs and issues

# Future Development

I will also be adding support for gridFS by storing the files somehow and indexing them in a db like mongo currently does

More to come......

# License

MIT License
