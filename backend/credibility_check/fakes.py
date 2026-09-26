"""In-memory stand-ins for the AWS clients used by the credibility check Lambdas."""

import copy
import io
import re


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeTable:
    def __init__(self, key_names):
        self.key_names = key_names
        self.items = {}
        self.history = []  # every update, in order

    def _key(self, key):
        return tuple(key[k] for k in self.key_names)

    def put_item(self, Item):
        self.items[self._key(Item)] = copy.deepcopy(Item)

    def get_item(self, Key):
        item = self.items.get(self._key(Key))
        return {"Item": copy.deepcopy(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        item = self.items.setdefault(self._key(Key), dict(Key))
        assignments = UpdateExpression.removeprefix("SET ").split(", ")
        changes = {}
        for a in assignments:
            name, value = (p.strip() for p in a.split(" = "))
            changes[ExpressionAttributeNames.get(name, name)] = ExpressionAttributeValues[value]
        item.update(changes)
        self.history.append(changes)

    def query(self, KeyConditionExpression, ExpressionAttributeValues, ScanIndexForward=True, Limit=None):
        m = re.fullmatch(r"(\w+) = (:\w+)", KeyConditionExpression)
        field, value = m.group(1), ExpressionAttributeValues[m.group(2)]
        rows = [copy.deepcopy(i) for i in self.items.values() if i.get(field) == value]
        rows.sort(key=lambda i: i[self.key_names[1]], reverse=not ScanIndexForward)
        return {"Items": rows[:Limit] if Limit else rows}


class FakeS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError("NoSuchKey")
        data = self.objects[Key]
        return {"Body": io.BytesIO(data), "ContentLength": len(data)}


class FakeLambda:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def invoke(self, **kwargs):
        if self.fail:
            raise ClientError("ServiceException")
        self.calls.append(kwargs)
        return {"StatusCode": 202}


class FakeTextract:
    def __init__(self, lines=None, fail=False):
        self.lines = lines or []
        self.fail = fail
        self.calls = []

    def detect_document_text(self, Document):
        self.calls.append(Document)
        if self.fail:
            raise ClientError("InvalidParameterException")
        blocks = [{"BlockType": "PAGE"}] + [{"BlockType": "LINE", "Text": t} for t in self.lines]
        return {"Blocks": blocks}
