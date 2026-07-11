from datetime import datetime

try:
    from tinydb_serialization import Serializer
except ImportError:
    raise RuntimeError(
        "Cannot import tinydb_serialization due to {0} "
        'install it with `pip install "tinymongo[serialization]"`'
    )


class DateTimeSerializer(Serializer):
    OBJ_CLASS = datetime

    def __init__(self, dateformat="%Y-%m-%dT%H:%M:%S", *args, **kwargs):
        # super(DateTimeSerializer, self).__init__(*args, **kwargs)
        self._format = dateformat

    def encode(self, obj):
        return obj.strftime(self._format)

    def decode(self, s):
        return self.OBJ_CLASS.strptime(s, self._format)
