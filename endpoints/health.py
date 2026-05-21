from collections.abc import Mapping

from werkzeug import Request, Response

from dify_plugin.interfaces.endpoint import Endpoint


class HealthEndpoint(Endpoint):
    def _invoke(self, r: Request, values: Mapping, settings: Mapping) -> Response:
        return Response(
            response='{"status":"ok","plugin":"convoprobe","version":"0.0.11"}',
            status=200,
            content_type="application/json",
        )
