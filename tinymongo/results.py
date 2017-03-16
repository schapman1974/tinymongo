"""Result class definitions."""


class _WriteResult(object):
    """Base class for write result classes."""

    def __init__(self, acknowledged=True):
        self.acknowledged = acknowledged  # here only to PyMongo compat


class InsertOneResult(_WriteResult):
    """The return type for :meth:`~tinymongo.TinyMongoCollection.insert_one`.
    """

    __slots__ = ("__inserted_id", "__acknowledged", "__eid")

    def __init__(self, eid, inserted_id, acknowledged=True):
        self.__eid = eid
        self.__inserted_id = inserted_id
        super(InsertOneResult, self).__init__(acknowledged)

    @property
    def inserted_id(self):
        """The inserted document's _id."""
        return self.__inserted_id

    @property
    def eid(self):
        """The inserted document's tinyDB eid."""
        return self.__eid


class InsertManyResult(_WriteResult):
    """The return type for :meth:`~tinymongo.TinyMongoCollection.insert_many`.
    """

    __slots__ = ("__inserted_ids", "__acknowledged", "__eids")

    def __init__(self, eids, inserted_ids, acknowledged=True):
        self.__eids = eids
        self.__inserted_ids = inserted_ids
        super(InsertManyResult, self).__init__(acknowledged)

    @property
    def inserted_ids(self):
        """A list of _ids of the inserted documents, in the order provided."""
        return self.__inserted_ids

    @property
    def eids(self):
        """A list of _ids of the inserted documents, in the order provided."""
        return self.__eids


class UpdateResult(_WriteResult):
    """The return type for :meth:`~tinymongo.TinyMongoCollection.update_one`,
    :meth:`~tinymongo.TinyMongoCollection.update_many`, and
    :meth:`~tinymongo.TinyMongoCollection.replace_one`.
    """

    __slots__ = ("__raw_result", "__acknowledged")

    def __init__(self, raw_result, acknowledged=True):
        self.__raw_result = raw_result
        super(UpdateResult, self).__init__(acknowledged)

    @property
    def raw_result(self):
        """The raw result document returned by the server."""
        return self.__raw_result

    @property
    def matched_count(self):
        """The number of documents matched for this update."""
        # TODO: Implement this

    @property
    def modified_count(self):
        """The number of documents modified.
        """
        # TODO: Implement this

    @property
    def upserted_id(self):
        """The _id of the inserted document if an upsert took place. Otherwise
        ``None``.
        """
        # TODO: Implement this


class DeleteResult(_WriteResult):
    """The return type for :meth:`~tinymongo.TinyMongoCollection.delete_one`
    and :meth:`~tinymongo.TinyMongoCollection.delete_many`"""

    __slots__ = ("__raw_result", "__acknowledged")

    def __init__(self, raw_result, acknowledged=True):
        self.__raw_result = raw_result
        super(DeleteResult, self).__init__(acknowledged)

    @property
    def raw_result(self):
        """The raw result document returned by the server."""
        return self.__raw_result

    @property
    def deleted_count(self):
        """The number of documents deleted."""
        if isinstance(self.raw_result, list):
            return len(self.raw_result)
        else:
            return self.raw_result
