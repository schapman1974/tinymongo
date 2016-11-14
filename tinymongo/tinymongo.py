import os
import logging

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
    def __init__(self, table, parent=None):
        self.tablename = table
        self.table = None
        self.parent = parent
        self.query = Query()
        self.insert_one = self.insert
        self.insert_many = self.insert
        self.update_one = self.update
        self.update_many = self.update

    def __getattr__(self, name):
        if self.table is None:
            self.tablename += "." + name
        return self

    def buildTable(self):
        self.table = self.parent.tinydb.table(self.tablename)

    def insert(self, data, **kwargs):
        if self.table is None: self.buildTable()
        if not type(data) is list: data = [data]
        eids = []
        for adat in data:
            if not "_id" in adat:
                theid = str(uuid1()).replace("-", "")
                eids.append(theid)
                adat["_id"] = theid
            else:
                eids.append(adat["_id"])
            self.table.insert(adat)
        if len(eids) == 1:
            return eids[0]
        return eids

    def parseQuery(self, query):
        logger.debug('query to parse: {}'.format(query))

        if self.table is None:
            self.buildTable()

        cnt = 0
        allcond = None
        for akey, avalue in query.items():
            # set the operator
            if type(avalue) == dict:
                theop = operator.__dict__[avalue.keys()[0]]
                avalue = avalue.values()[0]
            elif "ObjectId" in str(type(avalue)):
                theop = operator.eq
                avalue = str(avalue)
            # defalt to equals
            else:
                theop = operator.eq

            if cnt == 0:
                allcond = (self.query[akey] == avalue)
            else:
                allcond = allcond & (theop(self.query[akey], avalue))
            cnt += 1

        logger.debug('all conditions from parsed query: {}'.format(allcond))
        return allcond

    def parseQuery2(self, query):
        logger.debug('query to parse2: {}'.format(query))

        q = Query()
        c = []

        for key, value in query.items():
            if isinstance(value, dict):
                logger.debug('the value {} is a dict'.format(value))
                for k, v in value.items():
                    logger.debug('k: {} v: {}'.format(k, v))
                    if k in ['$gte', '$gt', '$lte', 'lt']:
                        cond = self.parse_numeric_condition(value, key)
                        logger.debug('cond: {}'.format(cond))
                        c.append(cond)


        logger.debug('new query item: {}, {}'.format(q, c))

        return q

    def parse_numeric_condition(self, query, prev_key):
        # use this to determine gt/lt/eq on prev_query
        logger.debug('query: {} prev_query: {}'.format(query, prev_key))

        q = Query()
        c = None

        for key, value in query.items():
            logger.debug('conditions: {} {}'.format(key, value))

            if key == '$gte':
                c = (q[prev_key] >= value) if not c else (c & (q[prev_key] >= value))
            elif key == '$gt':
                c = (q[prev_key] > value) if not c else (c & (q[prev_key] > value))
            elif key == '$lte':
                c = (q[prev_key] <= value) if not c else (c & (q[prev_key] <= value))
            elif key == '$lt':
                c = (q[prev_key] < value) if not c else (c & (q[prev_key] < value))
            else:
                c = (q[prev_key] == value) if not c else (c & (q[prev_key] == value))

        logger.debug('c: {}'.format(c))
        return c

    def update(self, query, data, argsdict={}, **kwargs):
        if self.table is None:
            self.buildTable()

        if "$set" in data:
            data = data["$set"]

        allcond = self.parseQuery(query)

        try:
            self.table.update(data, allcond)
        except:
            # todo: exception too broad
            return False

        return True

    def find(self, query=None):
        if self.table is None:
            self.buildTable()

        allcond = self.parseQuery(query)

        if allcond is None:
            return TinyMongoCursor(self.table.all())

        return TinyMongoCursor(self.table.search(allcond))

    def find_one(self, query=None):
        if self.table is None:
            self.buildTable()

        allcond = self.parseQuery(query)

        if allcond is None:
            return self.table.get(eid=1)

        return self.table.get(allcond)

    def count(self):
        if self.table is None:
            self.buildTable()

        return len(self.table)


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
