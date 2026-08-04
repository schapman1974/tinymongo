import asyncio
import datetime

import pytest
import tinymongo


beanie = pytest.importorskip("beanie")
pydantic = pytest.importorskip("pydantic")
pymongo = pytest.importorskip("pymongo")


class InterviewQuestion(pydantic.BaseModel):
    text: str
    asked: bool = False


class Interview(beanie.Document):
    title: str
    guest_name: str
    created_date: datetime.datetime
    questions: list[InterviewQuestion] = pydantic.Field(default_factory=list)

    class Settings:
        name = "beanie_interviews"
        indexes = [
            pymongo.IndexModel(
                [("guest_name", pymongo.ASCENDING)],
                name="guest_name_asc",
            )
        ]


def test_beanie_initializes_and_runs_crud_without_application_shims(tmp_path):
    async def scenario():
        client = tinymongo.AsyncMongoClient(
            tinymongo_folder=str(tmp_path),
            backend="sqlite",
        )
        try:
            await beanie.init_beanie(
                database=client.interviewcue,
                document_models=[Interview],
            )

            interview = Interview(
                title="Python's origin",
                guest_name="Guido",
                created_date=datetime.datetime(2026, 8, 3, 12, 0),
                questions=[InterviewQuestion(text="How did Python begin?")],
            )
            await interview.insert()

            loaded = await Interview.get(interview.id)
            assert loaded is not None
            assert loaded.guest_name == "Guido"
            assert loaded.questions == [InterviewQuestion(text="How did Python begin?")]

            loaded.title = "The origins of Python"
            await loaded.replace()
            replaced = await Interview.get(interview.id)
            assert replaced is not None
            assert replaced.title == "The origins of Python"

            assert await Interview.find(Interview.guest_name == "Guido").count() == 1
            await replaced.delete()
            assert await Interview.find_all().count() == 0
        finally:
            await client.close()

    asyncio.run(scenario())
