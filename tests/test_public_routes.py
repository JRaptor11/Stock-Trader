import json
import os
import unittest
from unittest.mock import patch

from routes.public_routes import uptime_health_check


class PublicHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_uptime_health_exposes_safe_deployment_identity(self):
        with patch.dict(os.environ, {
            "RENDER_GIT_COMMIT": "test-commit",
            "RENDER_GIT_BRANCH": "layers",
        }, clear=False):
            response = await uptime_health_check()
        payload = json.loads(response.body)
        self.assertEqual("ok", payload["status"])
        self.assertEqual("test-commit", payload["git_commit"])
        self.assertEqual("layers", payload["git_branch"])
        self.assertRegex(payload["python_version"], r"^\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
