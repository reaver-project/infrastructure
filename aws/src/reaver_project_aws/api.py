import boto3


class AwsApi:
    def __init__(self, profile, region, session_factory=None):
        factory = session_factory or boto3.Session
        self._session_factory = factory
        self._session = factory(profile_name=profile, region_name=region)
        self._region = region
        self._clients = {}

    def client(self, service):
        if service not in self._clients:
            self._clients[service] = self._session.client(
                service,
                region_name=self._region,
            )
        return self._clients[service]

    def collect(self, service, operation, result_key, **arguments):
        client = self.client(service)
        if client.can_paginate(operation):
            return [
                item
                for page in client.get_paginator(operation).paginate(**arguments)
                for item in page.get(result_key, [])
            ]
        return getattr(client, operation)(**arguments).get(result_key, [])

    def root_iam(self, credentials):
        session = self._session_factory(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            region_name=self._region,
        )
        return session.client("iam", region_name=self._region)
