from __future__ import absolute_import
import os
import logging
import copy

from tinydb import *
from operator import itemgetter
from uuid import uuid1

logger = logging.getLogger(__name__)


class TinyMongoClient(object):
    def __init__(self, foldername=u"tinydb"):
        self.foldername = foldername
        try:
            os.mkdir(foldername)
        except OSError as x:
            logger.warning('{}'.format(x))

    def __getitem__(self, key):
        return TinyMongoDatabase(key, self.foldername)

    def close(self):
        pass

    def __getattr__(self, name):
        return TinyMongoDatabase(name, self.foldername)


class TinyMongoDatabase(object):
    def __init__(self, database, foldername):
        self.foldername = foldername
        self.tinydb = TinyDB(os.path.join(foldername, database + u".json"))

    def __getattr__(self, name):
        return TinyMongoCollection(name, self)


class TinyMongoCollection(object):
    u"""
    This class represents a collection and all of the operations that are commonly performed on a collection
    """

    def __init__(self, table, parent=None):
        u"""
        Initilialize the collection

        :param table: the table name
        :param parent: the parent db name
        """
        self.tablename = table
        self.table = None
        self.parent = parent

    def __getattr__(self, name):
        u"""

        :param name:
        :return:
        """
        if self.table is None:
            self.tablename += u"." + name
        return self

    def build_table(self):
        u"""
        Builds a new tinydb table at the parent database
        :return:
        """
        self.table = self.parent.tinydb.table(self.tablename)

    def insert_one(self, doc):
        u"""
        Inserts one document into the collection
        :param doc: the document
        :return: the ids of the documents that were inserted
        """
        if self.table is None:
            self.build_table()

        if not isinstance(doc, dict):
            raise ValueError(u'"doc" must be a dict')

        try:
            theid = unicode(uuid1()).replace(u"-", u"")
        except NameError:
            theid = str(uuid1()).replace(u"-", u"")

        eid = theid
        doc[u"_id"] = theid

        self.table.insert(doc)

        # todo: return the result object with 'inserted_id' property
        return eid

    def insert_many(self, docs):
        u"""
        Inserts several documents into the collection
        :param docs: a list of documents
        :return:
        """
        if self.table is None:
            self.build_table()

        if not isinstance(docs, list):
            raise ValueError(u'"insert_many" requires a list input')

        eids = []
        for doc in docs:
            eid = self.insert_one(doc)
            eids.append(eid)

        return eids

    def parse_query(self, query):
        u"""
        Creates a tinydb Query() object from the query dict

        :param query: object containing the dictionary representation of the query
        :return: composite Query()
        """
        logger.debug(u'query to parse2: {}'.format(query))

        # this should find all records
        if query == {} or query is None:
            return (Query()._id != u'-1')

        q = None
        # find the final result of the generator
        for c in self.parse_condition(query):
            q = c

        logger.debug(u'new query item2: {}'.format(q))

        return q

    def parse_condition(self, query, prev_key=None):
        u"""
        Creates a recursive generator for parsing some types of Query() conditions

        :param query: Query object
        :param prev_key: The key at the next-higher level
        :return: generator object, the last of which will be the complete Query() object containing all conditions
        """
        # use this to determine gt/lt/eq on prev_query
        logger.debug(u'query: {} prev_query: {}'.format(query, prev_key))

        q = Query()
        conditions = None

        # deal with the {'name': value} case by injecting a previous key
        if not prev_key:
            temp_query = copy.deepcopy(query)
            k, v = temp_query.popitem()
            prev_key = k

        # deal with the conditions
        for key, value in query.items():
            logger.debug(u'conditions: {} {}'.format(key, value))

            if key == u'$gte':
                conditions = (q[prev_key] >= value) if not conditions else (conditions & (q[prev_key] >= value))
            elif key == u'$gt':
                conditions = (q[prev_key] > value) if not conditions else (conditions & (q[prev_key] > value))
            elif key == u'$lte':
                conditions = (q[prev_key] <= value) if not conditions else (conditions & (q[prev_key] <= value))
            elif key == u'$lt':
                conditions = (q[prev_key] < value) if not conditions else (conditions & (q[prev_key] < value))
            elif key == u'$ne':
                conditions = (q[prev_key] != value) if not conditions else (conditions & (q[prev_key] != value))
            elif key == u'$and':
                pass
            else:
                #conditions = (q[prev_key] == value) if not conditions else (conditions & (q[prev_key] == value))
                #dont want to use the previous key if this is a secondary key (fixes multiple item query that includes $ codes)
                conditions = (q[key] == value) if not conditions else (conditions & (q[key] == value))
                prev_key = key

            logger.debug(u'c: {}'.format(conditions))
            if isinstance(value, dict):
                #yield from self.parse_condition(value, key)
                for parse_condition in self.parse_condition(value, key):
                    yield parse_condition
            elif isinstance(value,list) and key=="$and":
                for spec in value:
                    for parse_condition in self.parse_condition(spec, key):
                        yield parse_condition
            else:
                yield conditions

    def update_one(self, query, doc):
        u"""
        Updates one element of the collection

        :param query: dictionary representing the mongo query
        :param doc: dictionary representing the item to be updated
        :return:
        """
        if self.table is None:
            self.build_table()

        if u"$set" in doc:
            doc = doc[u"$set"]

        allcond = self.parse_query(query)

        try:
            self.table.update(doc, allcond)
        except:
            # todo: exception too broad
            return False

        # todo: return result with result.matched_count and result.modified_count
        return True

    def find(self, query=None):
        u"""
        Finds all matching results

        :param query: dictionary representing the mongo query
        :return: cursor containing the search results
        """
        if self.table is None:
            self.build_table()

        allcond = self.parse_query(query)

        return TinyMongoCursor(self.table.search(allcond))

    def find_one(self, query=None):
        u"""
        Finds one matching query element

        :param query: dictionary representing the mongo query
        :return: the resulting document (if found)
        """

        if self.table is None:
            self.build_table()

        allcond = self.parse_query(query)

        return self.table.get(allcond)

    def delete_one(self, query):
        u"""
        Deletes one document from the collection

        :param query: dictionary representing the mongo query
        :return: None
        """
        item = self.find_one(query)
        self.table.remove(where(u'_id') == item[u'_id'])

        return None

    def delete_many(self, query):
        u"""
        Removes all items matching the mongo query

        :param query: dictionary representing the mongo query
        :return:
        """
        items = self.find(query)
        for item in items:
            self.table.remove(where(u'_id') == item[u'_id'])


class TinyMongoCursor(object):
    def __init__(self, cursordat):
        self.cursordat = cursordat
        self.cursorpos = -1

        if len(self.cursordat) == 0:
            self.currentrec = None
        else:
            self.currentrec = self.cursordat[self.cursorpos]

    def __getitem__(self, key):
        if isinstance(key, int):
            return self.cursordat[key]
        return self.currentrec[key]

    def sort(self, sort_specifier):
        u"""
        Sorts a cursor object based on the input

        :param sort_specifier: a dict containing the sort specification, i.e. {'user_number': -1}
        :return:
        """
        # todo: make this method able to read multiple sort_specifiers (currently only reads one)

        if not isinstance(sort_specifier, dict):
            raise ValueError(u'invalid field specifier, must be a dict')

        f = None
        for item in sort_specifier.keys():
            f = item
        direction = sort_specifier[f]

        if direction == -1:
            self.cursordat = sorted(self.cursordat, key=itemgetter(f), reverse=True)
            logger.debug(u'sort (reverse) based on {}'.format(f))
        else:
            self.cursordat = sorted(self.cursordat, key=itemgetter(f))
            logger.debug(u'sort based on {}'.format(f))

        return self

    def hasNext(self):
        u"""
        Returns True if the cursor has a next position, False if not
        :return:
        """
        cursor_pos = self.cursorpos + 1

        try:
            self.cursordat[cursor_pos]
            return True
        except IndexError:
            return False

    def next(self):
        u"""
        Returns the next record

        :return:
        """
        self.cursorpos += 1
        return self.cursordat[self.cursorpos]

    def count(self):
        u"""
        Returns the number of records in the current cursor

        :return: number of records
        """
        return len(self.cursordat)


class TinyGridFS(object):
    def __init__(self, *args, **kwargs):
        self.database = None

    def GridFS(self, tinydatabase):
        self.database = tinydatabase
        return self
