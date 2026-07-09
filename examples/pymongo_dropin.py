"""PyMongo-style usage backed by TinyMongo.

Change only the import line to try local file-backed storage:

    import tinymongo as pymongo

The rest of this file intentionally uses common PyMongo names.
"""

import tempfile

import tinymongo as pymongo


def run_example(path=None):
    db_path = path or tempfile.mkdtemp(prefix="tinymongo-pymongo-dropin-")
    client = pymongo.MongoClient(
        "mongodb://localhost:27017",
        serverSelectionTimeoutMS=2000,
        tinymongo_folder=db_path,
    )
    users = client.app.users

    users.delete_many({})
    users.create_index("email")
    users.insert_many(
        [
            {"_id": 1, "email": "ada@example.com", "name": "Ada", "score": 7},
            {"_id": 2, "email": "grace@example.com", "name": "Grace", "score": 9},
        ]
    )
    users.update_one({"email": "ada@example.com"}, {"$inc": {"score": 1}})

    rows = list(users.find({"score": {"$gte": 8}}).sort("score", pymongo.DESCENDING))
    users.delete_one({"_id": 1})
    client.close()
    return rows


if __name__ == "__main__":
    for row in run_example():
        print(row)
