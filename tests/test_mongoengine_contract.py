import mongoengine as me
from bson import ObjectId

import tinymongo


def test_mongoengine_crud_contract(tmp_path):
    alias = "tinymongo-contract"
    me.connect(
        "odmtest",
        alias=alias,
        host="mongodb://localhost",
        mongo_client_class=tinymongo.MongoClient,
        tinymongo_folder=str(tmp_path),
        uuidRepresentation="standard",
    )

    class Person(me.Document):
        id = me.StringField(primary_key=True, default=tinymongo.generate_id)
        name = me.StringField(required=True)
        score = me.IntField(default=0)

        meta = {"db_alias": alias}

    class NativePerson(me.Document):
        name = me.StringField(required=True)

        meta = {"db_alias": alias, "collection": "native_people"}

    try:
        Person.drop_collection()
        person = Person(name="Ada").save()
        person.score = 2
        person.save()

        assert Person.objects.get(name="Ada").score == 2
        assert Person.objects(name="Ada").update_one(inc__score=3) == 1
        assert Person.objects.get(name="Ada").score == 5
        assert Person.objects(name="Ada").delete() == 1
        assert Person.objects.count() == 0

        native = NativePerson(name="Grace").save()
        assert isinstance(native.id, ObjectId)
        assert NativePerson.objects.get(id=native.id).name == "Grace"
    finally:
        me.disconnect(alias=alias)
