import os
import logging
import copy

from tinydb import *
from operator import itemgetter
import operator
from uuid import uuid1
from bson.objectid import ObjectId

logger = logging.getLogger(__name__)


class TinyMongoClient(object):
    def __init__(self, foldername="tinydb"):
        self.foldername = foldername
        try:
            os.mkdir(foldername)
        except FileExistsError:
            pass

    def __getitem__(self, key):
        return TinyMongoDatabase(key, self.foldername)

    def close(self):
        pass

    def __getattr__(self, name):
        return TinyMongoDatabase(name, self.foldername)


class TinyMongoDatabase(object):
    def __init__(self, database, foldername):
        self.foldername = foldername
        self.tinydb = TinyDB(os.path.join(foldername, database + ".json"))

    def __getattr__(self, name):
        return TinyMongoCollection(name, self)


class TinyMongoCollection(object):
    """
    This class represents a collection and all of the operations that are commonly performed on a collection
    """

    def __init__(self, table, parent=None):
        """
        Initilialize the collection

        :param table: the table name
        :param parent: the parent db name
        """
        self.tablename = table
        self.table = None
        self.parent = parent

    def __getattr__(self, name):
        """

        :param name:
        :return:
        """
        if self.table is None:
            self.tablename += "." + name
        return self

    def build_table(self):
        """
        Builds a new tinydb table at the parent database
        :return:
        """
        self.table = self.parent.tinydb.table(self.tablename)

    def insert_one(self, doc):
        """
        Inserts one document into the collection
        :param doc: the document
        :return: the ids of the documents that were inserted
        """
        if self.table is None:
            self.build_table()

        if not isinstance(doc, dict):
            return 0

        if not "_id" in doc:
            theid = str(uuid1()).replace("-", "")
            eid = theid
            doc["_id"] = theid
        else:
            eid = doc["_id"]

        self.table.insert(doc)

        # todo: return the result object with 'inserted_id' property
        return eid

    def insert_many(self, docs):
        """
        Inserts several documents into the collection
        :param docs: a list of documents
        :return:
        """
        if self.table is None:
            self.build_table()

        if not isinstance(docs, list):
            return []

        eids = []
        for doc in docs:
            eid = self.insert_one(doc)
            eids.append(eid)

        return eids

    def parse_query(self, query):
        logger.debug('query to parse2: {}'.format(query))

        # this should find all records
        if query == {}:
            return (Query()._id != '-1')

        q = None
        # find the final result of the generator
        for c in self.parse_condition(query):
            q = c

        logger.debug('new query item2: {}'.format(q))

        return q

    def parse_condition(self, query, prev_key=None, conditions=None):
        # use this to determine gt/lt/eq on prev_query
        logger.debug('query: {} prev_query: {}'.format(query, prev_key))

        q = Query()

        # deal with the {'name': value} case by injecting a previous key
        if not prev_key:
            temp_query = copy.deepcopy(query)
            k, v = temp_query.popitem()
            prev_key = k

        # deal with the conditions
        for key, value in query.items():
            logger.debug('conditions: {} {}'.format(key, value))

            if key == '$gte':
                conditions = (q[prev_key] >= value) if not conditions else (conditions & (q[prev_key] >= value))
            elif key == '$gt':
                conditions = (q[prev_key] > value) if not conditions else (conditions & (q[prev_key] > value))
            elif key == '$lte':
                conditions = (q[prev_key] <= value) if not conditions else (conditions & (q[prev_key] <= value))
            elif key == '$lt':
                conditions = (q[prev_key] < value) if not conditions else (conditions & (q[prev_key] < value))
            else:
                conditions = (q[prev_key] == value) if not conditions else (conditions & (q[prev_key] == value))

            logger.debug('c: {}'.format(conditions))
            if isinstance(value, dict):
                yield from self.parse_condition(value, key)
            else:
                yield conditions

    def update_one(self, query, data, argsdict={}, **kwargs):
        if self.table is None:
            self.build_table()

        if "$set" in data:
            data = data["$set"]

        allcond = self.parse_query(query)

        try:
            self.table.update(data, allcond)
        except:
            # todo: exception too broad
            return False

        # todo: return result with result.matched_count and result.modified_count
        return True

    def find(self, query=None):
        if self.table is None:
            self.build_table()

        allcond = self.parse_query(query)

        if allcond is None:
            return TinyMongoCursor(self.table.all())

        return TinyMongoCursor(self.table.search(allcond))

    def find_one(self, query=None):
        if self.table is None:
            self.build_table()

        allcond = self.parse_query(query)

        if allcond is None:
            return self.table.get(eid=1)

        return self.table.get(allcond)

    def count(self):
        if self.table is None:
            self.build_table()

        return len(self.table)

    def delete_one(self, query):
        item = self.find_one(query)
        self.table.remove(where('_id') == item['_id'])

        return None

    def delete_many(self, query):
        items = self.find(query)
        for item in items:
            self.table.remove(where('_id') == item['_id'])


class TinyMongoCursor(object):
    def __init__(self, cursordat):
        self.cursordat = cursordat
        self.cursorpos = 0
        if type(self.cursordat) is list:
            if len(self.cursordat) == 0:
                self.currentrec = None
            else:
                self.currentrec = self.cursordat[self.cursorpos]
        else:
            self.currentrec = self.cursordat

    def __getitem__(self, key):
        if type(key) is int:
            return self.cursordat[key]
        return self.currentrec[key]

    def __contains__(self, item):
        if self.currentrec is None:
            return False
        if item in self.currentrec:
            return True
        return False

    def sort(self, field, direction=1):
        if not type(self.cursordat) is list:
            pass
        elif direction == -1:
            self.cursordat = sorted(self.cursordat, key=itemgetter(field), reverse=True)
        else:
            self.cursordat = sorted(self.cursordat, key=itemgetter(field))
        return self

    def next(self):
        self.cursorpos += 1
        self.currentrec = self.cursordat[self.cursorpos]

    def count(self):
        return len(self.cursordat)


class TinyGridFS(object):
    def __init__(self, *args, **kwargs):
        self.database = None

    def GridFS(self, tinydatabase):
        self.database = tinydatabase
        return self
