from unittest.mock import MagicMock


class Session:
    def __init__(self, clients):
        self.clients = clients

    def client(self, service, **_arguments):
        return self.clients[service]


class SessionFactory:
    def __init__(self, clients, root_iam=None):
        self.session = Session(clients)
        self.root_session = Session({"iam": root_iam}) if root_iam else None

    def __call__(self, **arguments):
        if "aws_access_key_id" in arguments:
            if self.root_session is None:
                raise AssertionError("Unexpected temporary credential session.")
            return self.root_session
        return self.session


def client(**operations):
    result = MagicMock()
    result.can_paginate.return_value = False
    for name, value in operations.items():
        getattr(result, name).return_value = value
    return result
