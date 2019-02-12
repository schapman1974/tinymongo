"""Acts like a Pymongo client to TinyDB"""
# coding: utf-8

from __future__ import absolute_import

import copy
import logging
import os
from math import ceil
from operator import itemgetter
from uuid import uuid1

from tinydb import Query, TinyDB, where
from .results import (
    InsertOneResult,
    InsertManyResult,
    UpdateResult,
    DeleteResult
)
from .errors import DuplicateKeyError

try:
  basestring
except NameError:
  basestring = str


logger = logging.getLogger(__name__)


class TinyMongoClient(object):
    """Represents the Tiny `db` client"""
    def __init__(self, foldername=u"tinydb", **kwargs):
        """Initialize container folder"""
        self._foldername = foldername
        try:
            os.mkdir(foldername)
        except OSError as x:
            logger.info('{}'.format(x))

    @property
    def _storage(self):
        """By default return Tiny.DEFAULT_STORAGE and can be overwritten to
        return custom storages and middlewares.

            class CustomClient(TinyMongoClient):
                @property
                def _storage(self):
                    return CachingMiddleware(OtherMiddleware(JSONMiddleware))

        This property is also useful to define Serializers using required
        `tinydb-serialization` module.

            from tinymongo.serializers import DateTimeSerializer
            from tinydb_serialization import SerializationMiddleware
            class CustomClient(TinyMongoClient):
                @property
                def _storage(self):
                    serialization = SerializationMiddleware()
                    serialization.register_serializer(
                        DateTimeSerializer(), 'TinyDate')
                    # register other custom serializers
                    return serialization

        """
        return TinyDB.DEFAULT_STORAGE

    def __getitem__(self, key):
        """Gets a new or existing database based in key"""
        return TinyMongoDatabase(key, self._foldername, self._storage)

    def close(self):
        """Do nothing"""
        pass

    def __getattr__(self, name):
        """Gets a new or existing database based in attribute"""
        return TinyMongoDatabase(name, self._foldername, self._storage)


class TinyMongoDatabase(object):
    """Representation of a Pymongo database"""
    def __init__(self, database, foldername, storage):
        """Initialize a TinyDB file named as the db name in the given folder
        """
        self._foldername = foldername
        self.tinydb = TinyDB(
            os.path.join(foldername, database + u".json"),
            storage=storage
        )

    def __getattr__(self, name):
        """Gets a new or existing collection"""
        return TinyMongoCollection(name, self)

    def __getitem__(self, name):
        """Gets a new or existing collection"""
        return TinyMongoCollection(name, self)

    def collection_names(self):
        """Get a list of all the collection names in this database"""
        return list(self.tinydb.tables())


class TinyMongoCollection(object):
    """
    This class represents a collection and all of the operations that are
    commonly performed on a collection
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

    def __repr__(self):
        """Return collection name"""
        return self.tablename

    def __getattr__(self, name):
        """
        If attr is not found return self
        :param name:
        :return:
        """
        # if self.table is None:
        #     self.tablename += u"." + name
        if self.table is None:
            self.build_table()
        return self

    def build_table(self):
        """
        Builds a new tinydb table at the parent database
        :return:
        """
        self.table = self.parent.tinydb.table(self.tablename)

    def count(self):
        """
        Counts the documents in the collection.
        :return: Integer representing the number of documents in the collection.
        """
        return self.find().count()

    def insert(self, docs, *args, **kwargs):
        """Backwards compatibility with insert"""
        if isinstance(docs, list):
            return self.insert_many(docs, *args, **kwargs)
        else:
            return self.insert_one(docs, *args, **kwargs)

    def insert_one(self, doc, *args, **kwargs):
        """
        Inserts one document into the collection
        If contains '_id' key it is used, else it is generated.
        :param doc: the document
        :return: InsertOneResult
        """
        if self.table is None:
            self.build_table()

        if not isinstance(doc, dict):
            raise ValueError(u'"doc" must be a dict')

        _id = doc[u'_id'] = doc.get('_id') or generate_id()

        bypass_document_validation = kwargs.get('bypass_document_validation')
        if bypass_document_validation is True:
            # insert doc without validation of duplicated `_id`
            eid = self.table.insert(doc)
        else:
            existing = self.find_one({'_id': _id})
            if existing is None:
                eid = self.table.insert(doc)
            else:
                raise DuplicateKeyError(
                    u'_id:{0} already exists in collection:{1}'.format(
                        _id, self.tablename
                    )
                )

        return InsertOneResult(eid=eid, inserted_id=_id)

    def insert_many(self, docs, *args, **kwargs):
        """
        Inserts several documents into the collection
        :param docs: a list of documents
        :return: InsertManyResult
        """
        if self.table is None:
            self.build_table()

        if not isinstance(docs, list):
            raise ValueError(u'"insert_many" requires a list input')

        bypass_document_validation = kwargs.get('bypass_document_validation')

        if bypass_document_validation is not True:
            # get all _id in once, to reduce I/O. (without projection)
            existing = [doc['_id'] for doc in self.find({})]

        _ids = list()
        for doc in docs:

            _id = doc[u'_id'] = doc.get('_id') or generate_id()

            if bypass_document_validation is not True:
                if _id in existing:
                    raise DuplicateKeyError(
                        u'_id:{0} already exists in collection:{1}'.format(
                            _id, self.tablename
                        )
                    )
                existing.append(_id)

            _ids.append(_id)

        results = self.table.insert_multiple(docs)

        return InsertManyResult(
            eids=[eid for eid in results],
            inserted_ids=[inserted_id for inserted_id in _ids]
        )

    def parse_query(self, query):
        """
        Creates a tinydb Query() object from the query dict

        :param query: object containing the dictionary representation of the
        query
        :return: composite Query()
        """
        logger.debug(u'query to parse2: {}'.format(query))

        # this should find all records
        if query == {} or query is None:
            return Query()._id != u'-1'  # noqa

        q = None
        # find the final result of the generator
        for c in self.parse_condition(query):
            if q is None:
                q = c
            else:
                q = q & c

        logger.debug(u'new query item2: {}'.format(q))

        return q

    def parse_condition(self, query, prev_key=None, last_prev_key=None):
        """
        Creates a recursive generator for parsing some types of Query()
        conditions

        :param query: Query object
        :param prev_key: The key at the next-higher level
        :return: generator object, the last of which will be the complete
        Query() object containing all conditions
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
                conditions = (
                    q[prev_key] >= value
                ) if not conditions and prev_key != "$not" \
                else (conditions & (q[prev_key] >= value)) if prev_key != "$not" \
                else (q[last_prev_key] < value)
            elif key == u'$gt':
                conditions = (
                    q[prev_key] > value
                ) if not conditions and prev_key != "$not" \
                else (conditions & (q[prev_key] > value)) if prev_key != "$not" \
                else (q[last_prev_key] <= value)
            elif key == u'$lte':
                conditions = (
                    q[prev_key] <= value
                ) if not conditions and prev_key != "$not" \
                else (conditions & (q[prev_key] <= value)) if prev_key != "$not" \
                else (q[last_prev_key] > value)
            elif key == u'$lt':
                conditions = (
                    q[prev_key] < value
                ) if not conditions and prev_key != "$not" \
                else (conditions & (q[prev_key] < value)) if prev_key != "$not" \
                else (q[last_prev_key] >= value)
            elif key == u'$ne':
                conditions = (
                    q[prev_key] != value
                ) if not conditions and prev_key != "$not" \
                else (conditions & (q[prev_key] != value))if prev_key != "$not" \
                else (q[last_prev_key] == value)
            elif key == u'$not':
                if not isinstance(value, dict) and not isinstance(value, list):
                    conditions = (
                        q[prev_key] != value
                    ) if not conditions and prev_key != "$not" \
                    else (conditions & (q[prev_key] != value)) \
                    if prev_key != "$not" else (q[last_prev_key] >= value)
                else:
                    # let the value's condition be parsed below
                    pass
            elif key == u'$regex':
                value = value.replace('\\\\\\', '|||')
                value = value.replace('\\\\', '|||')
                regex = value.replace('\\', '')
                regex = regex.replace('|||', '\\')
                currCond = (where(prev_key).matches(regex))
                conditions = currCond if not conditions else (conditions & currCond)
            elif key in ['$and', '$or', '$in', '$all']:
                pass
            else:
                # don't want to use the previous key if this is a secondary key
                # (fixes multiple item query that includes $ codes)
                if not isinstance(value, dict) and not isinstance(value, list):
                    conditions = (
                        (q[key] == value) | (q[key].any([value]))
                    ) if not conditions else (conditions & ((q[key] == value) | (q[key].any([value]))))
                    prev_key = key

            logger.debug(u'c: {}'.format(conditions))
            if isinstance(value, dict):
                # yield from self.parse_condition(value, key)
                for parse_condition in self.parse_condition(value, key, prev_key):
                    yield parse_condition
            elif isinstance(value, list):
                if key == '$and':
                    grouped_conditions = None
                    for spec in value:
                        for parse_condition in self.parse_condition(spec):
                            grouped_conditions = (
                                parse_condition
                                if not grouped_conditions
                                else grouped_conditions & parse_condition
                            )
                    yield grouped_conditions
                elif key == '$or':
                    grouped_conditions = None
                    for spec in value:
                        for parse_condition in self.parse_condition(spec):
                            grouped_conditions = (
                                parse_condition
                                if not grouped_conditions
                                else grouped_conditions | parse_condition
                            )
                    yield grouped_conditions
                elif key == '$in':
                    # use `any` to find with list, before comparing to single string
                    grouped_conditions = q[prev_key].any(value)
                    for val in value:
                        for parse_condition in self.parse_condition({prev_key : val}):
                            grouped_conditions = (
                                parse_condition
                                if not grouped_conditions
                                else grouped_conditions | parse_condition
                            )
                    yield grouped_conditions
                elif key == '$all':
                    yield q[prev_key].all(value)
                else:
                    yield q[prev_key].any([value])
            else:
                yield conditions

    def update(self, query, doc, *args, **kwargs):
        """BAckwards compatibility with update"""
        if isinstance(doc, list):
            return [
                self.update_one(query, item, *args, **kwargs)
                for item in doc
            ]
        else:
            return self.update_one(query, doc, *args, **kwargs)

    def update_one(self, query, doc):
        """
        Updates one element of the collection

        :param query: dictionary representing the mongo query
        :param doc: dictionary representing the item to be updated
        :return: UpdateResult
        """
        if self.table is None:
            self.build_table()

        if u"$set" in doc:
            doc = doc[u"$set"]

        allcond = self.parse_query(query)

        try:
            result = self.table.update(doc, allcond)
        except:
            # TODO: check table.update result
            # check what pymongo does in that case
            result = None

        return UpdateResult(raw_result=result)

    def find(self, filter=None, sort=None, skip=None, limit=None,
             *args, **kwargs):
        """
        Finds all matching results

        :param query: dictionary representing the mongo query
        :return: cursor containing the search results
        """
        if self.table is None:
            self.build_table()

        if filter is None:
            result = self.table.all()
        else:
            allcond = self.parse_query(filter)

            try:
                result = self.table.search(allcond)
            except (AttributeError, TypeError):
                result = []

        result = TinyMongoCursor(
            result,
            sort=sort,
            skip=skip,
            limit=limit
        )

        return result

    def find_one(self, filter=None):
        """
        Finds one matching query element

        :param query: dictionary representing the mongo query
        :return: the resulting document (if found)
        """

        if self.table is None:
            self.build_table()

        allcond = self.parse_query(filter)

        return self.table.get(allcond)

    def remove(self, spec_or_id, multi=True, *args, **kwargs):
        """Backwards compatibility with remove"""
        if multi:
            return self.delete_many(spec_or_id)
        return self.delete_one(spec_or_id)

    def delete_one(self, query):
        """
        Deletes one document from the collection

        :param query: dictionary representing the mongo query
        :return: DeleteResult
        """
        item = self.find_one(query)
        result = self.table.remove(where(u'_id') == item[u'_id'])

        return DeleteResult(raw_result=result)

    def delete_many(self, query):
        """
        Removes all items matching the mongo query

        :param query: dictionary representing the mongo query
        :return: DeleteResult
        """
        items = self.find(query)
        result = [
            self.table.remove(where(u'_id') == item[u'_id'])
            for item in items
        ]

        if query == {}:
            # need to reset TinyDB's index for docs order consistency
            self.table._last_id = 0

        return DeleteResult(raw_result=result)


class TinyMongoCursor(object):
    """Mongo iterable cursor"""

    def __init__(self, cursordat, sort=None, skip=None, limit=None):
        """Initialize the mongo iterable cursor with data"""
        self.cursordat = cursordat
        self.cursorpos = -1

        if len(self.cursordat) == 0:
            self.currentrec = None
        else:
            self.currentrec = self.cursordat[self.cursorpos]

        if sort:
            self.sort(sort)

        self.paginate(skip, limit)

    def __getitem__(self, key):
        """Gets record by index or value by key"""
        if isinstance(key, int):
            return self.cursordat[key]
        return self.currentrec[key]

    def paginate(self, skip, limit):
        """Paginate list of records"""
        if not self.count() or not limit:
            return
        skip = skip or 0
        pages = int(ceil(self.count() / float(limit)))
        limits = {}
        last = 0
        for i in range(pages):
            current = limit * i
            limits[last] = current
            last = current
        # example with count == 62
        # {0: 20, 20: 40, 40: 60, 60: 62}
        if limit and limit < self.count():
            limit = limits.get(skip, self.count())
            self.cursordat = self.cursordat[skip: limit]

    def _order(self, value, is_reverse=None):
        """Parsing data to a sortable form
        By giving each data type an ID(int), and assemble with the value
        into a sortable tuple.
        """

        def _dict_parser(dict_doc):
            """ dict ordered by:
            valueType_N -> key_N -> value_N
            """
            result = list()
            for key in dict_doc:
                data = self._order(dict_doc[key])
                res = (data[0], key, data[1])
                result.append(res)
            return tuple(result)

        def _list_parser(list_doc):
            """list will iter members to compare
            """
            result = list()
            for member in list_doc:
                result.append(self._order(member))
            return result

        # (TODO) include more data type
        if value is None or not isinstance(value, (dict,
                                                   list,
                                                   basestring,
                                                   bool,
                                                   float,
                                                   int)):
            # not support/sortable value type
            value = (0, None)

        elif isinstance(value, bool):
            value = (5, value)

        elif isinstance(value, (int, float)):
            value = (1, value)

        elif isinstance(value, basestring):
            value = (2, value)

        elif isinstance(value, dict):
            value = (3, _dict_parser(value))

        elif isinstance(value, list):
            if len(value) == 0:
                # [] less then None
                value = [(-1, [])]
            else:
                value = _list_parser(value)

            if is_reverse is not None:
                # list will firstly compare with other doc by it's smallest
                # or largest member
                value = max(value) if is_reverse else min(value)
            else:
                # if the smallest or largest member is a list
                # then compaer with it's sub-member in list index order
                value = (4, tuple(value))

        return value

    def sort(self, key_or_list, direction=None):
        """
        Sorts a cursor object based on the input

        :param key_or_list: a list/tuple containing the sort specification,
        i.e. ('user_number': -1), or a basestring
        :param direction: sorting direction, 1 or -1, needed if key_or_list
                          is a basestring
        :return:
        """

        # checking input format

        sort_specifier = list()
        if isinstance(key_or_list, list):
            if direction is not None:
                raise ValueError('direction can not be set separately '
                                 'if sorting by multiple fields.')
            for pair in key_or_list:
                if not (isinstance(pair, list) or isinstance(pair, tuple)):
                    raise TypeError('key pair should be a list or tuple.')
                if not len(pair) == 2:
                    raise ValueError('Need to be (key, direction) pair')
                if not isinstance(pair[0], basestring):
                    raise TypeError('first item in each key pair must '
                                    'be a string')
                if not isinstance(pair[1], int) or not abs(pair[1]) == 1:
                    raise TypeError('bad sort specification.')

            sort_specifier = key_or_list

        elif isinstance(key_or_list, basestring):
            if direction is not None:
                if not isinstance(direction, int) or not abs(direction) == 1:
                    raise TypeError('bad sort specification.')
            else:
                # default ASCENDING
                direction = 1

            sort_specifier = [(key_or_list, direction)]

        else:
            raise ValueError('Wrong input, pass a field name and a direction,'
                             ' or pass a list of (key, direction) pairs.')

        # sorting

        _cursordat = self.cursordat

        total = len(_cursordat)
        pre_sect_stack = list()
        for pair in sort_specifier:

            is_reverse = bool(1-pair[1])
            value_stack = list()
            for index, data in enumerate(_cursordat):

                # get field value

                not_found = None
                for key in pair[0].split('.'):
                    not_found = True

                    if isinstance(data, dict) and key in data:
                        data = copy.deepcopy(data[key])
                        not_found = False

                    elif isinstance(data, list):
                        if not is_reverse and len(data) == 1:
                            # MongoDB treat [{data}] as {data}
                            # when finding fields
                            if isinstance(data[0], dict) and key in data[0]:
                                data = copy.deepcopy(data[0][key])
                                not_found = False

                        elif is_reverse:
                            # MongoDB will keep finding field in reverse mode
                            for _d in data:
                                if isinstance(_d, dict) and key in _d:
                                    data = copy.deepcopy(_d[key])
                                    not_found = False
                                    break

                    if not_found:
                        break

                # parsing data for sorting

                if not_found:
                    # treat no match as None
                    data = None

                value = self._order(data, is_reverse)

                # read previous section
                pre_sect = pre_sect_stack[index] if pre_sect_stack else 0
                # inverse if in reverse mode
                # for keeping order as ASCENDING after sort
                pre_sect = (total - pre_sect) if is_reverse else pre_sect
                _ind = (total - index) if is_reverse else index

                value_stack.append((pre_sect, value, _ind))

            # sorting cursor data

            value_stack.sort(reverse=is_reverse)

            ordereddat = list()
            sect_stack = list()
            sect_id = -1
            last_dat = None
            for dat in value_stack:
                # restore if in reverse mode
                _ind = (total - dat[-1]) if is_reverse else dat[-1]
                ordereddat.append(_cursordat[_ind])

                # define section
                # maintain the sorting result in next level sorting
                if not dat[1] == last_dat:
                    sect_id += 1
                sect_stack.append(sect_id)
                last_dat = dat[1]

            # save result for next level sorting
            _cursordat = ordereddat
            pre_sect_stack = sect_stack

        # done

        self.cursordat = _cursordat

        return self

    def hasNext(self):
        """
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
        """
        Returns the next record

        :return:
        """
        self.cursorpos += 1
        return self.cursordat[self.cursorpos]

    def count(self, with_limit_and_skip=False):
        """
        Returns the number of records in the current cursor

        :return: number of records
        """
        return len(self.cursordat)


class TinyGridFS(object):
    """GridFS for tinyDB"""
    def __init__(self, *args, **kwargs):
        self.database = None

    def GridFS(self, tinydatabase):
        """TODO: Must implement yet"""
        self.database = tinydatabase
        return self


def generate_id():
    """Generate new UUID"""
    # TODO: Use six.string_type to Py3 compat
    try:
        return unicode(uuid1()).replace(u"-", u"")
    except NameError:
        return str(uuid1()).replace(u"-", u"")
