import asyncio
import unittest

import pymysql
from fastapi import HTTPException

from backend.api.routes.catalog import repo_call


class DatabaseWriteErrorTests(unittest.TestCase):
    def test_lock_write_is_reported_as_service_unavailable(self):
        async def run():
            def locked_write():
                raise pymysql.OperationalError(
                    1290,
                    "The MySQL server is running with the LOCK_WRITE option so it cannot execute this statement",
                )

            with self.assertRaises(HTTPException) as context:
                await repo_call(locked_write)
            return context.exception

        error = asyncio.run(run())

        self.assertEqual(error.status_code, 503)
        self.assertIn("LOCK_WRITE", error.detail)


if __name__ == "__main__":
    unittest.main()
